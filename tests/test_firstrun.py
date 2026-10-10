from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.catalog import catalog
from lectern.core import settings

BASE = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def minimum_tier(monkeypatch):
    monkeypatch.setenv("LECTERN_TIER", "minimum")


def _client(paths):
    app = create_app(paths)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    return TestClient(app, base_url=BASE), headers, app


def _fake_downloaded(app, model_id):
    entry = catalog.load().get(model_id)
    for f in entry.files.all():
        path = app.state.downloads.file_path(entry, f)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")


def test_starts_at_hardware_with_the_tiers_chat_model(paths):
    client, headers, _ = _client(paths)

    body = client.get("/api/first-run", headers=headers).json()

    assert body["step"] == "hardware"
    assert body["chat_model"]["id"] == "qwen3.5-4b"
    assert body["chat_model"]["licence"] == "Apache-2.0"
    assert body["can_finish"] is False


def test_it_is_the_chat_model_chosen_in_models_and_memory_that_setup_waits_for(paths):
    client, headers, app = _client(paths)
    app.state.runtime.engine = FakeEngine(installed=True)
    chosen = {**settings.MODEL_DEFAULTS, "main": "qwen3.5-9b"}
    settings.put(app.state.sessions, settings.MODELS_KEY, chosen)

    assert client.get("/api/first-run", headers=headers).json()["chat_model"]["id"] == "qwen3.5-9b"

    client.post("/api/first-run", json={"to": "download"}, headers=headers)
    _fake_downloaded(app, "qwen3.5-4b")  # the recommended one being there isn't enough
    assert client.post("/api/first-run", json={"to": "done"}, headers=headers).status_code == 409
    _fake_downloaded(app, "qwen3.5-9b")
    assert client.post("/api/first-run", json={"to": "done"}, headers=headers).status_code == 200


def test_moves_forward_one_step_at_a_time(paths):
    client, headers, _ = _client(paths)

    skip = client.post("/api/first-run", json={"to": "done"}, headers=headers)
    assert skip.status_code == 409

    forward = client.post("/api/first-run", json={"to": "download"}, headers=headers)
    assert forward.status_code == 200
    assert forward.json()["step"] == "download"


def test_cannot_finish_without_the_chat_model(paths):
    client, headers, _ = _client(paths)
    client.post("/api/first-run", json={"to": "download"}, headers=headers)

    response = client.post("/api/first-run", json={"to": "done"}, headers=headers)

    assert response.status_code == 409
    assert "chat model" in response.json()["detail"]


def test_finishes_once_the_chat_model_is_there(paths):
    client, headers, app = _client(paths)
    app.state.runtime.engine = FakeEngine(installed=True)  # setup downloads it too now
    client.post("/api/first-run", json={"to": "download"}, headers=headers)
    _fake_downloaded(app, "qwen3.5-4b")

    response = client.post("/api/first-run", json={"to": "done"}, headers=headers)

    assert response.json()["step"] == "done"
    assert response.json()["can_finish"] is True


def test_step_survives_a_restart(paths):
    client, headers, _ = _client(paths)
    client.post("/api/first-run", json={"to": "download"}, headers=headers)

    client2, headers2, _ = _client(paths)

    assert client2.get("/api/first-run", headers=headers2).json()["step"] == "download"


class FakeEngine:
    """The llama.cpp build, as first-run sees it, without downloading anything."""

    variant = "linux-cuda12"

    def __init__(self, *, installed: bool = False, fail: str | None = None) -> None:
        self._installed = installed
        self.fail = fail
        self.installs = 0

    def installed(self) -> bool:
        return self._installed

    def size(self) -> int:
        return 0 if self._installed else 400

    async def install(self) -> None:
        self.installs += 1
        if self.fail:
            raise RuntimeError(self.fail)
        self._installed = True


def _with_engine(paths, engine):  # noqa: ANN001, ANN202
    app = create_app(paths)
    app.state.runtime.engine = engine
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    return app, headers


def test_first_run_downloads_the_engine_once_and_reports_it(paths):
    engine = FakeEngine()
    app, headers = _with_engine(paths, engine)

    with TestClient(app, base_url=BASE) as client:
        before = client.get("/api/first-run", headers=headers).json()["engine"]
        client.post("/api/first-run/engine", headers=headers)
        client.post("/api/first-run/engine", headers=headers)  # a second click: not twice
        after = client.get("/api/first-run", headers=headers).json()["engine"]

    assert before == {"variant": "linux-cuda12", "status": "none", "bytes_done": 0,
                      "bytes_total": 400, "error": None}  # fmt: skip
    assert after["status"] == "done"
    assert engine.installs == 1


def test_a_failed_engine_download_says_why_and_can_be_tried_again(paths):
    engine = FakeEngine(fail="no network")
    app, headers = _with_engine(paths, engine)

    with TestClient(app, base_url=BASE) as client:
        client.post("/api/first-run/engine", headers=headers)
        failed = client.get("/api/first-run", headers=headers).json()["engine"]
        engine.fail = None
        client.post("/api/first-run/engine", headers=headers)
        again = client.get("/api/first-run", headers=headers).json()["engine"]

    assert (failed["status"], failed["error"]) == ("failed", "no network")
    assert again["status"] == "done" and engine.installs == 2


def test_cannot_finish_without_the_engine(paths):
    app, headers = _with_engine(paths, FakeEngine())
    client = TestClient(app, base_url=BASE)
    client.post("/api/first-run", json={"to": "download"}, headers=headers)
    _fake_downloaded(app, "qwen3.5-4b")

    response = client.post("/api/first-run", json={"to": "done"}, headers=headers)

    assert response.status_code == 409 and "engine" in response.json()["detail"]
    assert client.get("/api/first-run", headers=headers).json()["can_finish"] is False
