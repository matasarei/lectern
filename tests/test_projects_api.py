from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.core.auth import LOCAL_USER, User
from lectern.core.tables import UserRow
from lectern.projects import files
from lectern.projects.files import Origin
from lectern.projects.tables import MessageRow

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


def _project(client, headers) -> int:
    return client.post("/api/projects", json={"name": "Files"}, headers=headers).json()["id"]


def test_import_a_file_and_read_it_back(api):
    client, headers, _ = api
    pid = _project(client, headers)

    imported = client.put(
        f"/api/projects/{pid}/files", params={"name": "../notes.md"}, content=b"# Hi\nthere",
        headers=headers,
    )  # fmt: skip
    assert imported.status_code == 201
    body = imported.json()
    assert (body["name"], body["kind"], body["text"], body["from"]["kind"]) == (
        "notes.md", "imported", "indexed", "import"
    )  # fmt: skip

    content = client.get(f"/api/projects/{pid}/artifacts/{body['id']}/content", headers=headers)
    assert content.content == b"# Hi\nthere"
    assert content.headers["content-type"].startswith("text/markdown")
    assert content.headers["content-disposition"].startswith("inline")
    assert content.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name", ["page.html", "logo.svg", "run.js"])
def test_files_that_could_run_as_pages_are_downloaded_not_shown(api, name):
    client, headers, _ = api
    pid = _project(client, headers)
    row = client.put(
        f"/api/projects/{pid}/files", params={"name": name}, content=b"<script>alert(1)</script>",
        headers=headers,
    ).json()  # fmt: skip

    content = client.get(f"/api/projects/{pid}/artifacts/{row['id']}/content", headers=headers)

    assert content.headers["content-type"] == "application/octet-stream"
    assert content.headers["content-disposition"].startswith("attachment")
    assert content.headers["x-content-type-options"] == "nosniff"


def test_import_refuses_hidden_names_and_needs_a_name(api):
    client, headers, _ = api
    pid = _project(client, headers)
    hidden = client.put(
        f"/api/projects/{pid}/files", params={"name": ".env"}, content=b"SECRET=1", headers=headers
    )
    assert hidden.status_code == 400
    missing = client.put(f"/api/projects/{pid}/files", content=b"x", headers=headers)
    assert missing.status_code == 422


def test_files_of_unknown_projects_and_artifacts_are_not_found(api):
    client, headers, _ = api
    pid = _project(client, headers)
    assert client.put(
        "/api/projects/999/files", params={"name": "a.txt"}, content=b"x", headers=headers
    ).status_code == 404  # fmt: skip
    assert (
        client.get(f"/api/projects/{pid}/artifacts/5/content", headers=headers).status_code == 404
    )


def test_import_needs_the_token(api):
    client, _, _ = api
    response = client.put("/api/projects/1/files", params={"name": "a.txt"}, content=b"x")
    assert response.status_code == 401


def test_overview_lists_sessions_and_files_with_their_origin(api):
    client, headers, app = api
    pid = _project(client, headers)
    imported = client.put(
        f"/api/projects/{pid}/files", params={"name": "scan.txt"}, content=b"lease text",
        headers=headers,
    ).json()  # fmt: skip
    project = app.state.projects.get(LOCAL_USER, pid)
    with app.state.projects.db(project).begin() as session:
        files.write_text(
            project.root, session, owner_id=LOCAL_USER.id, name="reply.md", text="# Reply",
            origin=Origin("session", id=1),
        )  # fmt: skip

    items = client.get(f"/api/projects/{pid}/items", headers=headers).json()

    assert [(s["id"], s["kind"], s["title"], s["messages"]) for s in items["sessions"]] == [
        (1, "chat", "New chat", 0)
    ]
    by_name = {a["name"]: a for a in items["artifacts"]}
    assert by_name["scan.txt"]["from"] == {
        "kind": "import",
        "id": None,
        "note": None,
        "title": None,
    }
    assert by_name["reply.md"]["from"]["title"] == "New chat"
    assert by_name["reply.md"]["kind"] == "generated"
    assert imported["id"] == by_name["scan.txt"]["id"]


def test_project_search_returns_file_names_and_block_refs(api):
    client, headers, _ = api
    pid = _project(client, headers)
    client.put(
        f"/api/projects/{pid}/files", params={"name": "terms.md"},
        content="# Пеня\n0,5% за кожен день прострочення".encode(), headers=headers,
    )  # fmt: skip

    hits = client.get(f"/api/projects/{pid}/search", params={"q": "простроч"}, headers=headers)

    assert [(h["name"], h["ref"], h["heading"]) for h in hits.json()] == [
        ("terms.md", "#c1", "Пеня")
    ]
    assert "прострочення" in hits.json()[0]["snippet"]


def test_tabs_default_to_the_first_chat_and_are_remembered(api):
    client, headers, _ = api
    pid = _project(client, headers)

    assert client.get(f"/api/projects/{pid}/tabs", headers=headers).json() == {
        "open": [{"kind": "session", "id": 1}],
        "active": {"kind": "session", "id": 1},
    }
    row = client.put(
        f"/api/projects/{pid}/files", params={"name": "a.txt"}, content=b"a", headers=headers
    ).json()
    saved = {"open": [{"kind": "artifact", "id": row["id"]}], "active": None}
    assert client.put(f"/api/projects/{pid}/tabs", json=saved, headers=headers).status_code == 200
    assert client.get(f"/api/projects/{pid}/tabs", headers=headers).json() == saved


def test_tabs_for_things_that_are_gone_are_dropped(api):
    client, headers, app = api
    pid = _project(client, headers)
    row = client.put(
        f"/api/projects/{pid}/files", params={"name": "a.txt"}, content=b"a", headers=headers
    ).json()
    tab = {"kind": "artifact", "id": row["id"]}
    client.put(
        f"/api/projects/{pid}/tabs",
        json={"open": [tab, {"kind": "session", "id": 99}], "active": tab},
        headers=headers,
    )
    project = app.state.projects.get(LOCAL_USER, pid)
    with app.state.projects.db(project).begin() as session:
        files.trash(project.root, session, row["id"])

    assert client.get(f"/api/projects/{pid}/tabs", headers=headers).json() == {
        "open": [],
        "active": None,
    }


def test_tabs_are_validated(api):
    client, headers, _ = api
    pid = _project(client, headers)
    bad = {"open": [{"kind": "terminal", "id": 1}]}
    assert client.put(f"/api/projects/{pid}/tabs", json=bad, headers=headers).status_code == 422
    many = {"open": [{"kind": "session", "id": i} for i in range(31)]}
    assert client.put(f"/api/projects/{pid}/tabs", json=many, headers=headers).status_code == 422


def test_times_carry_their_zone_and_counts_leave_out_tool_steps(api):
    client, headers, app = api
    pid = _project(client, headers)
    project = app.state.projects.get(LOCAL_USER, pid)
    with app.state.projects.db(project).begin() as session:
        for role, content in [
            ("user", "Find the lease"),
            ("assistant", ""),  # a tool-call step
            ("tool", "lease.md · file"),
            ("assistant", "It's lease.md."),
        ]:
            session.add(MessageRow(session_id=1, role=role, content=content))

    items = client.get(f"/api/projects/{pid}/items", headers=headers).json()

    chat = items["sessions"][0]
    assert chat["messages"] == 2
    assert chat["updated_at"].endswith("+00:00")
    assert (
        client.get(f"/api/projects/{pid}", headers=headers).json()["created_at"].endswith("+00:00")
    )


def test_overview_lists_an_ocr_session_with_its_pages_and_status(api, tmp_path):
    from PIL import Image

    from lectern.modules.ocr import store as ocr_store
    from lectern.projects.tables import OcrSessionRow

    client, headers, app = api
    pid = client.post("/api/projects", json={"name": "Scans"}, headers=headers).json()["id"]
    project = app.state.projects.get(LOCAL_USER, pid)
    Image.new("L", (40, 60), 255).save(tmp_path / "scan.png")
    with app.state.projects.db(project).begin() as session:
        source = files.write_bytes(project.root, session, owner_id=LOCAL_USER.id,
                                   name="scan.png", data=(tmp_path / "scan.png").read_bytes(),
                                   origin=Origin("import")).id  # fmt: skip
    row, _ = ocr_store.create_session(project.root, app.state.projects.db(project),
                                      owner_id=LOCAL_USER.id, artifact_id=source)  # fmt: skip
    with app.state.projects.db(project).begin() as session:
        ocr = session.get(OcrSessionRow, row.id)
        ocr.status, ocr.pages_total = "running", 12  # left running by a restart

    sessions = client.get(f"/api/projects/{pid}/items", headers=headers).json()["sessions"]

    chat, scan = sessions
    assert chat["kind"] == "chat" and chat["ocr"] is None
    assert (scan["kind"], scan["title"]) == ("ocr", "scan.png")
    assert scan["ocr"] == {"pages": 12, "status": "paused", "folder": "scan",
                           "document_version": 0, "open_doubts": 0, "figures": 0,
                           "failed_pages": [], "clear": False}  # paused: not finished  # fmt: skip


def test_a_document_tab_is_kept_while_its_session_is_and_never_keeps_a_page(api):
    client, headers, _ = api
    pid = _project(client, headers)
    tab = {"kind": "document", "id": 1, "page": 3}  # the page says where to open it, once
    saved = {"open": [tab, {"kind": "document", "id": 99}], "active": tab}
    assert client.put(f"/api/projects/{pid}/tabs", json=saved, headers=headers).status_code == 200
    assert client.get(f"/api/projects/{pid}/tabs", headers=headers).json() == {
        "open": [{"kind": "document", "id": 1}],
        "active": {"kind": "document", "id": 1},
    }


# --- removing a project ------------------------------------------------------------------


def _remove(client, headers, project_id: int, typed: str):  # noqa: ANN001, ANN202
    return client.request(
        "DELETE", f"/api/projects/{project_id}", json={"name": typed}, headers=headers
    )


def test_removing_a_project_needs_its_name_typed_exactly(api):
    client, headers, _ = api
    project = client.post("/api/projects", json={"name": "Lease 1998"}, headers=headers).json()

    for typed in ("lease 1998", "Lease 1998 ", ""):
        refused = _remove(client, headers, project["id"], typed)
        assert refused.status_code == 422
        assert "exactly" in refused.json()["detail"]
    assert client.get(f"/api/projects/{project['id']}", headers=headers).status_code == 200


def test_a_removed_project_is_gone_and_its_folder_is_in_the_trash(api, paths):
    client, headers, _ = api
    project = client.post("/api/projects", json={"name": "Lease 1998"}, headers=headers).json()
    kept = client.post("/api/projects", json={"name": "Kept"}, headers=headers).json()
    pid = project["id"]
    client.put(f"/api/projects/{pid}/files?name=a.md", content=b"# A", headers=headers)

    removed = _remove(client, headers, pid, "Lease 1998")
    assert removed.status_code == 200 and removed.json() == {"removed": pid}

    assert [p["id"] for p in client.get("/api/projects", headers=headers).json()] == [kept["id"]]
    for path in (f"/api/projects/{pid}", f"/api/projects/{pid}/items"):
        assert client.get(path, headers=headers).status_code == 404
    assert _remove(client, headers, pid, "Lease 1998").status_code == 404
    assert not Path(project["folder"]).exists()
    (trashed,) = paths.projects_trash("local").iterdir()
    assert (trashed / "a.md").read_bytes() == b"# A"


def test_someone_elses_project_cant_be_removed(api):
    client, headers, app = api
    with app.state.sessions.begin() as session:
        session.add(UserRow(id=2, name="other"))
    theirs = app.state.projects.create(User(id=2, name="other"), "Private")

    for project_id in (theirs.id, 999):
        assert _remove(client, headers, project_id, "Private").status_code == 404
    assert theirs.root.is_dir()


def test_removing_a_project_needs_the_token(api):
    client, _, _ = api
    project = client.post(
        "/api/projects",
        json={"name": "Kept"},
        headers={"X-Lectern-Token": client.app.state.token, "Origin": BASE},
    ).json()
    refused = _remove(client, {"Origin": BASE}, project["id"], "Kept")
    assert refused.status_code == 401
    assert Path(project["folder"]).is_dir()
