"""Event streams over a WebSocket (`lectern.api.ws`): what the page follows."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lectern.api import ws
from lectern.downloads.manager import EventHub

PROTOCOLS = ["lectern.v1"]


@pytest.fixture
def served():
    """A bare app serving one hub over a socket, and a place with nothing to follow.
    Returns (client, app, hub)."""
    app = FastAPI()
    app.state.shutting_down = asyncio.Event()
    hub = EventHub()

    @app.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        await ws.hub_socket(websocket, hub, snapshot={"type": "state", "n": 0})

    @app.websocket("/nothing")
    async def nothing(websocket: WebSocket) -> None:
        await ws.refuse(websocket, ws.NOT_FOUND, "No such chat.")

    return TestClient(app), app, hub


def test_it_sends_the_snapshot_and_then_each_event_as_json_text(served):
    client, _, hub = served
    with client.websocket_connect("/events", subprotocols=PROTOCOLS) as socket:
        assert socket.accepted_subprotocol == "lectern.v1"
        assert socket.receive_json() == {"type": "state", "n": 0}
        socket.portal.call(hub.publish, {"type": "download", "title": "Модель"})
        text = socket.receive_text()
    # As the HTTP stream sends it: not escaped to ASCII.
    assert text == '{"type": "download", "title": "Модель"}'


def test_it_closes_when_the_app_starts_shutting_down(served):
    client, app, _ = served
    with client.websocket_connect("/events", subprotocols=PROTOCOLS) as socket:
        socket.receive_json()
        socket.portal.call(app.state.shutting_down.set)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 1001  # going away: the page opens it again


def test_it_stops_listening_when_the_browser_closes(served):
    client, _, hub = served
    with client.websocket_connect("/events", subprotocols=PROTOCOLS) as socket:
        socket.receive_json()
        assert len(hub._queues) == 1
    # Leaving the block closed the socket and waited for the route to return.
    assert hub._queues == set()


def test_what_the_browser_sends_is_ignored(served):
    client, _, hub = served
    with client.websocket_connect("/events", subprotocols=PROTOCOLS) as socket:
        socket.receive_json()
        socket.send_text("anything")
        socket.portal.call(hub.publish, {"n": 1})
        assert socket.receive_json() == {"n": 1}


def test_a_refusal_is_a_close_the_page_can_read(served):
    client, _, _ = served
    # Accepted, then closed with the reason: a socket refused before it opens tells a
    # browser nothing, not even that it was refused.
    with client.websocket_connect("/nothing", subprotocols=PROTOCOLS) as socket:
        assert socket.accepted_subprotocol == "lectern.v1"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert (closed.value.code, closed.value.reason) == (4404, "No such chat.")


class _Socket:
    """A socket whose browser stays: nothing arrives on it. `send` is what sending does."""

    def __init__(self, send=None) -> None:  # noqa: ANN001
        self.app = SimpleNamespace(state=SimpleNamespace(shutting_down=asyncio.Event()))
        self.closed_with: int | None = None
        self.reason: str | None = None
        self._send = send

    async def accept(self, subprotocol: str | None = None) -> None:
        pass

    async def send_text(self, text: str) -> None:
        if self._send is not None:
            self._send(text)

    async def receive(self) -> dict:
        await asyncio.Event().wait()  # never: the browser is still there
        raise AssertionError

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_with = code
        self.reason = reason


def test_a_refusals_reason_is_cut_to_what_a_close_frame_carries():
    """123 bytes at most, and never half a character."""
    socket = _Socket()
    asyncio.run(ws.refuse(socket, ws.NOT_FOUND, "Немає такого проєкту. " * 10))
    assert socket.closed_with == 4404
    assert len(socket.reason.encode()) <= 123
    assert socket.reason.startswith("Немає такого проєкту. Немає") and "\ufffd" not in socket.reason


# --- the app's five streams, each at the path of its HTTP stream -------------------------------

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def api(paths):
    """The app with a project in it (which starts with a chat, session 1).
    Returns (follow, app, project_id): follow(path) opens that path's socket as the page does."""
    from lectern.app import create_app

    app = create_app(paths, port=8765)
    client = TestClient(app, base_url=BASE)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    made = client.post("/api/projects", json={"name": "Sockets"}, headers=headers)
    project_id = made.json()["id"]

    def follow(path: str, *, token: str | None = app.state.token):  # noqa: ANN202
        offered = [*PROTOCOLS, f"lectern.token.{token}"] if token else PROTOCOLS
        # Starlette's TestClient sends "testserver" as the WebSocket Host unless told otherwise.
        return client.websocket_connect(
            path, headers={"Host": "127.0.0.1:8765", "Origin": BASE}, subprotocols=offered
        )

    return follow, app, project_id


def test_the_runtime_socket_opens_with_the_state(api):
    follow, app, _ = api
    with follow("/api/runtime/events") as socket:
        first = socket.receive_json()
    assert first["type"] == "state"
    assert first["residents"] == [] and first["budget"] == app.state.runtime.state()["budget"]


def test_the_downloads_socket_sends_what_the_hub_publishes(api):
    follow, app, _ = api
    with follow("/api/models/events") as socket:
        socket.portal.call(app.state.downloads.events.publish, {"type": "download", "n": 1})
        assert socket.receive_json() == {"type": "download", "n": 1}


def test_a_chats_socket_replays_the_turn_so_far_and_says_where_the_replay_ends(api):
    follow, app, pid = api
    channel = app.state.turns.channel((pid, 1))
    channel.publish({"type": "model", "name": "m"})
    with follow(f"/api/projects/{pid}/sessions/1/events") as socket:
        replayed = [socket.receive_json(), socket.receive_json()]
        socket.portal.call(channel.publish, {"type": "delta", "text": "Hi"})
        live = socket.receive_json()
    assert replayed == [{"type": "model", "name": "m", "seq": 1}, {"type": "replayed", "seq": 0}]
    assert live == {"type": "delta", "text": "Hi", "seq": 2}


def test_a_projects_socket_is_live_only(api):
    follow, app, pid = api
    channel = app.state.turns.project_channel(pid)
    with follow(f"/api/projects/{pid}/events") as socket:
        assert socket.receive_json() == {"type": "replayed", "seq": 0}
        socket.portal.call(channel.publish, {"type": "open_tab", "session_id": 1})
        assert socket.receive_json() == {"type": "open_tab", "session_id": 1, "seq": 1}


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/api/projects/{pid}/sessions/99/events", "No such session."),
        ("/api/projects/999/sessions/1/events", "No such project."),
        ("/api/projects/999/events", "No such project."),
        ("/api/projects/{pid}/ocr/99/events", "No such session."),
        ("/api/projects/{pid}/ocr/1/events", "No such OCR session."),  # session 1 is a chat
    ],
)
def test_what_isnt_there_closes_its_socket_with_a_reason_the_page_can_read(api, path, reason):
    follow, _, pid = api
    with follow(path.format(pid=pid)) as socket, pytest.raises(WebSocketDisconnect) as closed:
        socket.receive_json()
    assert (closed.value.code, closed.value.reason) == (4404, reason)


@pytest.mark.parametrize(
    "path",
    ["/api/runtime/events", "/api/models/events", "/api/projects/{pid}/sessions/1/events"],
)
def test_an_events_socket_without_the_token_is_refused_before_it_opens(api, path):
    follow, _, pid = api
    with pytest.raises(WebSocketDisconnect) as refused, follow(path.format(pid=pid), token=None):
        pass
    assert refused.value.code == 1008


# --- what a failed send means -------------------------------------------------------------------


def test_an_error_while_sending_isnt_swallowed():
    """Something that can't be sent (a bug: an event that isn't JSON) must reach the server's
    log, as it does on an HTTP stream. Dropped silently, the page reopens the socket every
    second into the same event, and nothing anywhere says why."""
    socket = _Socket()
    with pytest.raises(TypeError, match="not JSON serializable"):
        asyncio.run(ws.socket_events(socket, asyncio.Queue(), [{"broken": object()}]))


def test_a_send_that_finds_the_browser_gone_ends_the_stream_quietly():
    def gone(text: str) -> None:
        raise WebSocketDisconnect(code=1006)

    socket = _Socket(send=gone)
    asyncio.run(ws.socket_events(socket, asyncio.Queue(), [{"n": 1}]))
    assert socket.closed_with is None  # nobody to tell
