"""The Model Runtime over HTTP: what's loaded, live events, and unloading idle models."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from lectern.api.sse import hub_stream
from lectern.runtime.supervisor import ModelRuntime

router = APIRouter()


def _runtime(request: Request) -> ModelRuntime:
    return request.app.state.runtime


@router.get("/runtime")
def get_runtime(request: Request) -> dict:
    runtime = _runtime(request)
    return {**runtime.state(), "tier": runtime.tier.value}


@router.get("/runtime/events")
async def runtime_events(request: Request) -> StreamingResponse:
    runtime = _runtime(request)
    return hub_stream(request, runtime.events, snapshot={"type": "state", **runtime.state()})


class Unload(BaseModel):
    role: str | None = None


@router.post("/runtime/unload")
async def unload(body: Unload, request: Request) -> dict:
    return {"unloaded": await _runtime(request).unload_idle(body.role)}
