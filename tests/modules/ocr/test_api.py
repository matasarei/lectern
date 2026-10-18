from __future__ import annotations

import json
import time

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lectern.app import create_app
from lectern.core.auth import LOCAL_USER, User
from lectern.core.tables import UserRow
from lectern.projects import files
from lectern.projects.tables import OcrSessionRow
from lectern.runtime.supervisor import NotAvailable

from ...projects.conftest import write_scan_pdf
from .test_jobs import FakeServer, _runtime

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def api(paths, tmp_path):
    server = FakeServer()
    runtime, _ = _runtime(server)
    app = create_app(paths, runtime=runtime)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    project = app.state.projects.create(LOCAL_USER, "Scans")
    pdf = write_scan_pdf(tmp_path / "lease-scan.pdf", pages=3).read_bytes()
    with app.state.projects.db(project).begin() as session:
        ids = {
            name: files.write_bytes(project.root, session, owner_id=LOCAL_USER.id, name=name,
                                    data=data, origin=files.Origin("import")).id
            for name, data in (("lease-scan.pdf", pdf), ("notes.txt", b"hello"))
        }  # fmt: skip
    with TestClient(app, base_url=BASE) as client:
        yield client, headers, app, project, ids, server


def _start(client, headers, project, artifact_id: int) -> int:  # noqa: ANN001
    body = {"artifact_id": artifact_id}
    return client.post(f"/api/projects/{project.id}/ocr", json=body, headers=headers).json()["id"]


def _until_done(client, headers, url: str) -> dict:  # noqa: ANN001
    for _ in range(400):
        body = client.get(url, headers=headers).json()
        if body["status"] in ("done", "failed", "paused"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"the job never finished: {body}")


def test_a_pdf_becomes_an_ocr_session_whose_pages_are_read(api):
    client, headers, _, project, ids, _ = api

    created = client.post(f"/api/projects/{project.id}/ocr",
                          json={"artifact_id": ids["lease-scan.pdf"]}, headers=headers)  # fmt: skip

    assert created.status_code == 201
    session = created.json()
    assert (session["kind"], session["title"], session["folder"]) == (
        "ocr", "lease-scan.pdf", "lease-scan"
    )  # fmt: skip
    url = f"/api/projects/{project.id}/ocr/{session['id']}"
    done = _until_done(client, headers, url)
    assert done["status"] == "done" and done["pages_total"] == 3
    assert [p["status"] for p in done["pages"]] == ["done"] * 3
    image = client.get(f"{url}/pages/2/image", headers=headers)
    assert image.headers["content-type"] == "image/png" and image.content[:4] == b"\x89PNG"
    text = client.get(f"{url}/pages/2/text", headers=headers).json()
    assert text["text"] == "page 2 read by ocr-model" and text["model_id"] == "ocr-model"
    # The fake text runs on without a full stop, so assembly joins page 2 onto page 1.
    assert text["cleaned"].startswith("{#b1.1 p=1→2} page 1 read by ocr-model page 2 read")
    items = client.get(f"/api/projects/{project.id}/items", headers=headers).json()
    assert {"id": session["id"], "kind": "ocr"}.items() <= next(
        s for s in items["sessions"] if s["id"] == session["id"]
    ).items()


def test_a_note_and_handwritten_are_kept_and_a_page_can_be_read_again(api):
    client, headers, _, project, ids, server = api
    sid = _start(client, headers, project, ids["lease-scan.pdf"])
    url = f"/api/projects/{project.id}/ocr/{sid}"
    _until_done(client, headers, url)

    note = "Печатка орендодавця, не орендаря."
    changed = client.patch(f"{url}/pages/1", json={"note": note, "handwritten": True},
                           headers=headers)  # fmt: skip
    rerun = client.post(f"{url}/pages/3/rerun", json={"model_id": "chandra-ocr-2"}, headers=headers)
    done = _until_done(client, headers, url)

    assert changed.json()["note"] == "Печатка орендодавця, не орендаря."
    assert done["pages"][0]["note"] == "Печатка орендодавця, не орендаря."
    assert done["pages"][0]["handwritten"] is True
    assert rerun.status_code == 202
    assert done["pages"][2]["model_id"] == "chandra-ocr-2"
    assert server.requests[-1] == "chandra-ocr-2"
    bad = client.post(f"{url}/pages/3/rerun", json={"model_id": "qwen3.5-4b"}, headers=headers)
    assert bad.status_code == 400 and "isn't an OCR model" in bad.json()["detail"]


def test_pause_and_resume_answer_by_state(api):
    client, headers, _, project, ids, _ = api
    sid = _start(client, headers, project, ids["lease-scan.pdf"])
    url = f"/api/projects/{project.id}/ocr/{sid}"
    _until_done(client, headers, url)

    not_running = client.post(f"{url}/pause", headers=headers)
    resumed = client.post(f"{url}/resume", headers=headers)  # a done session may run again

    assert not_running.status_code == 409 and "isn't running" in not_running.json()["detail"]
    assert resumed.status_code == 200
    assert _until_done(client, headers, url)["status"] == "done"


def test_start_now_lets_the_job_skip_the_wait_for_the_chat(api, monkeypatch):
    client, headers, app, project, ids, _ = api
    sid = _start(client, headers, project, ids["lease-scan.pdf"])
    calls: list[bool] = []

    async def skip_hold() -> None:
        calls.append(True)

    monkeypatch.setattr(app.state.runtime, "skip_hold", skip_hold)
    url = f"/api/projects/{project.id}/ocr/{sid}"

    started = client.post(f"{url}/start-now", headers=headers)
    elsewhere = client.post(f"/api/projects/{project.id}/ocr/99999/start-now", headers=headers)

    assert started.status_code == 200 and calls == [True]
    assert elsewhere.status_code == 404


def test_a_chat_on_mlx_without_its_usual_model_starts_no_session(api):
    client, headers, app, project, ids, _ = api
    app.state.runtime.cleanup_key = lambda: "main@qwen3.5-4b"  # cleanup asks by name
    url = f"/api/projects/{project.id}/ocr"

    refused = client.post(url, json={"artifact_id": ids["lease-scan.pdf"]}, headers=headers)

    assert refused.status_code == 409
    assert "needs Qwen3.5 4B (2.7 GB)" in refused.json()["detail"]
    assert "Models & memory" in refused.json()["detail"]
    with app.state.projects.db(project)() as session:
        assert session.scalars(select(OcrSessionRow)).all() == []  # nothing was opened


def test_without_the_model_that_reads_pages_no_session_is_started(api, monkeypatch):
    """It would start, then stop at its first page: the tab says what to download first."""
    client, headers, app, project, ids, _ = api
    resolve = app.state.runtime.resolve

    def without_ocr(key: str):  # noqa: ANN202
        if key == "ocr":
            raise NotAvailable("PaddleOCR-VL isn't downloaded yet. Download it in Models & memory.")
        return resolve(key)

    monkeypatch.setattr(app.state.runtime, "resolve", without_ocr)

    refused = client.post(f"/api/projects/{project.id}/ocr",
                          json={"artifact_id": ids["lease-scan.pdf"]}, headers=headers)  # fmt: skip

    assert refused.status_code == 409
    assert "PaddleOCR-VL isn't downloaded yet" in refused.json()["detail"]
    with app.state.projects.db(project)() as session:
        assert session.scalars(select(OcrSessionRow)).all() == []  # nothing was opened


def test_a_file_that_isnt_a_scan_says_so_even_when_the_cleanup_model_is_missing(api):
    """The file is checked first: a missing model doesn't matter for a file OCR can't read."""
    client, headers, app, project, ids, _ = api
    app.state.runtime.cleanup_key = lambda: "main@qwen3.5-4b"  # its model isn't on disk
    refused = client.post(f"/api/projects/{project.id}/ocr",
                          json={"artifact_id": ids["notes.txt"]}, headers=headers)  # fmt: skip
    assert refused.status_code == 400 and "isn't a PDF or an image" in refused.json()["detail"]


def test_only_a_pdf_or_image_starts_a_session(api):
    client, headers, _, project, ids, _ = api
    refused = client.post(f"/api/projects/{project.id}/ocr",
                          json={"artifact_id": ids["notes.txt"]}, headers=headers)  # fmt: skip
    missing = client.post(f"/api/projects/{project.id}/ocr", json={"artifact_id": 999},
                          headers=headers)  # fmt: skip
    assert refused.status_code == 400 and "isn't a PDF or an image" in refused.json()["detail"]
    assert missing.status_code == 400


def test_other_peoples_projects_chats_and_pages_that_dont_exist_are_not_found(api):
    client, headers, app, project, ids, _ = api
    sid = _start(client, headers, project, ids["lease-scan.pdf"])
    _until_done(client, headers, f"/api/projects/{project.id}/ocr/{sid}")
    with app.state.sessions.begin() as session:
        session.add(UserRow(id=2, name="other"))
    theirs = app.state.projects.create(User(id=2, name="other"), "Private")
    chat_id = 1  # the project's first chat

    for url in (
        f"/api/projects/{theirs.id}/ocr/{sid}",
        f"/api/projects/{project.id}/ocr/{chat_id}",  # a chat isn't an OCR session
        f"/api/projects/{project.id}/ocr/999",
        f"/api/projects/{project.id}/ocr/{sid}/pages/0/image",
        f"/api/projects/{project.id}/ocr/{sid}/pages/4/text",
    ):
        assert client.get(url, headers=headers).status_code == 404, url
    patched = client.patch(f"/api/projects/{project.id}/ocr/{sid}/pages/9", json={"note": "x"},
                           headers=headers)  # fmt: skip
    assert patched.status_code == 404
    assert client.get(f"/api/projects/{project.id}/ocr/{sid}").status_code == 401  # no token


def test_the_models_list_says_which_are_downloaded(api):
    client, headers, _, project, _, _ = api
    models = client.get(f"/api/projects/{project.id}/ocr/models", headers=headers).json()
    assert {m["id"]: m["role"] for m in models} == {
        "paddleocr-vl-1.6": "ocr",
        "qwen3-vl-4b": "ocr.hand",
        "qwen3-vl-8b": "ocr.hand",
        "chandra-ocr-2": "ocr.hand",
    }
    assert all(m["downloaded"] is False for m in models)  # a fresh Lectern home


def test_the_events_stream_follows_the_job(paths, tmp_path, free_port, live_server):
    runtime, _ = _runtime(FakeServer())
    app = create_app(paths, port=free_port, runtime=runtime)
    project = app.state.projects.create(LOCAL_USER, "Scans")
    pdf = write_scan_pdf(tmp_path / "scan.pdf", pages=2).read_bytes()
    with app.state.projects.db(project).begin() as session:
        artifact = files.write_bytes(
            project.root, session, owner_id=LOCAL_USER.id, name="scan.pdf", data=pdf,
            origin=files.Origin("import"),
        )  # fmt: skip

    with live_server(app, free_port) as base, httpx2.Client(base_url=base, timeout=10) as client:
        headers = {"X-Lectern-Token": app.state.token, "Origin": base}
        sid = _start(client, headers, project, artifact.id)
        seen = []
        with client.stream("GET", f"/api/projects/{project.id}/ocr/{sid}/events",
                           headers=headers) as stream:  # fmt: skip
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    seen.append(json.loads(line.removeprefix("data: ")))
                    if seen[-1] == {**seen[-1], "type": "session", "status": "done"}:
                        break

    kinds = [e["type"] for e in seen]
    assert "phase" in kinds and kinds.count("page") >= 2
    assert seen[-1]["status"] == "done"


def test_a_finished_session_reports_and_serves_its_document(api):
    client, headers, _, project, ids, _ = api
    sid = _start(client, headers, project, ids["lease-scan.pdf"])
    url = f"/api/projects/{project.id}/ocr/{sid}"
    done = _until_done(client, headers, url)

    assert done["document"]["version"] == 1 and done["document"]["open_doubts"] == 0
    document = client.get(f"{url}/document", headers=headers).json()
    assert document["schema"] == "lectern.document/1" and len(document["pages"]) == 3
    items = client.get(f"/api/projects/{project.id}/items", headers=headers).json()
    names = {a["id"]: a["name"] for a in items["artifacts"]}
    assert names[done["document"]["artifact_id"]] == "lease-scan/document.md"


def test_a_session_without_a_document_says_so(api, monkeypatch):
    client, headers, app, project, ids, _ = api
    from lectern.modules.ocr import store as ocr_store

    db = app.state.projects.db(project)
    source = ids["lease-scan.pdf"]
    row, _ = ocr_store.create_session(project.root, db, owner_id=LOCAL_USER.id, artifact_id=source)
    url = f"/api/projects/{project.id}/ocr/{row.id}"
    assert client.get(url, headers=headers).json()["document"] is None
    missing = client.get(f"{url}/document", headers=headers)
    assert missing.status_code == 404 and "no Document yet" in missing.json()["detail"]


def _session_with_doubts(api) -> tuple[str, int]:  # noqa: ANN001
    from .test_jobs import _with_a_doubt

    client, headers, _, project, ids, server = api
    server.clean = _with_a_doubt  # each page: "… до 1? числа", one doubt
    sid = _start(client, headers, project, ids["lease-scan.pdf"])
    url = f"/api/projects/{project.id}/ocr/{sid}"
    _until_done(client, headers, url)
    return url, sid


def test_answering_the_last_open_question_tells_whoever_waits_that_its_finished(api):
    client, headers, app, *_ = api
    url, sid = _session_with_doubts(api)
    heard: list[str] = []

    async def on_status(project, session_id, status, notice):  # noqa: ANN001, ANN202
        heard.append(notice)

    app.state.ocr_jobs.on_status = on_status
    for doubt in ("d1.1", "d2.1"):
        client.put(f"{url}/doubts/{doubt}", json={"value": "15", "action": "accept"},
                   headers=headers)  # fmt: skip
    assert not any("finished ·" in n for n in heard)  # two of three answered: not yet

    client.put(f"{url}/doubts/d3.1", json={"value": "15", "action": "accept"}, headers=headers)
    assert heard[-1] == "OCR · lease-scan.pdf finished · 3 pages."


def test_an_answer_is_kept_even_when_telling_whoever_waits_fails(api):
    client, headers, app, *_ = api
    url, _ = _session_with_doubts(api)

    async def broken(project, session_id):  # noqa: ANN001, ANN202
        raise RuntimeError("the summary couldn't be read")

    app.state.ocr_jobs.tell = broken
    answered = client.put(f"{url}/doubts/d1.1", json={"value": "15", "action": "accept"},
                          headers=headers)  # fmt: skip
    assert answered.status_code == 200 and answered.json()["open_doubts"] == 2


def test_a_doubt_is_accepted_edited_and_reopened_each_a_new_version(api):
    client, headers, *_ = api
    url, _ = _session_with_doubts(api)

    accepted = client.put(f"{url}/doubts/d1.1", json={"value": "15", "action": "accept"},
                          headers=headers)  # fmt: skip
    assert accepted.status_code == 200
    assert accepted.json() == {"version": 2, "artifact_id": accepted.json()["artifact_id"],
                               "open_doubts": 2}  # fmt: skip
    edited = client.put(f"{url}/doubts/d1.1", json={"value": "18", "action": "edit"},
                        headers=headers).json()  # fmt: skip
    assert edited["version"] == 3
    document = client.get(f"{url}/document", headers=headers).json()
    doubts = {d["id"]: d for b in document["blocks"] for d in b["doubts"]}
    assert (doubts["d1.1"]["status"], doubts["d1.1"]["value"]) == ("resolved", "18")
    assert document["unplaced"] == []

    reopened = client.delete(f"{url}/doubts/d1.1", headers=headers)
    assert reopened.status_code == 200 and reopened.json()["open_doubts"] == 3


def test_cleaning_one_page_again_starts_the_job_for_that_page(api):
    client, headers, _, _, _, server = api
    url, _ = _session_with_doubts(api)
    before = len(server.cleanups)

    started = client.post(f"{url}/pages/2/clean", headers=headers)
    assert started.status_code == 202 and started.json()["clean_status"] == "pending"
    done = _until_done(client, headers, url)
    assert len(server.cleanups) == before + 1 and done["document"]["version"] == 2


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("put", "doubts/d9.9", {"value": "x", "action": "edit"}, 404),  # no such doubt
        ("delete", "doubts/d1.1", None, 404),  # open: nothing to reopen
        ("put", "doubts/d1.1", {"value": "", "action": "edit"}, 422),
        ("put", "doubts/d1.1", {"value": "x" * 201, "action": "edit"}, 422),
        ("put", "doubts/d1.1", {"value": "x", "action": "skip"}, 422),
        ("post", "pages/9/clean", None, 404),
    ],
)
def test_a_decision_on_nothing_or_out_of_bounds_is_refused(api, method, path, body, status):
    client, headers, *_ = api
    url, _ = _session_with_doubts(api)
    kwargs = {"json": body} if body is not None else {}
    answer = getattr(client, method)(f"{url}/{path}", headers=headers, **kwargs)
    assert answer.status_code == status, answer.text


def test_someone_elses_session_takes_no_decisions(api):
    client, headers, app, project, _, _ = api
    _, sid = _session_with_doubts(api)
    with app.state.sessions.begin() as session:
        session.add(UserRow(id=2, name="other"))
    theirs = app.state.projects.create(User(id=2, name="other"), "Private")
    for url in (f"/api/projects/{theirs.id}/ocr/{sid}", f"/api/projects/{project.id}/ocr/1"):
        put = client.put(f"{url}/doubts/d1.1", json={"value": "x", "action": "edit"},
                         headers=headers)  # fmt: skip
        assert put.status_code == 404, url
        assert client.delete(f"{url}/doubts/d1.1", headers=headers).status_code == 404
        assert client.post(f"{url}/pages/1/clean", headers=headers).status_code == 404


def test_overview_learns_an_ocr_sessions_folder_and_its_documents_counts(api):
    client, headers, _, project, _, _ = api
    _, sid = _session_with_doubts(api)
    items = client.get(f"/api/projects/{project.id}/items", headers=headers).json()
    (scan,) = [s for s in items["sessions"] if s["id"] == sid]
    # done, but 3 questions open: not clear
    assert scan["ocr"] == {"pages": 3, "status": "done", "folder": "lease-scan",
                           "document_version": 1, "open_doubts": 3, "figures": 0,
                           "failed_pages": [], "clear": False}  # fmt: skip
