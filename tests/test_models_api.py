"""Models & memory over HTTP: which model each role uses, and choosing another."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.catalog import catalog
from lectern.core import settings

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def api(paths, monkeypatch):
    """A 4 GB card: the 4B is the recommended chat model."""
    monkeypatch.setenv("LECTERN_TIER", "minimum")
    app = create_app(paths)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    return TestClient(app, base_url=BASE), headers, app


def _stored(app) -> dict:  # noqa: ANN001
    return settings.get(app.state.sessions, settings.MODELS_KEY, settings.MODEL_DEFAULTS)


def _choose(client, headers, role: str, model_id: str | None):  # noqa: ANN001, ANN202
    return client.put(f"/api/models/roles/{role}", json={"model_id": model_id}, headers=headers)


def test_the_list_says_what_each_role_uses_and_what_suits_this_computer(api):
    client, headers, _ = api

    body = client.get("/api/models", headers=headers).json()

    assert body["defaults"]["main"] == "qwen3.5-4b"  # recommended for this computer
    assert body["chosen"] == body["defaults"]  # nothing chosen yet
    suits = {m["id"]: m["suits_tier"] for m in body["models"]}
    assert suits["qwen3.5-4b"] is True
    assert suits["qwen3.5-35b-a3b"] is False  # more than a 4 GB card is meant for


def test_choosing_a_model_for_a_role_is_saved_and_reported(api):
    client, headers, app = api

    saved = _choose(client, headers, "main", "qwen3.5-9b")

    assert saved.status_code == 200
    assert saved.json()["chosen"]["main"] == "qwen3.5-9b"
    assert saved.json()["defaults"]["main"] == "qwen3.5-4b"  # the recommendation stays
    assert _stored(app)["main"] == "qwen3.5-9b"
    assert client.get("/api/models", headers=headers).json()["chosen"]["main"] == "qwen3.5-9b"
    assert _stored(app)["ocr"] is None  # the other roles weren't touched


def test_choosing_the_recommended_model_stores_no_choice(api):
    client, headers, app = api
    _choose(client, headers, "main", "qwen3.5-9b")

    back = _choose(client, headers, "main", "qwen3.5-4b")

    # Not pinned to the 4B: the role follows the recommendation again.
    assert back.json()["chosen"]["main"] == "qwen3.5-4b"
    assert _stored(app)["main"] is None
    _choose(client, headers, "main", "qwen3.5-9b")
    assert _choose(client, headers, "main", None).json()["chosen"]["main"] == "qwen3.5-4b"
    assert _stored(app)["main"] is None


def test_a_restricted_licence_model_can_be_chosen(api):
    client, headers, _ = api
    # Its licence is asked about when it's downloaded, not when it's chosen.
    assert _choose(client, headers, "ocr.hand", "chandra-ocr-2").status_code == 200


@pytest.mark.parametrize(
    ("role", "model_id", "status", "says"),
    [
        ("stt", "qwen3.5-4b", 404, "no 'stt' role"),
        ("main", "nope", 404, "No model 'nope'"),
        ("main", "paddleocr-vl-1.6", 409, "can't be used for the 'main' role"),
        ("main", "qwen3.5-4b-mlx", 409, "MLX build"),
    ],
)
def test_a_choice_that_cannot_be_made_is_refused_and_nothing_is_stored(
    api, role, model_id, status, says
):
    client, headers, app = api

    refused = _choose(client, headers, role, model_id)

    assert refused.status_code == status
    assert says in refused.json().get("detail", "")
    assert set(_stored(app).values()) == {None}


def test_choosing_needs_the_token(api):
    client, _, app = api
    refused = client.put(
        "/api/models/roles/main", json={"model_id": "qwen3.5-9b"}, headers={"Origin": BASE}
    )
    assert refused.status_code == 401
    assert _stored(app)["main"] is None


def test_a_change_unloads_the_roles_idle_model(api):
    client, headers, app = api
    unloaded: list[str | None] = []

    async def record(role: str | None = None) -> list[str]:
        unloaded.append(role)
        return []

    app.state.runtime.unload_between_replies = record

    _choose(client, headers, "code", "qwen2.5-coder-3b")

    assert unloaded == ["code"]  # that role's model only, and never mid-reply

    # Saved again as it is, or set to what it already uses: the loaded model is the right
    # one, and unloading it would only make the next use wait for a load.
    _choose(client, headers, "code", "qwen2.5-coder-3b")
    _choose(client, headers, "main", None)
    _choose(client, headers, "main", "qwen3.5-4b")  # the recommended one: stored as no choice
    assert unloaded == ["code"]


# --- installed models, and removing one -------------------------------------------------------


def _downloaded(app, model_id: str, *, recorded: bool = True) -> int:  # noqa: ANN001
    """Put a model's files on disk, as a finished download leaves them. Returns their size."""
    entry = catalog.load().get(model_id)
    manager = app.state.downloads
    for file in entry.files.all():
        path = manager.file_path(entry, file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
    size = 10 * len(entry.files.all())
    if recorded:
        manager._save(
            model_id, status="done", bytes_done=size, bytes_total=size, revision=entry.revision
        )
    return size


def _models(client, headers) -> dict:  # noqa: ANN001
    body = client.get("/api/models", headers=headers).json()
    return {m["id"]: m for m in body["models"]} | {"": body}


def test_the_list_says_what_is_installed_at_which_version_and_who_uses_it(api):
    client, headers, app = api
    size = _downloaded(app, "qwen3.5-4b")
    _downloaded(app, "qwen3.5-9b", recorded=False)  # copied in by hand
    left = app.state.downloads.models_dir / "model-of-an-older-lectern"
    left.mkdir()
    (left / "weights.gguf").write_bytes(b"x" * 7)

    models = _models(client, headers)

    chat = models["qwen3.5-4b"]
    assert chat["installed"]["revision"] == catalog.load().get("qwen3.5-4b").revision
    assert chat["installed"]["bytes"] == size
    assert chat["installed"]["at"].startswith("20")  # a date the page can show
    assert chat["used_by"] == ["main"]
    assert models["qwen3.5-9b"]["installed"]["revision"] is None  # version not recorded
    assert models["qwen3.5-9b"]["used_by"] == []
    assert models["qwen3.5-35b-a3b"]["installed"] is None
    assert models[""]["unlisted"] == [{"name": "model-of-an-older-lectern", "bytes": 7}]


def test_an_mlx_build_is_used_by_its_models_role_while_mlx_is_on(api, monkeypatch):
    client, headers, app = api
    monkeypatch.setattr("lectern.runners.mlx.mlx_available", lambda: True)
    assert _models(client, headers)["qwen3.5-4b-mlx"]["used_by"] == []

    settings.put(app.state.sessions, settings.RUNTIME_KEY, {"mlx_enabled": True})

    assert _models(client, headers)["qwen3.5-4b-mlx"]["used_by"] == ["main"]
    assert _models(client, headers)["qwen3.5-9b-mlx"]["used_by"] == []


def test_removing_a_model_deletes_it_and_the_list_says_so(api):
    client, headers, app = api
    _downloaded(app, "qwen3.5-4b")
    folder = app.state.downloads.models_dir / "qwen3.5-4b"

    removed = client.delete("/api/models/qwen3.5-4b", headers=headers)

    assert removed.status_code == 200
    assert not folder.exists()
    chat = next(m for m in removed.json()["models"] if m["id"] == "qwen3.5-4b")
    assert chat["installed"] is None and chat["download"]["status"] == "none"
    assert removed.json()["chosen"]["main"] == "qwen3.5-4b"  # the role still asks for it


def test_a_folder_no_model_owns_can_be_removed_too(api):
    client, headers, app = api
    left = app.state.downloads.models_dir / "model-of-an-older-lectern"
    left.mkdir(parents=True)
    (left / "weights.gguf").write_bytes(b"x")

    removed = client.delete("/api/models/model-of-an-older-lectern", headers=headers)

    assert removed.status_code == 200 and removed.json()["unlisted"] == []
    assert not left.exists()


def test_a_model_in_use_is_not_removed(api):
    from lectern.runtime.supervisor import ModelInUse

    client, headers, app = api
    _downloaded(app, "qwen3.5-4b")

    async def in_use(model_id, action):  # noqa: ANN001, ANN202
        raise ModelInUse(model_id)

    app.state.runtime.without_model = in_use

    refused = client.delete("/api/models/qwen3.5-4b", headers=headers)

    assert refused.status_code == 409
    assert "Qwen3.5 4B is in use" in refused.json()["detail"]
    assert (app.state.downloads.models_dir / "qwen3.5-4b").is_dir()


def test_the_files_go_only_while_the_runtime_holds_the_model_unloaded(api):
    client, headers, app = api
    _downloaded(app, "qwen3.5-4b")
    folder = app.state.downloads.models_dir / "qwen3.5-4b"
    seen: list[tuple[str, bool, bool]] = []

    async def holding(model_id, action):  # noqa: ANN001, ANN202
        before = folder.exists()
        await action()
        seen.append((model_id, before, folder.exists()))

    app.state.runtime.without_model = holding

    client.delete("/api/models/qwen3.5-4b", headers=headers)

    assert seen == [("qwen3.5-4b", True, False)]


@pytest.mark.parametrize(
    ("make", "name", "status", "says"),
    [
        ("nothing", "nope", 404, "no model folder 'nope'"),
        ("link", "linked", 409, "link"),
        ("file", "a-file.txt", 409, "isn't a folder"),
    ],
)
def test_what_is_not_a_model_folder_is_not_removed(api, tmp_path, make, name, status, says):
    client, headers, app = api
    models_dir = app.state.downloads.models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.gguf").write_bytes(b"someone else's")
    if make == "link":
        (models_dir / name).symlink_to(elsewhere, target_is_directory=True)
    if make == "file":
        (models_dir / name).write_bytes(b"x")

    refused = client.delete(f"/api/models/{name}", headers=headers)

    assert refused.status_code == status and says in refused.json()["detail"]
    assert (elsewhere / "keep.gguf").exists()
    assert make == "nothing" or (models_dir / name).exists() or (models_dir / name).is_symlink()


def test_removing_needs_the_token(api):
    client, _, app = api
    _downloaded(app, "qwen3.5-4b")

    refused = client.delete("/api/models/qwen3.5-4b", headers={"Origin": BASE})

    assert refused.status_code == 401
    assert (app.state.downloads.models_dir / "qwen3.5-4b").is_dir()
