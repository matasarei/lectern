"""First run: show the hardware and tier, download the chat model and the llama.cpp
engine, then done.

The step is stored in the settings table, so closing the app halfway through picks up where
it left off. Only the chat model and the engine are downloaded up front (so the first chat
only loads the model); everything else on first use.
"""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from lectern.catalog import catalog
from lectern.catalog.catalog import CatalogEntry
from lectern.core.tables import SettingRow
from lectern.hardware.probe import probe
from lectern.hardware.tiers import effective_tier

Step = Literal["hardware", "download", "done"]
ORDER: tuple[Step, ...] = ("hardware", "download", "done")
SETTING_KEY = "first_run"

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


def _chat_model() -> CatalogEntry:
    return catalog.load().defaults_for(effective_tier(probe()))["main"]


def _report(request: Request, step: Step) -> dict:
    entry = _chat_model()
    download = request.app.state.downloads.state(entry)
    return {
        "step": step,
        "chat_model": {
            "id": entry.id,
            "title": entry.title,
            "quant": entry.quant,
            "size": entry.size,
            "licence": entry.licence,
            "download": download.to_dict(),
        },
        "engine": _engine_report(request),
        "can_finish": download.status == "done" and _engine_ready(request),
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
              "bytes_total": engine.size(), "error": None}  # fmt: skip
    if engine.installed():
        return {**report, "status": "done", "bytes_done": report["bytes_total"]}
    if task is not None and not task.done():
        activity = request.app.state.runtime.activity or {}
        if activity.get("kind") == "engine":
            report.update(bytes_done=activity["done"], bytes_total=activity["total"])
        return {**report, "status": "downloading"}
    if task is not None and task.exception() is not None:
        return {**report, "status": "failed", "error": str(task.exception())}
    return report


@router.post("/first-run/engine", status_code=202)
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


@router.post("/first-run")
def advance_first_run(body: Advance, request: Request) -> dict:
    with request.app.state.sessions.begin() as session:
        current = _load_step(session)
        if ORDER.index(body.to) != ORDER.index(current) + 1:
            raise HTTPException(409, f"First run is at '{current}'; it can't move to '{body.to}'.")
        if body.to == "done" and request.app.state.downloads.state(_chat_model()).status != "done":
            raise HTTPException(409, "Download the chat model before finishing.")
        if body.to == "done" and not _engine_ready(request):
            raise HTTPException(409, "Download the engine (llama.cpp) before finishing.")
        _save_step(session, body.to)
    return get_first_run(request)
