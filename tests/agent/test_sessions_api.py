from __future__ import annotations

import asyncio
import json
import threading
import time

import anyio.from_thread
import httpx2
import pytest
from fastapi.testclient import TestClient

from lectern.agent import state
from lectern.api import sessions as sessions_api
from lectern.app import create_app
from lectern.core.auth import User
from lectern.core.tables import UserRow
from lectern.projects.tables import MessageRow

from .fake_model import FakeHandle, make_runtime, text_reply, tool_reply

BASE = "http://127.0.0.1:8765"


def _app(paths, replies, **kwargs):
    handle = FakeHandle(replies, **kwargs)
    return create_app(paths, runtime=make_runtime(handle)), handle


@pytest.fixture
def api(paths):
    def make(replies):
        app, handle = _app(paths, replies)
        client = TestClient(app, base_url=BASE)
        headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
        pid = client.post("/api/projects", json={"name": "Chat"}, headers=headers).json()["id"]
        return client, headers, app, pid, handle

    return make


def _wait_idle(client, headers, pid, sid=1, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chat = client.get(f"/api/projects/{pid}/sessions/{sid}", headers=headers).json()
        if chat["state"] == "idle":
            return chat
        time.sleep(0.02)
    raise AssertionError("the turn didn't finish")


def test_send_runs_a_turn_and_the_transcript_has_it(api):
    client, headers, _, pid, _ = api([text_reply("Hello", " there")])
    with client:
        sent = client.post(
            f"/api/projects/{pid}/sessions/1/messages", json={"text": "Hi"}, headers=headers
        )
        assert sent.status_code == 202 and sent.json() == {"message_id": 1}
        chat = _wait_idle(client, headers, pid)

    assert chat["title"] == "Hi"
    assert [(m["role"], m["content"]) for m in chat["messages"]] == [
        ("user", "Hi"),
        ("assistant", "Hello there"),
    ]
    assert chat["pending"] == []


def test_a_chat_that_reads_idle_has_the_reply_its_turn_ended_with(paths, monkeypatch):
    hold = threading.Event()
    app, _ = _app(paths, [text_reply("Hello")], hold=hold)
    client = TestClient(app, base_url=BASE)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    manager = app.state.turns

    class EndsWhenAsked:
        """The turn ends at the moment the route asks for its state."""

        def state(self, key: tuple[int, int]) -> str:
            hold.set()
            anyio.from_thread.run(manager.wait, key)
            return manager.state(key)

    with client:
        pid = client.post("/api/projects", json={}, headers=headers).json()["id"]
        client.post(
            f"/api/projects/{pid}/sessions/1/messages", json={"text": "Hi"}, headers=headers
        )
        monkeypatch.setattr(sessions_api, "turns", lambda request: EndsWhenAsked())
        chat = client.get(f"/api/projects/{pid}/sessions/1", headers=headers).json()

    assert chat["state"] == "idle"
    assert [(m["role"], m["content"]) for m in chat["messages"]] == [
        ("user", "Hi"),
        ("assistant", "Hello"),
    ]


def test_new_chat_and_retitle(api):
    client, headers, _, pid, _ = api([])
    created = client.post(f"/api/projects/{pid}/sessions", json={}, headers=headers).json()
    assert (created["id"], created["title"], created["state"]) == (2, "New chat", "idle")
    renamed = client.patch(
        f"/api/projects/{pid}/sessions/2", json={"title": "Payment terms"}, headers=headers
    ).json()
    assert renamed["title"] == "Payment terms"
    items = client.get(f"/api/projects/{pid}/items", headers=headers).json()
    assert [s["title"] for s in items["sessions"]] == ["New chat", "Payment terms"]


def test_a_page_attaching_mid_turn_gets_the_turn_so_far_then_the_rest_live(
    paths, free_port, live_server
):
    hold = threading.Event()
    port = free_port
    handle = FakeHandle([text_reply("Hel", "lo")], hold=hold)
    app = create_app(paths, port=port, runtime=make_runtime(handle))

    with live_server(app, port) as base, httpx2.Client(base_url=base, timeout=10) as client:
        headers = {"X-Lectern-Token": app.state.token, "Origin": base}
        pid = client.post("/api/projects", json={}, headers=headers).json()["id"]
        client.post(
            f"/api/projects/{pid}/sessions/1/messages", json={"text": "hi"}, headers=headers
        )
        deadline = time.monotonic() + 5
        while not handle.requests:  # the model has been asked, and is holding its reply
            assert time.monotonic() < deadline, "the model was never asked"
            time.sleep(0.01)
        seen = []
        with client.stream(
            "GET", f"/api/projects/{pid}/sessions/1/events", headers=headers
        ) as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                seen.append(json.loads(line.removeprefix("data: ")))
                if seen[-1]["type"] == "usage":
                    hold.set()  # everything so far has been replayed; now let it answer
                if seen[-1]["type"] == "turn" and seen[-1]["status"] != "running":
                    break

    types = [e["type"] for e in seen]
    assert types[:4] == ["turn", "model", "usage", "replayed"]  # the replay, then its end
    assert "".join(e["text"] for e in seen if e["type"] == "delta") == "Hello"  # live
    assert types[-2:] == ["done", "turn"]
    numbered = [e["seq"] for e in seen if e["type"] != "replayed"]
    assert numbered == list(range(1, len(numbered) + 1))


def test_a_second_message_while_answering_is_refused(paths):
    hold = asyncio.Event()
    app, _ = _app(paths, [text_reply("slow")], hold=hold)
    client = TestClient(app, base_url=BASE)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    with client:
        pid = client.post("/api/projects", json={}, headers=headers).json()["id"]
        url = f"/api/projects/{pid}/sessions/1/messages"
        assert client.post(url, json={"text": "one"}, headers=headers).status_code == 202
        busy = client.post(url, json={"text": "two"}, headers=headers)
        assert busy.status_code == 409 and "Stop" in busy.json()["detail"]
        stopped = client.post(f"/api/projects/{pid}/sessions/1/stop", headers=headers)
        assert stopped.json() == {"stopped": True}
        assert client.post(f"/api/projects/{pid}/sessions/1/stop", headers=headers).json() == {
            "stopped": False
        }


def test_a_card_is_answered_through_the_api(api):
    client, headers, app, pid, _ = api(
        [tool_reply("artifact_trash", {"name": "old.md"}), text_reply("Done.")]
    )
    with client:
        client.put(
            f"/api/projects/{pid}/files", params={"name": "old.md"}, content=b"x", headers=headers
        )
        client.post(
            f"/api/projects/{pid}/sessions/1/messages", json={"text": "delete old.md"},
            headers=headers,
        )  # fmt: skip
        for _ in range(250):
            chat = client.get(f"/api/projects/{pid}/sessions/1", headers=headers).json()
            if chat["pending"]:
                break
            time.sleep(0.02)
        card = chat["pending"][0]
        assert (chat["state"], card["name"]) == ("waiting", "artifact.trash")
        url = f"/api/projects/{pid}/sessions/1/confirmations/{card['confirmation_id']}"
        assert client.post(url, json={"approve": True}, headers=headers).json() == {
            "approved": True
        }
        assert client.post(url, json={"approve": True}, headers=headers).status_code == 404
        chat = _wait_idle(client, headers, pid)

    assert [m["role"] for m in chat["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert chat["messages"][2]["tool_name"] == "artifact.trash"
    assert client.get(f"/api/projects/{pid}/items", headers=headers).json()["artifacts"] == []


def test_attachments_are_listed_with_their_names(api):
    client, headers, _, pid, _ = api([text_reply("ok")])
    with client:
        row = client.put(
            f"/api/projects/{pid}/files", params={"name": "n.md"}, content=b"# N", headers=headers
        ).json()
        client.post(
            f"/api/projects/{pid}/sessions/1/messages",
            json={"text": "see file", "attachments": [row["id"]]},
            headers=headers,
        )
        chat = _wait_idle(client, headers, pid)
    assert chat["messages"][0]["attachments"] == [{"id": row["id"], "name": "n.md"}]


def test_messages_are_validated(api):
    client, headers, _, pid, _ = api([])
    url = f"/api/projects/{pid}/sessions/1/messages"
    assert client.post(url, json={"text": ""}, headers=headers).status_code == 422
    many = {"text": "x", "attachments": list(range(21))}
    assert client.post(url, json=many, headers=headers).status_code == 422


def test_someone_elses_sessions_are_not_found(api):
    client, headers, app, pid, _ = api([])
    with app.state.sessions.begin() as session:
        session.add(UserRow(id=2, name="other"))
    theirs = app.state.projects.create(User(id=2, name="other"), "Theirs")
    for url in (
        f"/api/projects/{theirs.id}/sessions/1",
        f"/api/projects/{pid}/sessions/99",
        f"/api/projects/{pid}/sessions/99/events",
        f"/api/projects/{theirs.id}/events",
    ):
        assert client.get(url, headers=headers).status_code == 404
    response = client.post(
        f"/api/projects/{theirs.id}/sessions/1/messages", json={"text": "x"}, headers=headers
    )
    assert response.status_code == 404


def test_the_chat_shows_what_is_pinned_and_where_the_summary_ends(api):
    client, headers, app, pid, _ = api([])
    project = app.state.projects.get(User(id=1, name="local"), pid)
    sessions = app.state.projects.db(project)
    pin = state.pin(sessions, 1, "Answer in Ukrainian", "instruction")
    with sessions.begin() as session:
        for role, content in [("user", "a"), ("assistant", "b"), ("user", "c")]:
            session.add(MessageRow(session_id=1, role=role, content=content))
    state.save_summary(sessions, 1, "Goals.", upto=2)

    chat = client.get(f"/api/projects/{pid}/sessions/1", headers=headers).json()
    assert chat["pinned"] == [{"id": pin.id, "kind": "instruction", "text": "Answer in Ukrainian"}]
    assert chat["summary"] == {"upto": 2, "version": 1, "messages": 2}

    url = f"/api/projects/{pid}/sessions/1/pins/{pin.id}"
    assert client.delete(url, headers=headers).json() == {
        "id": pin.id, "text": "Answer in Ukrainian"
    }  # fmt: skip
    assert client.delete(url, headers=headers).status_code == 404
    assert client.get(f"/api/projects/{pid}/sessions/1", headers=headers).json()["pinned"] == []


def test_unpinning_needs_the_chat_to_be_yours(api):
    client, headers, app, pid, _ = api([])
    with app.state.sessions.begin() as session:
        session.add(UserRow(id=2, name="other"))
    theirs = app.state.projects.create(User(id=2, name="other"), "Theirs")
    url = f"/api/projects/{theirs.id}/sessions/1/pins/abc"
    assert client.delete(url, headers=headers).status_code == 404


def test_the_chat_shows_its_pending_follow_ups(api):
    from lectern.agent import followups

    client, headers, app, pid, _ = api([])
    project = app.state.projects.get(User(id=1, name="local"), pid)
    db = app.state.projects.db(project)
    added = followups.add(db, 1, ocr_session_id=5, tool="export.run", arguments={},
                          label="after OCR · lease-scan.pdf: export to ODT",
                          what="export to ODT")  # fmt: skip
    chat = client.get(f"/api/projects/{pid}/sessions/1", headers=headers).json()
    assert chat["followups"] == [{"id": added.id, "label": added.label, "ocr_session_id": 5,
                                  "tool": "export.run", "what": "export to ODT"}]  # fmt: skip


def test_export_now_runs_a_held_follow_up_once(api):
    from lectern.agent import followups

    client, headers, app, pid, handle = api([text_reply("Exported, with the questions marked.")])
    project = app.state.projects.get(User(id=1, name="local"), pid)
    db = app.state.projects.db(project)
    held = followups.add(db, 1, ocr_session_id=5, tool="export.run",
                         arguments={"name": "lease-scan/", "format": "docx"},
                         label="after OCR · lease-scan.pdf: export to DOCX",
                         what="export to DOCX")  # fmt: skip
    followups.mark_held(db, 1, [held.id])
    url = f"/api/projects/{pid}/sessions/1/followups/{held.id}/run"

    with client:
        ran = client.post(url, headers=headers)
        chat = _wait_idle(client, headers, pid)
        again = client.post(url, headers=headers)

    assert ran.status_code == 202 and again.status_code == 404
    assert chat["followups"] == []
    calls = [c["function"]["name"] for m in chat["messages"] for c in (m["tool_calls"] or [])]
    assert calls == ["export_run"]  # run as the held follow-up, once
    assert chat["messages"][-1]["content"] == "Exported, with the questions marked."


def test_export_now_on_someone_elses_chat_is_not_found(api):
    client, headers, app, _, _ = api([])
    with app.state.sessions.begin() as session:
        session.add(UserRow(id=2, name="other"))
    theirs = app.state.projects.create(User(id=2, name="other"), "Theirs")
    url = f"/api/projects/{theirs.id}/sessions/1/followups/abc/run"
    assert client.post(url, headers=headers).status_code == 404


def test_a_tool_results_refs_are_kept_with_it(api):
    client, headers, _, pid, _ = api(
        [tool_reply("project_find", {"query": "lease"}), text_reply("Found it.")]
    )
    with client:
        client.put(
            f"/api/projects/{pid}/files", params={"name": "lease.md"}, content=b"x", headers=headers
        )
        client.post(
            f"/api/projects/{pid}/sessions/1/messages", json={"text": "find the lease"},
            headers=headers,
        )  # fmt: skip
        chat = _wait_idle(client, headers, pid)

    tool, answer = chat["messages"][2], chat["messages"][3]
    assert tool["role"] == "tool"
    assert ("artifact", "lease.md") in [(r["kind"], r["name"]) for r in tool["refs"]]
    assert answer["refs"] == []  # only tool results carry refs


def test_the_project_page_hears_a_tab_its_chat_opens(paths, free_port, live_server):
    """The page shows one chat at a time; a chat that isn't showing still adds its tab."""
    port = free_port
    handle = FakeHandle([tool_reply("tab_open", {"name": "Opening"}), text_reply("Opened.")])
    app = create_app(paths, port=port, runtime=make_runtime(handle))

    with live_server(app, port) as base, httpx2.Client(base_url=base, timeout=10) as client:
        headers = {"X-Lectern-Token": app.state.token, "Origin": base}
        pid = client.post("/api/projects", json={}, headers=headers).json()["id"]
        heard = []
        with client.stream("GET", f"/api/projects/{pid}/events", headers=headers) as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line.removeprefix("data: "))
                if event["type"] == "replayed":  # listening: now the chat may open its tab
                    with httpx2.Client(base_url=base, timeout=10) as sender:
                        sender.post(
                            f"/api/projects/{pid}/sessions/1/messages",
                            json={"text": "Opening"},
                            headers=headers,
                        )
                    continue
                heard.append(event)
                break

    tab = {"kind": "session", "id": 1}
    assert heard == [{"type": "open_tab", "session_id": 1, "tab": tab, "seq": 1}]


def test_removing_a_project_stops_its_chat_first(paths):
    hold = asyncio.Event()  # the model never answers: the turn is still running
    app, handle = _app(paths, [text_reply("slow")], hold=hold)
    client = TestClient(app, base_url=BASE)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    with client:
        project = client.post("/api/projects", json={"name": "Busy"}, headers=headers).json()
        pid = project["id"]
        sent = client.post(
            f"/api/projects/{pid}/sessions/1/messages", json={"text": "one"}, headers=headers
        )
        assert sent.status_code == 202
        deadline = time.monotonic() + 5
        while not handle.requests:  # the turn has asked the model
            assert time.monotonic() < deadline, "the turn never asked the model"
            time.sleep(0.01)
        assert app.state.turns.state((pid, 1)) == "running"

        removed = client.request(
            "DELETE", f"/api/projects/{pid}", json={"name": "Busy"}, headers=headers
        )
        assert removed.status_code == 200
        assert app.state.turns.state((pid, 1)) == "idle"  # stopped, and nothing kept of it
        assert not any(key[0] == pid for key in app.state.turns._turns)
        assert pid not in app.state.projects._dbs  # its database closed, not reopened
