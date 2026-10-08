"""POST /api/projects/{id}/export: the Export buttons, through export.run's writer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.core.auth import User
from lectern.core.tables import UserRow
from lectern.projects import files
from lectern.projects.files import Origin

from .agent.fake_model import FakeHandle, make_runtime
from .documents.fixtures import lease

BASE = "http://127.0.0.1:8765"
LOCAL = User(id=1, name="local")


@pytest.fixture
def api(paths):
    app = create_app(paths, runtime=make_runtime(FakeHandle([])))
    client = TestClient(app, base_url=BASE)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    pid = client.post("/api/projects", json={"name": "Scans"}, headers=headers).json()["id"]
    return client, headers, app, pid


def _add(app, pid: int, name: str, text: str, *, folder: str = "", origin=None) -> int:  # noqa: ANN001
    project = app.state.projects.get(LOCAL, pid)
    with app.state.projects.db(project).begin() as session:
        row = files.write_text(project.root, session, owner_id=1, name=name, text=text,
                               origin=origin or Origin("import"), folder=folder)  # fmt: skip
        return row.id


def _session_document(app, pid: int) -> int:  # noqa: ANN001
    """An OCR session's output: Lease 1998/document.json beside its document.md."""
    project = app.state.projects.get(LOCAL, pid)
    (project.root / "Lease 1998").mkdir()
    (project.root / "Lease 1998" / "document.json").write_text(lease().to_json(), encoding="utf-8")
    return _add(app, pid, "document.md", "{#b3.1 p=3} …", folder="Lease 1998",
                origin=Origin("session", 9))  # fmt: skip


def test_the_formats_are_listed_with_what_they_keep(api):
    client, headers, _, _ = api
    formats = client.get("/api/exporters", headers=headers).json()
    assert [f["format"] for f in formats] == ["docx", "md", "odt"]
    docx = next(f for f in formats if f["format"] == "docx")
    assert docx["capabilities"] == ["tables", "comments"]


def test_a_sessions_document_exports_to_a_new_file_pointing_at_it(api):
    client, headers, app, pid = api
    source = _session_document(app, pid)
    url = f"/api/projects/{pid}/export"

    first = client.post(url, json={"artifact_id": source, "format": "docx"}, headers=headers)
    again = client.post(url, json={"artifact_id": source, "format": "DOCX"}, headers=headers)

    assert first.status_code == 201
    names = [first.json()["name"], again.json()["name"]]
    assert names == ["Lease 1998.docx", "Lease 1998 (2).docx"]
    assert first.json()["from"] == {"kind": "artifact", "id": source, "note": None,
                                    "title": "Lease 1998/document.md"}  # fmt: skip
    root = app.state.projects.get(LOCAL, pid).root
    assert (root / "Lease 1998.docx").read_bytes()[:2] == b"PK"


def test_what_a_format_loses_comes_back_first_and_goes_ahead_once_accepted(api):
    client, headers, app, pid = api
    source = _session_document(app, pid)
    url = f"/api/projects/{pid}/export"
    asked = {"artifact_id": source, "format": "md", "open_doubts": "comment"}

    refused = client.post(url, json=asked, headers=headers)
    accepted = client.post(url, json={**asked, "accept_loss": True}, headers=headers)

    assert refused.status_code == 409
    assert refused.json()["lost"] == ["comments: the open questions become inline marks"]
    assert accepted.status_code == 201 and accepted.json()["name"] == "Lease 1998.md"


def test_a_bad_format_or_a_file_that_cant_be_exported_is_a_400(api):
    client, headers, app, pid = api
    letter = _add(app, pid, "reply.md", "# Reply\n")
    scan = _add(app, pid, "scan.png", "x")
    url = f"/api/projects/{pid}/export"

    bad = client.post(url, json={"artifact_id": letter, "format": "pdf"}, headers=headers)
    image = client.post(url, json={"artifact_id": scan, "format": "odt"}, headers=headers)

    assert (bad.status_code, bad.json()["detail"]) == (400, "No pdf exporter is installed.")
    assert image.status_code == 400 and "isn't a Document" in image.json()["detail"]


def test_someone_elses_file_or_project_isnt_found(api):
    client, headers, app, pid = api
    with app.state.sessions.begin() as session:
        session.add(UserRow(id=2, name="other"))
    theirs = app.state.projects.create(User(id=2, name="other"), "Theirs")
    with app.state.projects.db(theirs).begin() as session:
        row = files.write_text(theirs.root, session, owner_id=2, name="their.md", text="# Theirs\n",
                               origin=Origin("import"))  # fmt: skip
        their_file = row.id
    mine = _add(app, pid, "mine.md", "# Mine\n")

    in_their_project = client.post(f"/api/projects/{theirs.id}/export",
                                   json={"artifact_id": their_file, "format": "md"},
                                   headers=headers)  # fmt: skip
    missing = client.post(f"/api/projects/{pid}/export",
                          json={"artifact_id": mine + 100, "format": "md"},
                          headers=headers)  # fmt: skip

    assert in_their_project.status_code == 404
    assert missing.status_code == 404
