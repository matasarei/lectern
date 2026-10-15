"""A model server stand-in for chat tests: it plays back prepared OpenAI-style streams."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from pathlib import Path

import httpx2

from lectern.hardware.tiers import Tier
from lectern.runners.base import MeasuredMemory
from lectern.runtime.planner import ModelShape
from lectern.runtime.supervisor import ModelChoice, ModelRuntime

GIB = 1024**3
SHAPE = ModelShape(layers=8, kv_layers=8, kv_heads=1, key_length=64, value_length=64)


def sse(*chunks: dict) -> bytes:
    return "".join(
        [f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    ).encode()


def text_reply(*pieces: str) -> bytes:
    return sse(
        *[{"choices": [{"delta": {"content": p}}]} for p in pieces],
        {"choices": [], "usage": {"completion_tokens": len(pieces)}},
    )


def tool_reply(name: str, arguments: dict | str, call_id: str = "call_1") -> bytes:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    head, tail = args[: len(args) // 2], args[len(args) // 2 :]
    return sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": head}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": tail}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )  # fmt: skip


class FakeHandle:
    """A model server that plays back one prepared reply per completion request."""

    model_id = "qwen3.5-4b"
    base_url = "http://127.0.0.1:1"
    pid = 4242

    def __init__(
        self,
        replies: list[bytes],
        *,
        hold: asyncio.Event | threading.Event | None = None,
        hold_if: Callable[[dict], bool] | None = None,
    ) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.hold = hold
        self.hold_if = hold_if  # only the requests it picks wait for `hold`; all, without it

    def client(self) -> httpx2.AsyncClient:
        async def handler(request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(0)
            body = json.loads(request.content)
            if request.url.path == "/tokenize":
                return httpx2.Response(200, json={"tokens": [0] * (len(body["content"]) // 4 + 1)})
            self.requests.append(body)
            if self.hold_if is not None and not self.hold_if(body):
                pass
            elif isinstance(self.hold, threading.Event):  # set from another thread
                while not self.hold.is_set():
                    await asyncio.sleep(0.01)
            elif self.hold is not None:
                await self.hold.wait()
            return httpx2.Response(200, content=self.replies.pop(0))

        return httpx2.AsyncClient(base_url=self.base_url, transport=httpx2.MockTransport(handler))

    def is_running(self) -> bool:
        return True

    def measured_memory(self) -> MeasuredMemory:
        return MeasuredMemory(device_bytes=2 * GIB)

    async def stop(self) -> None:
        pass


class FakeRunner:
    def __init__(self, handle):  # noqa: ANN001
        self.handle = handle

    async def start(self, spec):  # noqa: ANN001
        return self.handle


def make_runtime(
    handle: FakeHandle, *, weights: int = 2 * GIB, per_role: bool = False
) -> ModelRuntime:
    """A runtime whose every model is `handle`. With per_role=True each role gets its own
    model id (so main and code are separate residents, and big weights force a swap)."""
    return ModelRuntime(
        budget=8 * GIB,
        unified=False,
        tier=Tier.MINIMUM,
        resolve=lambda role: ModelChoice(
            role,
            f"{role}-model" if per_role else "qwen3.5-4b",
            Path("/m/q.gguf"),
            weights,
            SHAPE,
            8192,
        ),  # fmt: skip
        runner_for=lambda choice: FakeRunner(handle),
    )
