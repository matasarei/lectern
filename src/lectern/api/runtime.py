"""The Model Runtime over HTTP: what's loaded, live events, and unloading idle models."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from lectern.api.sse import hub_stream
from lectern.core import settings
from lectern.runners import mlx
from lectern.runtime.supervisor import ModelRuntime

router = APIRouter()


def _runtime(request: Request) -> ModelRuntime:
    return request.app.state.runtime


@router.get("/runtime")
async def get_runtime(request: Request) -> dict:
    # `async` on purpose: the runtime's state belongs to the event loop. A plain `def` would
    # run in a worker thread and could read it mid-change.
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
    available = mlx.mlx_available()
    # Switched on where MLX can't run (before the switch was hidden there) or isn't installed
    # (a checkout without the extra, on the installed Lectern's data): it does nothing here,
    # so report it off, and the next save stores it off.
    values = {**values, "mlx_enabled": values["mlx_enabled"] and available}
    return {
        "values": values,
        "implemented": {k: sorted(v) for k, v in IMPLEMENTED.items()},
        "mlx_supported": mlx.mlx_supported(),
        "mlx_available": available,
    }


@router.put("/runtime/settings")
async def put_settings(body: RuntimeSettings, request: Request) -> dict:
    for key, allowed in IMPLEMENTED.items():
        if getattr(body, key) not in allowed:
            raise HTTPException(409, f"{getattr(body, key)!r} for {key} isn't available yet.")
    if body.mlx_enabled and not mlx.mlx_supported():
        raise HTTPException(409, "MLX only runs on Apple Silicon Macs.")
    if body.mlx_enabled and not mlx.mlx_available():
        raise HTTPException(409, mlx.NOT_INSTALLED)
    sessions = request.app.state.sessions
    before = await run_in_threadpool(
        settings.get, sessions, settings.RUNTIME_KEY, settings.RUNTIME_DEFAULTS
    )
    await run_in_threadpool(settings.put, sessions, settings.RUNTIME_KEY, body.model_dump())
    if body.mlx_enabled != before["mlx_enabled"]:
        # The switch changes which model a role runs on, and whether OCR cleanup needs its
        # own copy of the chat model. What's loaded and idle goes now and loads again by the
        # new rule when it's next used; the model of a reply being written changes over with
        # the next turn. A model a job is using stays as well, and one kind is then left over:
        # cleanup's copy of the chat model, in use when MLX is switched off, isn't asked for
        # again and stays loaded until another model needs its room.
        await _runtime(request).unload_between_replies()
    return await run_in_threadpool(get_settings, request)
