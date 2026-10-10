"""App-wide settings stored as JSON values in the settings table."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from lectern.core.tables import SettingRow

RUNTIME_KEY = "runtime"
RUNTIME_DEFAULTS: dict[str, Any] = {
    "mlx_enabled": False,  # a choice on a Mac; llama.cpp is what a fresh install runs
    "when_models_dont_fit": "swap_between_phases",  # or "keep_one_loaded"
    "chat_during_job": "pause_job_after_current_item",  # or "chat_waits"
}
MODELS_KEY = "models"
# The model chosen for each role, as a catalog id. None: the one recommended for this computer,
# which follows the hardware (runtime/wiring.py `pick`).
MODEL_DEFAULTS: dict[str, Any] = {"main": None, "ocr": None, "ocr.hand": None, "code": None}
HARDWARE_KEY = "hardware"
# The tier this computer had at the last start, to notice that its hardware changed. Never
# used to decide anything else: the tier itself is worked out from the hardware at each start.
HARDWARE_DEFAULTS: dict[str, Any] = {"tier": None}


def get(sessions: sessionmaker[Session], key: str, default: dict[str, Any]) -> dict[str, Any]:
    with sessions() as session:
        row = session.get(SettingRow, key)
    stored = json.loads(row.value) if row else {}
    return {**default, **{k: v for k, v in stored.items() if k in default}}


def update(
    sessions: sessionmaker[Session], key: str, default: dict[str, Any], changes: dict[str, Any]
) -> dict[str, Any]:
    """Change some of a setting's values and return them as they were. Read, change and write
    are one transaction: with `get` and then `put`, two changes made at the same moment could
    each write back the other's old value."""
    with sessions.begin() as session:
        row = session.get(SettingRow, key)
        stored = json.loads(row.value) if row else {}
        before = {**default, **{k: v for k, v in stored.items() if k in default}}
        value = json.dumps({**before, **changes})
        if row is None:
            session.add(SettingRow(key=key, value=value))
        else:
            row.value = value
    return before


def put(sessions: sessionmaker[Session], key: str, values: dict[str, Any]) -> None:
    with sessions.begin() as session:
        row = session.get(SettingRow, key)
        if row is None:
            session.add(SettingRow(key=key, value=json.dumps(values)))
        else:
            row.value = json.dumps(values)
