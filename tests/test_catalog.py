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

ROLES_EVERY_TIER_NEEDS = {"main", "ocr", "ocr.hand", "code"}


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


def test_minimum_tier_defaults_match_the_blueprint():
    ids = {r: e.id for r, e in catalog.load().defaults_for(Tier.MINIMUM).items()}
    assert ids == {
        "main": "qwen3.5-4b",
        "ocr": "paddleocr-vl-1.6",
        "ocr.hand": "qwen3-vl-4b",
        "code": "qwen2.5-coder-1.5b",
    }


def test_recommended_tier_defaults_match_the_blueprint():
    ids = {r: e.id for r, e in catalog.load().defaults_for(Tier.RECOMMENDED).items()}
    assert ids == {
        "main": "qwen3.5-9b",
        "ocr": "paddleocr-vl-1.6",
        "ocr.hand": "qwen3-vl-8b",
        "code": "qwen2.5-coder-7b",
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
