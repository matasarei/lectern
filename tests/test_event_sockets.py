"""Event streams over a WebSocket (`lectern.api.ws`): what the page follows."""

from __future__ import annotations

import asyncio

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
