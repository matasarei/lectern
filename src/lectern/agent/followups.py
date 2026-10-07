"""Follow-ups: tool calls the chat queued to run when an OCR session finishes (task.after,
or ocr.start's `then`), such as exporting the Document to ODT.

They live in the chat's state record beside its pins, so they survive a restart and the
model always sees them (in the pinned part of its context, never summarised). Running one
takes it out first, so it never runs twice.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lectern.projects.tables import ChatStateRow, now

MAX_FOLLOWUPS = 5  # per chat: a follow-up is a step the person agreed to, not a queue


class FollowUpError(ValueError):
    pass


@dataclass(frozen=True)
class FollowUp:
    id: str
    ocr_session_id: int  # the session it waits for
    tool: str  # a dotted tool name, "export.run"
    arguments: dict = field(default_factory=dict)
    label: str = ""  # for the person and the model: "export to ODT"


def _load(row: ChatStateRow | None) -> list[FollowUp]:
    return [FollowUp(**f) for f in json.loads(row.followups or "[]")] if row else []


def _row(session: Session, chat_id: int) -> ChatStateRow:
    row = session.get(ChatStateRow, chat_id)
    if row is None:
        row = ChatStateRow(session_id=chat_id)
        session.add(row)
    return row


def add(
    sessions: sessionmaker[Session],
    chat_id: int,
    *,
    ocr_session_id: int,
    tool: str,
    arguments: dict,
    label: str,
) -> FollowUp:
    with sessions.begin() as session:
        row = _row(session, chat_id)
        pending = _load(row)
        if len(pending) >= MAX_FOLLOWUPS:
            raise FollowUpError(f"{MAX_FOLLOWUPS} follow-ups are already waiting in this chat.")
        new = FollowUp(uuid.uuid4().hex[:8], ocr_session_id, tool, dict(arguments), label)
        row.followups = json.dumps([asdict(f) for f in [*pending, new]], ensure_ascii=False)
        row.updated_at = now()
        return new


def pending(sessions: sessionmaker[Session], chat_id: int) -> list[FollowUp]:
    with sessions() as session:
        return _load(session.get(ChatStateRow, chat_id))


def waiting_on(sessions: sessionmaker[Session], ocr_session_id: int) -> list[tuple[int, FollowUp]]:
    """Every chat's follow-ups for this session, as (chat id, follow-up), chats in order."""
    with sessions() as session:
        rows = session.scalars(select(ChatStateRow).order_by(ChatStateRow.session_id)).all()
        return [
            (row.session_id, f)
            for row in rows
            for f in _load(row)
            if f.ocr_session_id == ocr_session_id
        ]


def take(sessions: sessionmaker[Session], chat_id: int, followup_id: str) -> FollowUp | None:
    """Remove the follow-up and return it; None when it's already gone (so it runs once)."""
    with sessions.begin() as session:
        row = session.get(ChatStateRow, chat_id)
        pending_now = _load(row)
        found = next((f for f in pending_now if f.id == followup_id), None)
        if found is None:
            return None
        row.followups = json.dumps(
            [asdict(f) for f in pending_now if f.id != followup_id], ensure_ascii=False
        )
        row.updated_at = now()
        return found
