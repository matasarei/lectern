"""POST /api/chat: a streamed reply from the main model, with timing.

The M0 chat has no history or projects: it proves the runtime. It streams the runner's
OpenAI-style chunks as `{"type": "delta", "text": ...}` events and ends with
`{"type": "done", "ttft": seconds, "tps": tokens per second, "tokens": n}`.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from lectern.api.sse import encode
from lectern.runtime.supervisor import NotAvailable, Priority

router = APIRouter()


class Message(BaseModel):
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0.6, ge=0, le=2)
    thinking: bool = False


def _body(req: ChatRequest) -> dict:
    return {
        "messages": [m.model_dump() for m in req.messages],
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": req.thinking},
    }


@router.post("/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    runtime = request.app.state.runtime

    async def stream() -> AsyncIterator[bytes]:
        started = time.perf_counter()
        first: float | None = None
        tokens = 0
        server_tps: float | None = None
        try:
            async with runtime.lease("main", Priority.CHAT) as lease:
                yield encode({"type": "model", "model_id": lease.model_id, "ctx": lease.plan.ctx})
                async with lease.client.stream(
                    "POST", "/v1/chat/completions", json=_body(req)
                ) as response:
                    if response.status_code != 200:
                        detail = (await response.aread()).decode(errors="replace")[:500]
                        yield encode({"type": "error", "detail": f"Model error: {detail}"})
                        return
                    async for line in response.aiter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        chunk = json.loads(line.removeprefix("data: "))
                        timings = chunk.get("timings") or {}
                        if timings.get("predicted_per_second"):
                            server_tps = float(timings["predicted_per_second"])
                        usage = chunk.get("usage") or {}
                        if usage.get("completion_tokens"):
                            tokens = int(usage["completion_tokens"])
                        for choice in chunk.get("choices") or []:
                            text = (choice.get("delta") or {}).get("content")
                            if text:
                                if first is None:
                                    first = time.perf_counter()
                                yield encode({"type": "delta", "text": text})
        except NotAvailable as exc:
            yield encode({"type": "error", "detail": str(exc)})
            return
        ended = time.perf_counter()
        own_tps = tokens / (ended - first) if first and tokens and ended > first else None
        yield encode(
            {
                "type": "done",
                "ttft": round(first - started, 3) if first else None,
                "tps": round(server_tps or own_tps or 0.0, 1),
                "tokens": tokens,
            }
        )

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )
