from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.catalog import catalog

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
