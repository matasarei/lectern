from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.core.auth import User
from lectern.core.tables import UserRow

from .fake_model import FakeHandle, make_runtime, text_reply, tool_reply

BASE = "http://127.0.0.1:8765"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _running(app, port):
    """A real uvicorn server in a thread: TestClient can't read an open event stream."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


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


def test_a_page_attaching_mid_turn_gets_the_turn_so_far_then_the_rest_live(paths):
    hold = threading.Event()
    port = _free_port()
    handle = FakeHandle([text_reply("Hel", "lo")], hold=hold)
    app = create_app(paths, port=port, runtime=make_runtime(handle))

    with _running(app, port) as base, httpx2.Client(base_url=base, timeout=10) as client:
        headers = {"X-Lectern-Token": app.state.token, "Origin": base}
        pid = client.post("/api/projects", json={}, headers=headers).json()["id"]
        client.post(
            f"/api/projects/{pid}/sessions/1/messages", json={"text": "hi"}, headers=headers
        )
        while not handle.requests:  # the model has been asked, and is holding its reply
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
    ):
        assert client.get(url, headers=headers).status_code == 404
    response = client.post(
        f"/api/projects/{theirs.id}/sessions/1/messages", json={"text": "x"}, headers=headers
    )
    assert response.status_code == 404
