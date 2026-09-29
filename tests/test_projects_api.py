from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.core.auth import User
from lectern.core.tables import UserRow

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def api(paths):
    app = create_app(paths)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    with TestClient(app, base_url=BASE) as client:
        yield client, headers, app


def test_create_list_get_and_rename(api, paths):
    client, headers, _ = api

    created = client.post("/api/projects", json={"name": "Lease archive 1998"}, headers=headers)
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Lease archive 1998"
    assert body["first_chat_id"] == 1
    assert Path(body["folder"]) == paths.projects_root("local") / "Lease archive 1998"

    untitled = client.post("/api/projects", json={}, headers=headers).json()
    assert untitled["name"] == "Untitled project"

    listed = client.get("/api/projects", headers=headers).json()
    assert [p["name"] for p in listed] == ["Lease archive 1998", "Untitled project"]

    renamed = client.patch(
        f"/api/projects/{untitled['id']}", json={"name": "Payment terms"}, headers=headers
    )
    assert renamed.json()["name"] == "Payment terms"
    assert client.get(f"/api/projects/{untitled['id']}", headers=headers).json()["name"] == (
        "Payment terms"
    )


def test_unknown_and_foreign_projects_are_not_found(api):
    client, headers, app = api
    with app.state.sessions.begin() as session:
        session.add(UserRow(id=2, name="other"))
    theirs = app.state.projects.create(User(id=2, name="other"), "Private")

    for project_id in (theirs.id, 999):
        assert client.get(f"/api/projects/{project_id}", headers=headers).status_code == 404
        response = client.patch(f"/api/projects/{project_id}", json={"name": "x"}, headers=headers)
        assert response.status_code == 404
    assert client.get("/api/projects", headers=headers).json() == []


def test_rename_needs_a_name(api):
    client, headers, _ = api
    project = client.post("/api/projects", json={}, headers=headers).json()
    response = client.patch(f"/api/projects/{project['id']}", json={"name": ""}, headers=headers)
    assert response.status_code == 422
