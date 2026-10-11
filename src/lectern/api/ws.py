"""Event streams over a WebSocket: what the page follows.

The same events as the HTTP streams in `sse`, one per text frame. The page uses sockets because
a browser allows six HTTP/1.1 connections to a host, shared by all its tabs: two project tabs
held that many streams open, and every other request then waited in the browser. Sockets don't
count against the six.

Like an HTTP stream, a socket ends when the app starts shutting down, so Ctrl+C isn't held up
by an open page. Nothing is sent while a stream is quiet: the server's own WebSocket ping
finds a browser that went away without closing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from fastapi import WebSocket

from lectern.api.sse import follow
from lectern.core.security import WS_SUBPROTOCOL
from lectern.downloads.manager import EventHub

# Close codes 4000-4999 are an application's own. Ours: 4000 + the HTTP status it stands for.
NOT_FOUND = 4404
GOING_AWAY = 1001
REASON_BYTES = 123  # the most a close frame carries


async def hub_socket(websocket: WebSocket, hub: EventHub, *, snapshot: dict | None = None) -> None:
    async with hub.listen() as queue:
        await socket_events(websocket, queue, [snapshot] if snapshot else [])


async def socket_events(
    websocket: WebSocket, queue: asyncio.Queue[dict], first: list[dict]
) -> None:
    """Accept the socket and send `first`, then whatever arrives on `queue`, until the browser
    closes it or the app starts shutting down."""
    stopping: asyncio.Event = websocket.app.state.shutting_down
    await websocket.accept(subprotocol=WS_SUBPROTOCOL)
    closed = asyncio.ensure_future(_closed(websocket))
    sending = asyncio.ensure_future(_send(websocket, follow(queue, first, stopping, _never)))
    ran_out = False
    try:
        await asyncio.wait({closed, sending}, return_when=asyncio.FIRST_COMPLETED)
        ran_out = sending.done() and not closed.done()
    finally:
        for task in (closed, sending):
            task.cancel()
        # Waited for, so the event loop's waits are cancelled before the listener is dropped.
        # If this route is itself being cancelled, that cancellation is already on its way
        # out of this block: one more arriving here must not take its place.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(closed, sending, return_exceptions=True)
    # The events ran out with the browser still there: the app is shutting down. (A send
    # that failed means the browser is gone, and there is nobody to tell.)
    if ran_out and sending.exception() is None:
        await websocket.close(code=GOING_AWAY)


async def refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Say why there is nothing to follow. Accepted first: a browser is told nothing about a
    socket closed before it opened, only that it closed, as when the server is down."""
    await websocket.accept(subprotocol=WS_SUBPROTOCOL)
    fits = reason.encode()[:REASON_BYTES].decode(errors="ignore")
    await websocket.close(code=code, reason=fits)


async def _send(websocket: WebSocket, events: AsyncIterator[dict | None]) -> None:
    async for event in events:
        if event is not None:  # None: a quiet stretch, which a socket needs nothing sent for
            await websocket.send_text(json.dumps(event, ensure_ascii=False))


async def _closed(websocket: WebSocket) -> None:
    """Returns when the browser has closed the socket. The page has nothing to say on it:
    whatever else arrives is ignored."""
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass


async def _never() -> bool:
    return False  # a socket says when it's gone (`_closed`); there is nothing to ask
