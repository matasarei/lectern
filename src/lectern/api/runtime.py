"""The Model Runtime over HTTP: what's loaded, live events, and unloading idle models."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from lectern.api.sse import hub_stream
from lectern.core import settings
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


# --- settings ------------------------------------------------------------------------------

# Only these values are implemented in M0; the others are shown in the UI as "later".
IMPLEMENTED = {
    "when_models_dont_fit": {"swap_between_phases"},
    "chat_during_job": {"pause_job_after_current_item"},
}


class RuntimeSettings(BaseModel):
    mlx_enabled: bool
    when_models_dont_fit: Literal["swap_between_phases", "keep_one_loaded"]
    chat_during_job: Literal["pause_job_after_current_item", "chat_waits"]


@router.get("/runtime/settings")
def get_settings(request: Request) -> dict:
    values = settings.get(
        request.app.state.sessions, settings.RUNTIME_KEY, settings.RUNTIME_DEFAULTS
    )
    return {"values": values, "implemented": {k: sorted(v) for k, v in IMPLEMENTED.items()}}


@router.put("/runtime/settings")
def put_settings(body: RuntimeSettings, request: Request) -> dict:
    for key, allowed in IMPLEMENTED.items():
        if getattr(body, key) not in allowed:
            raise HTTPException(409, f"{getattr(body, key)!r} for {key} isn't available yet.")
    settings.put(request.app.state.sessions, settings.RUNTIME_KEY, body.model_dump())
    return get_settings(request)
