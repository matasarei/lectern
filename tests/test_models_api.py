"""Models & memory over HTTP: which model each role uses, and choosing another."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.catalog import catalog
from lectern.core import settings
from lectern.core.tables import DownloadRow

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
        ("clean", "nope", 404, "No model 'nope'"),
        ("clean", "paddleocr-vl-1.6", 409, "can't clean up scanned pages"),
        ("clean", "qwen3.5-4b-mlx", 409, "can't be held to the shape cleanup needs"),
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


# --- the model that cleans up scanned pages: a choice of its own, not a catalog role ---------

CHAT_MODELS = ["qwen3.5-4b", "qwen3.5-9b", "qwen3.5-35b-a3b"]


def test_the_list_says_which_model_cleans_pages_and_what_each_choice_can_be(api):
    client, headers, _ = api

    body = client.get("/api/models", headers=headers).json()

    # Nothing chosen, the chat on llama.cpp: the chat's own model cleans.
    assert (body["defaults"]["clean"], body["chosen"]["clean"]) == ("qwen3.5-4b", "qwen3.5-4b")
    assert body["choices"]["clean"] == CHAT_MODELS  # never an MLX build: it can't clean
    assert body["choices"]["main"] == CHAT_MODELS
    assert body["choices"]["ocr"] == ["paddleocr-vl-1.6"]
    assert "chandra-ocr-2" in body["choices"]["ocr.hand"]
    assert set(body["choices"]) == set(body["chosen"])


def test_with_the_chat_on_an_mlx_build_a_small_model_cleans(api, monkeypatch):
    client, headers, app = api
    monkeypatch.setattr("lectern.runners.mlx.mlx_supported", lambda: True)
    monkeypatch.setattr("lectern.runners.mlx.mlx_available", lambda: True)
    settings.put(app.state.sessions, settings.RUNTIME_KEY, {"mlx_enabled": True})
    _choose(client, headers, "main", "qwen3.5-9b")
    _downloaded(app, "qwen3.5-9b-mlx")

    body = client.get("/api/models", headers=headers).json()

    assert (body["chosen"]["main"], body["chosen"]["clean"]) == ("qwen3.5-9b", "qwen3.5-4b")
    assert body["defaults"]["clean"] == "qwen3.5-4b"
    used_by = {m["id"]: m["used_by"] for m in body["models"]}
    assert used_by["qwen3.5-4b"] == ["clean"]
    assert used_by["qwen3.5-9b-mlx"] == ["main"]


def test_with_mlx_on_and_neither_file_on_disk_cleanup_is_planned_for_the_build(api, monkeypatch):
    client, headers, app = api
    monkeypatch.setattr("lectern.runners.mlx.mlx_supported", lambda: True)
    monkeypatch.setattr("lectern.runners.mlx.mlx_available", lambda: True)
    settings.put(app.state.sessions, settings.RUNTIME_KEY, {"mlx_enabled": True})
    _choose(client, headers, "main", "qwen3.5-9b")

    # MLX was asked for and nothing is there yet: the build is what the chat will run on,
    # so the small model is the one to clean with, not the chat model's own 5.7 GB file.
    body = client.get("/api/models", headers=headers).json()
    assert (body["chosen"]["main"], body["chosen"]["clean"]) == ("qwen3.5-9b", "qwen3.5-4b")

    # The usual file already on disk stands in until the build is: it cleans too.
    _downloaded(app, "qwen3.5-9b")
    body = client.get("/api/models", headers=headers).json()
    assert body["chosen"]["clean"] == "qwen3.5-9b"


def test_a_model_chosen_for_cleanup_is_saved_and_the_automatic_one_stores_no_choice(api):
    client, headers, app = api

    saved = _choose(client, headers, "clean", "qwen3.5-9b")

    assert saved.status_code == 200
    assert saved.json()["chosen"]["clean"] == "qwen3.5-9b"
    assert saved.json()["defaults"]["clean"] == "qwen3.5-4b"  # what it would be, left alone
    assert saved.json()["chosen"]["main"] == "qwen3.5-4b"  # the chat's model wasn't touched
    assert _stored(app)["clean"] == "qwen3.5-9b"

    _choose(client, headers, "clean", "qwen3.5-4b")  # the automatic pick: follow it again
    assert _stored(app)["clean"] is None


def test_a_change_of_the_cleanup_model_unloads_the_idle_copies_of_the_chat_model(api):
    client, headers, app = api
    unloaded: list[str | None] = []

    async def record(role: str | None = None) -> list[str]:
        unloaded.append(role)
        return []

    app.state.runtime.unload_between_replies = record

    _choose(client, headers, "clean", "qwen3.5-9b")
    _choose(client, headers, "clean", "qwen3.5-9b")  # saved again as it is: nothing to unload

    # Cleanup's copies are leased as the chat role's ("main", "main@<model>").
    assert unloaded == ["main"]


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
    # With its zone: SQLite hands the time back without one, and a browser would read that
    # as local time and show the model installed hours off.
    at = datetime.fromisoformat(chat["installed"]["at"])
    assert at.utcoffset() == timedelta(0)
    with app.state.sessions() as session:
        recorded = session.get(DownloadRow, "qwen3.5-4b").updated_at
    assert at.replace(tzinfo=None) == recorded.replace(tzinfo=None)
    assert chat["used_by"] == ["main", "clean"]  # the chat, and cleanup of scanned pages
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
def test_what_is_not_a_model_folder_is_not_removed(
    api, tmp_path, request, make, name, status, says
):
    client, headers, app = api
    models_dir = app.state.downloads.models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.gguf").write_bytes(b"someone else's")
    if make == "link":
        request.getfixturevalue("symlinks")  # skipped where none can be made
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


# --- a newer revision in the catalog: Update --------------------------------------------------


def test_the_list_says_when_a_model_has_an_update_and_how_far_one_is(api):
    client, headers, app = api
    _downloaded(app, "qwen3.5-4b")
    assert _models(client, headers)["qwen3.5-4b"]["update_available"] is False

    app.state.downloads._save("qwen3.5-4b", revision="0" * 40)  # an older pin's download

    chat = _models(client, headers)["qwen3.5-4b"]
    assert (chat["update_available"], chat["update"]) == (True, None)
    assert chat["installed"]["revision"] == "0" * 40
    assert _models(client, headers)["qwen3.5-9b"]["update_available"] is False  # not on disk


def test_update_starts_in_the_background_and_answers_at_once(api):
    from lectern.downloads.manager import UpdateState

    client, headers, app = api
    _downloaded(app, "qwen3.5-4b")
    app.state.downloads._save("qwen3.5-4b", revision="0" * 40)
    started: list[str] = []

    def start_update(entry):  # noqa: ANN001, ANN202
        started.append(entry.id)
        state = UpdateState(entry.id, "updating", 5, entry.size)
        app.state.downloads._updates[entry.id] = state
        return state

    app.state.downloads.start_update = start_update

    answer = client.post("/api/models/qwen3.5-4b/update", headers=headers)

    assert answer.status_code == 202 and started == ["qwen3.5-4b"]
    assert answer.json()["status"] == "updating"
    update = _models(client, headers)["qwen3.5-4b"]["update"]
    assert (update["status"], update["bytes_done"]) == ("updating", 5)


@pytest.mark.parametrize(
    ("model_id", "status", "says"),
    [
        ("nope", 404, "No model 'nope'"),
        ("qwen3.5-4b", 409, "has no update"),  # on disk at the pinned revision
        ("qwen3.5-9b", 409, "has no update"),  # not on disk
    ],
)
def test_update_is_refused_when_there_is_nothing_to_update(api, model_id, status, says):
    client, headers, app = api
    _downloaded(app, "qwen3.5-4b")

    refused = client.post(f"/api/models/{model_id}/update", headers=headers)

    assert refused.status_code == status and says in refused.json()["detail"]


def test_updating_needs_the_token(api):
    client, _, app = api
    _downloaded(app, "qwen3.5-4b")
    app.state.downloads._save("qwen3.5-4b", revision="0" * 40)

    refused = client.post("/api/models/qwen3.5-4b/update", headers={"Origin": BASE})

    assert refused.status_code == 401
    assert app.state.downloads.update_state("qwen3.5-4b") is None


def test_the_swap_that_ends_an_update_goes_through_the_runtime(api):
    _, _, app = api
    # The app hands the downloads the runtime's own gate, in its waiting form: an update's
    # last step runs once no copy of the model is loaded, however long that takes.
    gate = app.state.downloads.exclusive
    assert gate.func == app.state.runtime.without_model
    assert gate.keywords == {"wait": True}
