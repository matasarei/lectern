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

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult
from lectern.projects.tables import ChatStateRow, OcrSessionRow, SessionRow, now

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


# --- task.after: queue a follow-up from the chat ------------------------------------------


class AfterParams(BaseModel):
    session: str = Field(min_length=1, description="The OCR session's title or folder.")
    tool: str = Field(min_length=1, description='The tool to run then, e.g. "export_run".')
    arguments: dict = Field(default_factory=dict, description="Its arguments.")
    label: str = Field(min_length=1, max_length=120, description='In words: "export to ODT".')


def _ocr_session(ctx: ToolContext, name: str) -> tuple[SessionRow, OcrSessionRow]:
    wanted = name.strip().rstrip("/").casefold()
    with ctx.sessions() as session:
        rows = session.execute(
            select(SessionRow, OcrSessionRow)
            .join(OcrSessionRow, OcrSessionRow.session_id == SessionRow.id)
            .where(SessionRow.owner_id == ctx.user.id, SessionRow.kind == "ocr")
        ).all()
    found = [(s, o) for s, o in rows if wanted in (s.title.casefold(), o.folder.casefold())]
    if not found:
        raise ToolError(f"No OCR session called {name!r}. Use project.list to look.")
    return found[-1]


def _checked(ctx: ToolContext, params: AfterParams) -> tuple[SessionRow, AgentTool]:
    """The session and the tool, or a ToolError before any card: a follow-up never runs a
    tool that would have asked, and never waits for a session that won't finish."""
    chat, ocr = _ocr_session(ctx, params.session)
    if ocr.status == "done":
        raise ToolError(f"{chat.title} has already finished: run {params.tool} now instead.")
    tool = ctx.registry.get(params.tool.replace(".", "_")) if ctx.registry else None
    if tool is None or tool.name == "task.after":
        raise ToolError(f"{params.tool} can't be a follow-up: there's no such tool here.")
    if tool.confirm:
        raise ToolError(f"{tool.name} asks the person first, so it can't run unattended.")
    tool.validate(params.arguments)  # ToolError with what's wrong, now rather than later
    return chat, tool


def _after_question(ctx: ToolContext, params: AfterParams) -> str:
    chat, tool = _checked(ctx, params)
    return f"After OCR · {chat.title} finishes: {params.label} ({tool.name})?"


def _after(ctx: ToolContext, params: AfterParams) -> ToolResult:
    chat, tool = _checked(ctx, params)
    try:
        added = add(ctx.sessions, ctx.session_id, ocr_session_id=chat.id, tool=tool.name,
                    arguments=params.arguments,
                    label=f"after OCR · {chat.title}: {params.label}")  # fmt: skip
    except FollowUpError as exc:
        raise ToolError(str(exc)) from None
    return ToolResult(f"Queued: {added.label}. It runs when the session finishes.")


TASK_AFTER = AgentTool(
    name="task.after",
    description="Queue a tool call (e.g. export_run) to run when an OCR session finishes. "
    "Asks the person first.",
    params=AfterParams,
    run=_after,
    confirm=True,
    confirm_text=_after_question,
    max_tokens=150,
)
