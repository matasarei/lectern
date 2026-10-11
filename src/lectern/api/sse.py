"""The server's event streams as HTTP responses (server-sent events), and the loop every
stream runs whatever it is sent over (`follow`).

The page follows the same events on WebSockets (`lectern.api.ws`): held open as responses,
its streams used up the six connections a browser allows to one host. These routes stay for
what reads a stream without a browser: `lectern dev agent-check`, and the tests.

Every stream also ends when the app starts shutting down (`app.state.shutting_down`, set by
`lectern serve` on Ctrl+C). Uvicorn waits for open connections before it runs the app's
shutdown hook, so a stream that only ended when the browser disconnected would keep the
process alive after Ctrl+C.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Request
from fastapi.responses import StreamingResponse

from lectern.downloads.manager import EventHub

KEEPALIVE_SECONDS = 15.0


def encode(event: dict) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def hub_stream(
    request: Request, hub: EventHub, *, snapshot: dict | None = None
) -> StreamingResponse:
    async def stream() -> AsyncIterator[bytes]:
        async with hub.listen() as queue:
            async for chunk in pump(request, queue, [snapshot] if snapshot else []):
                yield chunk

    return event_response(stream())


def event_response(body: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(
        body, media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


async def follow(
    queue: asyncio.Queue[dict],
    first: list[dict],
    stopping: asyncio.Event,
    gone: Callable[[], Awaitable[bool]],
) -> AsyncIterator[dict | None]:
    """What a listener is sent, whatever it is sent over: `first`, then whatever arrives on
    `queue`, and None for every KEEPALIVE_SECONDS in which nothing did. Ends when the app
    starts shutting down or `gone()` says the listener went away."""
    for event in first:
        yield event
    while not stopping.is_set() and not await gone():
        next_event = asyncio.ensure_future(queue.get())
        stop = asyncio.ensure_future(stopping.wait())
        try:
            done, _ = await asyncio.wait(
                {next_event, stop},
                timeout=KEEPALIVE_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # Also when the listener is dropped mid-wait: a wait left pending is reported
            # as "Task was destroyed but it is pending!" when it is collected.
            for task in (next_event, stop):
                if not task.done():
                    task.cancel()
        if next_event in done:
            yield next_event.result()
        elif stop not in done:
            yield None


async def pump(
    request: Request, queue: asyncio.Queue[dict], first: list[dict]
) -> AsyncIterator[bytes]:
    """Send `first`, then whatever arrives on `queue`, until the browser goes away or the
    app starts shutting down, with a keep-alive comment every KEEPALIVE_SECONDS."""
    stopping: asyncio.Event = request.app.state.shutting_down
    yield b": connected\n\n"
    async for event in follow(queue, first, stopping, request.is_disconnected):
        yield b": keepalive\n\n" if event is None else encode(event)
