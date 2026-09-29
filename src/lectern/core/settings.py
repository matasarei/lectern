"""App-wide settings stored as JSON values in the settings table."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from lectern.core.tables import SettingRow

RUNTIME_KEY = "runtime"
RUNTIME_DEFAULTS: dict[str, Any] = {
    "mlx_enabled": False,  # MLX servers can't be protected with a key yet
    "when_models_dont_fit": "swap_between_phases",  # or "keep_one_loaded"
    "chat_during_job": "pause_job_after_current_item",  # or "chat_waits"
}


def get(sessions: sessionmaker[Session], key: str, default: dict[str, Any]) -> dict[str, Any]:
    with sessions() as session:
        row = session.get(SettingRow, key)
    stored = json.loads(row.value) if row else {}
    return {**default, **{k: v for k, v in stored.items() if k in default}}


def put(sessions: sessionmaker[Session], key: str, values: dict[str, Any]) -> None:
    with sessions.begin() as session:
        row = session.get(SettingRow, key)
        if row is None:
            session.add(SettingRow(key=key, value=json.dumps(values)))
        else:
            row.value = json.dumps(values)
