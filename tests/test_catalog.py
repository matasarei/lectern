from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lectern.api import models as models_api
from lectern.app import create_app
from lectern.catalog import catalog
from lectern.hardware.probe import HardwareInfo
from lectern.hardware.tiers import Tier

ROLES_EVERY_TIER_NEEDS = {"main", "ocr", "ocr.hand", "code", "image"}


def test_shipped_catalog_loads():
    cat = catalog.load()
    assert len(cat.models) >= 8


@pytest.mark.parametrize("tier", list(Tier))
def test_every_tier_has_a_default_for_every_role(tier):
    defaults = catalog.load().defaults_for(tier)
    assert set(defaults) == ROLES_EVERY_TIER_NEEDS


@pytest.mark.parametrize("tier", list(Tier))
def test_defaults_are_permissive_and_fully_described(tier):
    for role, entry in catalog.load().defaults_for(tier).items():
        assert entry.permissive, f"{role}/{tier}: {entry.id} is not permissively licensed"
        assert all(f.size > 0 and len(f.sha256) == 64 for f in entry.files.all())


def test_restricted_models_need_a_licence_notice():
    cat = catalog.load()
    for model_id in ("qwen2.5-coder-3b", "chandra-ocr-2"):
        entry = cat.get(model_id)
        assert entry is not None
        assert entry.licence_notice
        assert entry.defaults == {}


def test_minimum_tier_defaults_match_the_docs():
    ids = {r: e.id for r, e in catalog.load().defaults_for(Tier.MINIMUM).items()}
    assert ids == {
        "main": "qwen3.5-4b",
        "ocr": "paddleocr-vl-1.6",
        "ocr.hand": "qwen3-vl-4b",
        "code": "qwen2.5-coder-1.5b",
        "image": "flux2-klein-4b",
    }


def test_recommended_tier_defaults_match_the_docs():
    ids = {r: e.id for r, e in catalog.load().defaults_for(Tier.RECOMMENDED).items()}
    assert ids == {
        "main": "qwen3.5-9b",
        "ocr": "paddleocr-vl-1.6",
        "ocr.hand": "qwen3-vl-8b",
        "code": "qwen2.5-coder-7b",
        "image": "flux2-klein-4b-q8",
    }


def _raw():
    return yaml.safe_load(
        catalog.resources.files("lectern.catalog").joinpath("models.yaml").read_text()
    )


def test_a_restricted_default_is_rejected():
    raw = _raw()
    chandra = next(m for m in raw["models"] if m["id"] == "chandra-ocr-2")
    chandra["defaults"] = {"ocr.hand": ["comfortable"]}
    with pytest.raises(ValidationError, match="restricted-licence"):
        catalog.Catalog.model_validate(raw)


def test_two_defaults_for_one_role_and_tier_are_rejected():
    raw = _raw()
    coder7 = next(m for m in raw["models"] if m["id"] == "qwen2.5-coder-7b")
    coder7["defaults"] = {"code": ["recommended", "minimum"]}
    with pytest.raises(ValidationError, match="two defaults"):
        catalog.Catalog.model_validate(raw)


def test_a_bad_sha256_is_rejected():
    raw = _raw()
    raw["models"][0]["files"]["model"][0]["sha256"] = "abc"
    with pytest.raises(ValidationError):
        catalog.Catalog.model_validate(raw)


def test_models_api_flags_restricted_and_lists_defaults(paths, monkeypatch):
    monkeypatch.setattr(
        models_api,
        "probe",
        lambda: HardwareInfo(
            os="linux", arch="x86_64", cpu="c", cpu_cores=4, ram_total=1, ram_available=1
        ),
    )
    app = create_app(paths)
    client = TestClient(app, base_url="http://127.0.0.1:8765")

    body = client.get("/api/models", headers={"X-Lectern-Token": app.state.token}).json()

    assert body["tier"] == "below_minimum"
    assert body["defaults"]["main"] == "qwen3.5-4b"
    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["chandra-ocr-2"]["licence_notice"] is True
    assert by_id["qwen3.5-4b"]["licence_notice"] is False
    assert by_id["qwen3.5-4b"]["size"] == 2740937888
    # A page pairs a model with its MLX build by these two.
    assert (by_id["qwen3.5-4b"]["format"], by_id["qwen3.5-4b"]["replaces"]) == ("gguf", None)
    build = by_id["qwen3.5-4b-mlx"]
    assert (build["format"], build["replaces"]) == ("mlx", "qwen3.5-4b")


@pytest.mark.parametrize(
    "bad", ["../escape.gguf", "/etc/passwd", "a/../../b.gguf", "dir\\file.gguf", "", "a//b.gguf"]
)
def test_file_paths_must_stay_in_the_model_folder(bad):
    raw = _raw()
    raw["models"][0]["files"]["model"][0]["path"] = bad
    with pytest.raises(ValidationError, match="plain relative path"):
        catalog.Catalog.model_validate(raw)


def test_a_subfolder_path_is_fine():
    raw = _raw()
    raw["models"][0]["files"]["model"][0]["path"] = "Q4_K_M/model-00001-of-00002.gguf"
    catalog.Catalog.model_validate(raw)


def test_every_ocr_model_says_how_to_ask_it():
    for entry in catalog.load().models:
        if catalog.OCR_ROLES & set(entry.roles):
            assert entry.ocr is not None and entry.ocr.prompt and entry.ocr.max_side >= 448
        else:
            assert entry.ocr is None


def test_an_ocr_model_without_its_prompt_is_rejected():
    raw = _raw()
    paddle = next(m for m in raw["models"] if m["id"] == "paddleocr-vl-1.6")
    del paddle["ocr"]
    with pytest.raises(ValidationError, match="needs an `ocr:` block"):
        catalog.Catalog.model_validate(raw)


def test_an_ocr_block_on_a_chat_model_is_rejected():
    raw = _raw()
    coder = next(m for m in raw["models"] if m["id"] == "qwen2.5-coder-7b")
    coder["ocr"] = {"prompt": "OCR:", "max_side": 1024}
    with pytest.raises(ValidationError, match="only models with an OCR role"):
        catalog.Catalog.model_validate(raw)


def test_a_page_side_below_what_a_model_can_read_is_rejected():
    raw = _raw()
    paddle = next(m for m in raw["models"] if m["id"] == "paddleocr-vl-1.6")
    paddle["ocr"]["max_side"] = 100
    with pytest.raises(ValidationError, match="max_side"):
        catalog.Catalog.model_validate(raw)


# --- MLX builds: the same model in MLX's format, standing in for a GGUF one on a Mac ---------

MLX_BUILDS = {
    "qwen3.5-4b-mlx": "qwen3.5-4b",
    "qwen3.5-9b-mlx": "qwen3.5-9b",
    "qwen3.5-35b-a3b-mlx": "qwen3.5-35b-a3b",
}


@pytest.mark.parametrize(("build_id", "model_id"), MLX_BUILDS.items())
def test_each_chat_model_has_an_mlx_build(build_id, model_id):
    cat = catalog.load()
    build, model = cat.get(build_id), cat.get(model_id)
    assert build is not None and model is not None
    assert build.format == "mlx" and build.replaces == model_id
    assert cat.mlx_build_of(model) is build
    assert build.roles == model.roles and build.ctx == model.ctx
    assert build.permissive and build.licence == "Apache-2.0"
    assert not build.defaults  # a choice, never what a role gets by default
    # The runtime takes the model's folder from its first file.
    assert build.files.model[0].path == "config.json"
    assert build.files.mmproj is None
    assert "/" not in "".join(f.path for f in build.files.model)  # one flat folder


def test_a_model_without_an_mlx_build_has_none():
    cat = catalog.load()
    assert cat.mlx_build_of(cat.get("paddleocr-vl-1.6")) is None
    assert cat.mlx_build_of(cat.get("qwen3.5-4b-mlx")) is None  # a build has no build


@pytest.mark.parametrize("tier", list(Tier))
def test_no_default_is_an_mlx_build(tier):
    assert all(entry.format != "mlx" for entry in catalog.load().defaults_for(tier).values())


def _raw_with_build() -> tuple[dict, dict]:
    raw = _raw()
    return raw, next(m for m in raw["models"] if m["id"] == "qwen3.5-4b-mlx")


def test_an_mlx_build_must_say_what_it_replaces():
    raw, build = _raw_with_build()
    del build["replaces"]
    with pytest.raises(ValidationError, match="replaces"):
        catalog.Catalog.model_validate(raw)


def test_only_an_mlx_build_replaces_another_model():
    raw = _raw()
    next(m for m in raw["models"] if m["id"] == "qwen3.5-9b")["replaces"] = "qwen3.5-4b"
    with pytest.raises(ValidationError, match="only an MLX build"):
        catalog.Catalog.model_validate(raw)


@pytest.mark.parametrize(
    ("replaces", "why"),
    [
        ("no-such-model", "isn't in the catalog"),
        ("qwen3.5-9b-mlx", "must be a GGUF model"),  # another build
        ("qwen2.5-coder-7b", "same roles"),  # a model for another role
        ("qwen3.5-9b", "two MLX builds"),  # one that already has its build
    ],
)
def test_an_mlx_build_replaces_one_gguf_model_with_its_roles(replaces, why):
    raw, build = _raw_with_build()
    build["replaces"] = replaces
    with pytest.raises(ValidationError, match=why):
        catalog.Catalog.model_validate(raw)


def test_an_mlx_build_cant_be_a_default():
    raw, build = _raw_with_build()
    build["defaults"] = {"main": ["comfortable"]}
    with pytest.raises(ValidationError, match="can't be a default"):
        catalog.Catalog.model_validate(raw)


def test_an_mlx_builds_first_file_is_its_config():
    raw, build = _raw_with_build()
    build["files"]["model"].reverse()
    with pytest.raises(ValidationError, match="config.json"):
        catalog.Catalog.model_validate(raw)


def test_the_models_a_role_can_be_given_leave_out_mlx_builds():
    models = catalog.load()
    assert [m.id for m in models.choices_for("main")] == [
        "qwen3.5-4b",
        "qwen3.5-9b",
        "qwen3.5-35b-a3b",
    ]
    assert {m.id for m in models.choices_for("ocr.hand")} == {
        "qwen3-vl-4b",
        "qwen3-vl-8b",
        "chandra-ocr-2",  # a restricted licence doesn't stop it being chosen
    }
    assert models.choices_for("stt") == []


# --- image models ------------------------------------------------------------------------------

_SHA = "a" * 64
_REV = "b" * 40


def _image_entry(**changes):
    entry = {
        "id": "test-image",
        "title": "Test image model",
        "quant": "Q4_0",
        "roles": ["image"],
        "format": "sd",
        "repo": "someone/test-image-GGUF",
        "revision": _REV,
        "licence": "Apache-2.0",
        "permissive": True,
        "files": {
            "model": [{"path": "diffusion.gguf", "size": 10, "sha256": _SHA}],
            "text_encoder": {
                "path": "encoder.gguf",
                "size": 10,
                "sha256": _SHA,
                "repo": "someone/encoder-GGUF",
                "revision": "c" * 40,
            },
            "vae": {"path": "vae.safetensors", "size": 10, "sha256": _SHA},
        },
        "image": {
            "steps": 4,
            "cfg": 1.0,
            "sampler": "euler",
            "sizes": {"minimum": {"default": 768, "max_pixels": 1024 * 1024}},
        },
    }
    entry.update(changes)
    return entry


def _with(entry):
    raw = _raw()
    raw["models"].append(entry)
    return catalog.Catalog.model_validate(raw)


def test_an_image_model_loads_with_files_from_other_repos():
    entry = _with(_image_entry()).get("test-image")
    assert [f.path for f in entry.files.all()] == [
        "diffusion.gguf",
        "encoder.gguf",
        "vae.safetensors",
    ]
    assert entry.files.text_encoder.repo == "someone/encoder-GGUF"
    assert entry.size == 30
    assert entry.suits(Tier.MINIMUM)
    assert not entry.suits(Tier.RECOMMENDED)


def test_an_image_model_needs_its_image_block():
    with pytest.raises(ValidationError, match="needs an `image:` block"):
        _with(_image_entry(image=None))


def test_an_image_model_needs_an_encoder_and_a_vae():
    entry = _image_entry()
    del entry["files"]["vae"]
    with pytest.raises(ValidationError, match="text encoder and a VAE"):
        _with(entry)


def test_the_image_role_and_the_sd_format_go_together():
    with pytest.raises(ValidationError, match="go together"):
        _with(_image_entry(format="gguf"))
    with pytest.raises(ValidationError, match="go together"):
        _with(_image_entry(roles=["main"]))


def test_a_chat_model_cant_have_an_image_block():
    raw = _raw()
    raw["models"][0]["image"] = _image_entry()["image"]
    with pytest.raises(ValidationError, match="only an image model"):
        catalog.Catalog.model_validate(raw)


def test_a_restricted_image_model_cant_be_a_default():
    entry = _image_entry(permissive=False, defaults={"image": ["minimum"]})
    with pytest.raises(ValidationError, match="restricted-licence"):
        _with(entry)


def test_an_image_default_needs_a_size_for_its_tier():
    with pytest.raises(ValidationError, match="no size for"):
        _with(_image_entry(defaults={"image": ["recommended"]}))


def test_a_files_own_repo_needs_its_own_revision():
    entry = _image_entry()
    del entry["files"]["text_encoder"]["revision"]
    with pytest.raises(ValidationError, match="needs its own revision"):
        _with(entry)


def test_two_files_cant_share_a_path():
    entry = _image_entry()
    entry["files"]["vae"]["path"] = "encoder.gguf"
    with pytest.raises(ValidationError, match="same path"):
        _with(entry)


def test_an_image_files_path_stays_in_the_model_folder():
    entry = _image_entry()
    entry["files"]["text_encoder"]["path"] = "../escape.gguf"
    with pytest.raises(ValidationError, match="plain relative path"):
        _with(entry)


def test_an_image_default_size_must_fit_its_pixels():
    entry = _image_entry()
    entry["image"]["sizes"]["minimum"] = {"default": 1024, "max_pixels": 512 * 512}
    with pytest.raises(ValidationError, match="over max_pixels"):
        _with(entry)


def test_the_shipped_image_models_are_permissive_and_pin_every_piece():
    for model_id in ("flux2-klein-4b", "flux2-klein-4b-q8", "z-image-turbo"):
        entry = catalog.load().get(model_id)
        assert entry.permissive and entry.format == "sd"
        assert entry.files.text_encoder.revision and entry.files.vae.revision
        assert entry.pin != entry.revision  # a newer pin of a piece is an update
