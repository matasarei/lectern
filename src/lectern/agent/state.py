"""A chat's state record: pinned items and the summary of older turns.

Pinned items are what the person said must hold (an instruction, a confirmed fact). They go
to the model in every call, word for word, and are never summarised. The summary stands in
for the turns up to `summary_upto`; the transcript keeps those turns unchanged.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from lectern.agent.followups import FollowUp
from lectern.projects.tables import ChatStateRow, now

MAX_PINS = 20
PIN_CHARS = 300
PIN_KINDS = ("instruction", "fact")


class PinError(ValueError):
    pass


@dataclass(frozen=True)
class Pin:
    id: str
    kind: str  # "instruction" | "fact"
    text: str


@dataclass
class ChatState:
    pinned: list[Pin] = field(default_factory=list)
    summary: str = ""
    summary_upto: int = 0  # the last message id the summary covers; 0 = no summary
    summary_version: int = 0
    followups: list[FollowUp] = field(default_factory=list)  # waiting for an OCR session


def load(sessions: sessionmaker[Session], session_id: int) -> ChatState:
    with sessions() as session:
        row = session.get(ChatStateRow, session_id)
    return _state(row)


def _state(row: ChatStateRow | None) -> ChatState:
    if row is None:
        return ChatState()
    return ChatState(
        pinned=[Pin(**p) for p in json.loads(row.pinned)],
        summary=row.summary,
        summary_upto=row.summary_upto,
        summary_version=row.summary_version,
        followups=[FollowUp(**f) for f in json.loads(row.followups or "[]")],
    )


def _row(session: Session, session_id: int) -> ChatStateRow:
    row = session.get(ChatStateRow, session_id)
    if row is None:
        row = ChatStateRow(session_id=session_id)
        session.add(row)
    return row


def pin(sessions: sessionmaker[Session], session_id: int, text: str, kind: str) -> Pin:
    text = " ".join(text.split())
    if not text:
        raise PinError("There's nothing to pin.")
    if len(text) > PIN_CHARS:
        raise PinError(f"A pinned item is at most {PIN_CHARS} characters; shorten it.")
    if kind not in PIN_KINDS:
        raise PinError(f"kind must be one of {', '.join(PIN_KINDS)}.")
    with sessions.begin() as session:
        row = _row(session, session_id)
        pins = json.loads(row.pinned or "[]")
        for existing in pins:
            if existing["text"].casefold() == text.casefold():
                return Pin(**existing)  # already pinned
        if len(pins) >= MAX_PINS:
            raise PinError(f"At most {MAX_PINS} items can be pinned; unpin one first.")
        new = {"id": uuid.uuid4().hex[:8], "kind": kind, "text": text}
        row.pinned = json.dumps([*pins, new], ensure_ascii=False)
        row.updated_at = now()
        return Pin(**new)


def unpin(sessions: sessionmaker[Session], session_id: int, key: str) -> Pin:
    """Remove a pin by its id, or by its text (case aside). Raises PinError if none matches."""
    with sessions.begin() as session:
        row = _row(session, session_id)
        pins = json.loads(row.pinned or "[]")
        match = next(
            (p for p in pins if p["id"] == key or p["text"].casefold() == key.casefold().strip()),
            None,
        )
        if match is None:
            raise PinError("Nothing pinned matches that.")
        row.pinned = json.dumps([p for p in pins if p is not match], ensure_ascii=False)
        row.updated_at = now()
        return Pin(**match)


def save_summary(
    sessions: sessionmaker[Session], session_id: int, summary: str, upto: int
) -> ChatState:
    with sessions.begin() as session:
        row = _row(session, session_id)
        row.summary = summary
        row.summary_upto = upto
        row.summary_version = (row.summary_version or 0) + 1
        row.updated_at = now()
        session.flush()
        return _state(row)
