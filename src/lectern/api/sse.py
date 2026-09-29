"""Server-sent events read with fetch() (EventSource can't send the token header).

Every stream also ends when the app starts shutting down (`app.state.shutting_down`, set by
`lectern serve` on Ctrl+C). Uvicorn waits for open connections before it runs the app's
shutdown hook, so a stream that only ended when the browser disconnected would keep the
process alive after Ctrl+C.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import Request
from fastapi.responses import StreamingResponse

from lectern.downloads.manager import EventHub

KEEPALIVE_SECONDS = 15.0


def encode(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


def hub_stream(
    request: Request, hub: EventHub, *, snapshot: dict | None = None
) -> StreamingResponse:
    stopping: asyncio.Event = request.app.state.shutting_down

    async def stream() -> AsyncIterator[bytes]:
        async with hub.listen() as queue:
            yield b": connected\n\n"
            if snapshot is not None:
                yield encode(snapshot)
            while not stopping.is_set() and not await request.is_disconnected():
                next_event = asyncio.ensure_future(queue.get())
                stop = asyncio.ensure_future(stopping.wait())
                done, _ = await asyncio.wait(
                    {next_event, stop},
                    timeout=KEEPALIVE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in (next_event, stop):
                    if task not in done:
                        task.cancel()
                if next_event in done:
                    yield encode(next_event.result())
                elif stop not in done:
                    yield b": keepalive\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )
