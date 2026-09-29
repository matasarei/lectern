"""First run: show the hardware and tier, download the chat model, then done.

The step is stored in the settings table, so closing the app halfway through picks up where
it left off. Only the chat model is downloaded up front; everything else on first use.
"""

from __future__ import annotations

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
        "can_finish": download.status == "done",
    }


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
        _save_step(session, body.to)
    return get_first_run(request)
