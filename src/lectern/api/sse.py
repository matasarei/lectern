"""Server-sent events read with fetch() (EventSource can't send the token header)."""

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
    async def stream() -> AsyncIterator[bytes]:
        async with hub.listen() as queue:
            yield b": connected\n\n"
            if snapshot is not None:
                yield encode(snapshot)
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                yield encode(event)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )
