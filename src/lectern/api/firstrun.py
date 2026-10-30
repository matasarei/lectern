"""First run: show the hardware and tier, download the chat model and the llama.cpp
engine, then done.

The step is stored in the settings table, so closing the app halfway through picks up where
it left off. Only the chat model and the engine are downloaded up front (so the first chat
only loads the model); everything else on first use.

The chat model is the recommended one unless another is chosen from the list here. On a Mac
with MLX switched on, the model's MLX build is all the first chat needs: setup finishes on it,
and llama.cpp and the model that cleans scanned pages are fetched by the first scan.
"""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from lectern.catalog import catalog
from lectern.catalog.catalog import CatalogEntry
from lectern.core import settings
from lectern.core.auth import require_admin
from lectern.core.tables import SettingRow
from lectern.runners import mlx
from lectern.runtime.wiring import pick

Step = Literal["hardware", "download", "done"]
ORDER: tuple[Step, ...] = ("hardware", "download", "done")
SETTING_KEY = "first_run"
# Seconds without a byte after which the engine's download says it seems stuck: it reports
# every second while bytes arrive.
ENGINE_STALLED = 30.0

router = APIRouter()


def _load_step(session: Session) -> Step:
    row = session.get(SettingRow, SETTING_KEY)
    if row is None:
        return "hardware"
    step = json.loads(row.value).get("step", "hardware")
    return step if step in ORDER else "hardware"


def _save_step(session: Session, step: Step) -> None:
    value = json.dumps({"step": step})
    row = session.get(SettingRow, SETTING_KEY)
    if row is None:
        session.add(SettingRow(key=SETTING_KEY, value=value))
    else:
        row.value = value


def _chat_model(request: Request) -> CatalogEntry:
    """The chat role's model: the one recommended for this computer, unless another was
    chosen in Models & memory."""
    runtime = request.app.state.runtime
    return pick("main", runtime.tier, runtime.chosen_models())[1]


def _mlx(request: Request) -> dict:
    """Whether MLX can run here, and whether it's switched on. Switched on where it can't
    run, it does nothing, so it's reported off (as the runtime settings do)."""
    values = settings.get(
        request.app.state.sessions, settings.RUNTIME_KEY, settings.RUNTIME_DEFAULTS
    )
    available = mlx.mlx_available()
    return {
        "supported": mlx.mlx_supported(),
        "available": available,
        "enabled": bool(values["mlx_enabled"]) and available,
    }


def _mlx_build(request: Request) -> CatalogEntry | None:
    """The MLX build the chat would run on: the chat model's, while MLX is switched on."""
    if not _mlx(request)["enabled"]:
        return None
    return catalog.load().mlx_build_of(_chat_model(request))


def _missing(request: Request) -> str | None:
    """What still has to be downloaded before setup can finish, as a sentence; None when the
    first chat has what it runs on."""
    downloads = request.app.state.downloads
    build = _mlx_build(request)
    if build is not None and downloads.is_downloaded(build):
        return None
    # Without its build on disk the chat runs on the usual model, MLX switched on or not.
    if downloads.state(_chat_model(request)).status == "done":
        if _engine_ready(request):
            return None
        return "Download the engine (llama.cpp) before finishing."
    if build is not None:
        return f"Download {build.title} before finishing."
    return "Download the chat model before finishing."


def _model(request: Request, entry: CatalogEntry) -> dict:
    runtime = request.app.state.runtime
    downloads = request.app.state.downloads
    cat = catalog.load()
    build = cat.mlx_build_of(entry)
    return {
        "id": entry.id,
        "title": entry.title,
        "quant": entry.quant,
        "size": entry.size,
        "licence": entry.licence,
        "recommended": cat.defaults_for(runtime.tier).get("main") == entry,
        "suits_tier": runtime.tier in entry.ctx,  # listed for this computer's tier
        "download": downloads.state(entry).to_dict(),
        # The same model in MLX's format, which runs in its place with MLX switched on.
        "mlx_build": None if build is None else _build(request, build),
    }


def _build(request: Request, build: CatalogEntry) -> dict:
    return {
        "id": build.id,
        "title": build.title,
        "size": build.size,
        "licence": build.licence,
        "download": request.app.state.downloads.state(build).to_dict(),
    }


def _report(request: Request, step: Step) -> dict:
    return {
        "step": step,
        "chat_model": _model(request, _chat_model(request)),
        # Every model the chat can be given (PUT /models/roles/main chooses one).
        "chat_models": [_model(request, m) for m in catalog.load().choices_for("main")],
        "mlx": _mlx(request),
        "engine": _engine_report(request),
        "can_finish": _missing(request) is None,
    }


def _engine_ready(request: Request) -> bool:
    engine = getattr(request.app.state.runtime, "engine", None)
    return engine is None or engine.installed()


def _engine_report(request: Request) -> dict | None:
    """The llama.cpp build: none (not downloaded), downloading, done or failed; None where
    there's no llama.cpp build for this machine."""
    engine = getattr(request.app.state.runtime, "engine", None)
    if engine is None:
        return None
    task: asyncio.Task | None = getattr(request.app.state, "engine_task", None)
    report = {"variant": engine.variant, "status": "none", "bytes_done": 0,
              "bytes_total": engine.size(), "rate": 0, "stalled": False,
              "error": None}  # fmt: skip
    if engine.installed():
        weighs = engine.variant_bytes()
        return {**report, "status": "done", "bytes_done": weighs, "bytes_total": weighs}
    if task is not None and not task.done():
        runtime = request.app.state.runtime
        activity = runtime.activity or {}
        if activity.get("kind") == "engine":
            report.update(bytes_done=activity["done"], bytes_total=activity["total"],
                          rate=activity.get("rate", 0))  # fmt: skip
        idle = runtime.engine_idle_seconds()
        stalled = idle is not None and idle >= ENGINE_STALLED
        return {**report, "status": "downloading", "stalled": stalled,
                "rate": 0 if stalled else report["rate"]}  # fmt: skip
    if task is not None and task.exception() is not None:
        return {**report, "status": "failed", "error": str(task.exception())}
    return report


@router.post("/first-run/engine", status_code=202, dependencies=[Depends(require_admin)])
async def download_engine(request: Request) -> dict:
    """Start the engine's download in the background, once; the page follows it through
    the runtime's events. Again after a failure: it tries again."""
    engine = getattr(request.app.state.runtime, "engine", None)
    task: asyncio.Task | None = getattr(request.app.state, "engine_task", None)
    if engine is not None and not engine.installed() and (task is None or task.done()):
        request.app.state.engine_task = asyncio.create_task(engine.install(), name="engine")
        await asyncio.sleep(0)  # let it start, so the report below says "downloading"
    with request.app.state.sessions() as session:
        return _report(request, _load_step(session))


@router.get("/first-run")
def get_first_run(request: Request) -> dict:
    with request.app.state.sessions() as session:
        return _report(request, _load_step(session))


class Advance(BaseModel):
    to: Step


@router.post("/first-run", dependencies=[Depends(require_admin)])
def advance_first_run(body: Advance, request: Request) -> dict:
    with request.app.state.sessions.begin() as session:
        current = _load_step(session)
        if ORDER.index(body.to) != ORDER.index(current) + 1:
            raise HTTPException(409, f"First run is at '{current}'; it can't move to '{body.to}'.")
        if body.to == "done" and (missing := _missing(request)) is not None:
            raise HTTPException(409, missing)
        _save_step(session, body.to)
    return get_first_run(request)
