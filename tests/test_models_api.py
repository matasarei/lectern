"""Models & memory over HTTP: which model each role uses, and choosing another."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
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
