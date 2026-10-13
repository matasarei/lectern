"""Event streams over a WebSocket: what the page follows.

The same events as the HTTP streams in `sse`, one per text frame. The page uses sockets because
a browser allows six HTTP/1.1 connections to a host, shared by all its tabs: two project tabs
held that many streams open, and every other request then waited in the browser. Sockets don't
count against the six.

Like an HTTP stream, a socket ends when the app starts shutting down, so Ctrl+C isn't held up
by an open page. Nothing is sent while a stream is quiet: the server's own WebSocket ping
finds a browser that went away without closing.

The guard lets a socket in when it opens, and a stream then goes on for hours. So while it
follows, a socket asks every few seconds whether its opener is still signed in, and closes
when they aren't: signed out, given a new password or disabled, here or by `lectern user`
in another process, which is why it asks and isn't told.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from lectern.api.sse import follow
from lectern.core.security import WS_SUBPROTOCOL
from lectern.downloads.manager import EventHub

# Close codes 4000-4999 are an application's own. Ours: 4000 + the HTTP status it stands for.
APP_CODES = 4000
NOT_FOUND = APP_CODES + 404
NOT_SIGNED_IN = APP_CODES + 401
GOING_AWAY = 1001
REASON_BYTES = 123  # the most a close frame carries
# How often an open socket asks whether its opener is still signed in: how long events keep
# reaching a login that has ended, against a read of the database per socket.
LOGIN_CHECK_SECONDS = 10.0


async def hub_socket(websocket: WebSocket, hub: EventHub, *, snapshot: dict | None = None) -> None:
    async with hub.listen() as queue:
        await socket_events(websocket, queue, [snapshot] if snapshot else [])


async def socket_events(
    websocket: WebSocket, queue: asyncio.Queue[dict], first: list[dict]
) -> None:
    """Accept the socket and send `first`, then whatever arrives on `queue`, until the browser
    closes it, the app starts shutting down, or its opener is no longer signed in."""
    stopping: asyncio.Event = websocket.app.state.shutting_down
    await websocket.accept(subprotocol=WS_SUBPROTOCOL)
    closed = asyncio.ensure_future(_closed(websocket))
    sending = asyncio.ensure_future(_send(websocket, follow(queue, first, stopping, _never)))
    signed_out = asyncio.ensure_future(_signed_out(websocket))
    tasks = (closed, sending, signed_out)
    ran_out = turned_out = False
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        ran_out = sending.done() and not closed.done()
        turned_out = signed_out.done() and not closed.done()
    finally:
        for task in tasks:
            task.cancel()
        # Waited for, so the event loop's waits are cancelled before the listener is dropped.
        # If this route is itself being cancelled, that cancellation is already on its way
        # out of this block: one more arriving here must not take its place.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
    for task in (sending, signed_out):
        failure = None if task.cancelled() else task.exception()
        if isinstance(failure, WebSocketDisconnect):
            return  # the send found the browser gone: nobody to tell
        if failure is not None:
            # A bug (an event that can't be sent), or a question about the login that
            # couldn't be asked. Raised so it is logged, as on an HTTP stream: dropped
            # here, the page would reopen the socket into the same thing without a word.
            raise failure
    if turned_out:
        await websocket.close(code=NOT_SIGNED_IN, reason="Not signed in")
    elif ran_out:  # the events ran out with the browser still there: the app is shutting down
        await websocket.close(code=GOING_AWAY)


async def refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Say why there is nothing to follow. Accepted first: a browser is told nothing about a
    socket closed before it opened, only that it closed, as when the server is down."""
    await websocket.accept(subprotocol=WS_SUBPROTOCOL)
    fits = reason.encode()[:REASON_BYTES].decode(errors="ignore")
    await websocket.close(code=code, reason=fits)


async def refuse_as(websocket: WebSocket, error: HTTPException) -> None:
    """Refuse with what an HTTP route would have answered: a 404 closes with 4404."""
    await refuse(websocket, APP_CODES + error.status_code, str(error.detail))


async def _send(websocket: WebSocket, events: AsyncIterator[dict | None]) -> None:
    async for event in events:
        if event is not None:  # None: a quiet stretch, which a socket needs nothing sent for
            await websocket.send_text(json.dumps(event, ensure_ascii=False))


async def _closed(websocket: WebSocket) -> None:
    """Returns when the browser has closed the socket. The page has nothing to say on it:
    whatever else arrives is ignored."""
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass


async def _signed_out(websocket: WebSocket) -> None:
    """Returns once whoever opened the socket is no longer signed in. Never, for a socket
    the guard left nothing to ask about (it sets `signed_in` on every one it lets in)."""
    still_signed_in = getattr(websocket.state, "signed_in", None)
    if still_signed_in is None:
        await asyncio.Event().wait()
        return  # not reached: nothing sets that event
    while await still_signed_in():
        await asyncio.sleep(websocket.app.state.login_check_seconds)


async def _never() -> bool:
    return False  # a socket says when it's gone (`_closed`); there is nothing to ask
