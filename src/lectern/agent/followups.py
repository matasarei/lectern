"""Follow-ups: tool calls the chat queued to run when an OCR session finishes (task.after,
or ocr.start's `then`), such as exporting the Document to ODT. A follow-up with no tool is a
notice: when the session ends, the chat gets a plain message saying so, and no model runs.

They live in the chat's state record beside its pins, so they survive a restart and the
model always sees them (in the pinned part of its context, never summarised). Running one
takes it out first, so it never runs twice.
"""

from __future__ import annotations

import inspect
import json
import uuid
from dataclasses import asdict, dataclass, field, replace

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult
from lectern.projects.tables import ChatStateRow, OcrSessionRow, SessionRow, now

MAX_FOLLOWUPS = 5  # per chat, notices aside: a follow-up is a step the person agreed to


class FollowUpError(ValueError):
    pass


@dataclass(frozen=True)
class FollowUp:
    id: str
    ocr_session_id: int  # the session it waits for
    tool: str  # a dotted tool name, "export.run"; "" for a notice (no tool, no model)
    arguments: dict = field(default_factory=dict)
    label: str = ""  # for the person and the model: "after OCR · scan.pdf: export to ODT"
    what: str = ""  # the label's own part, "export to ODT" (older records: "")
    held: bool = False  # waiting for the Document to be clear, and the chat was told so


def is_notice(follow: FollowUp) -> bool:
    return not follow.tool


def redundant_notices(waiting: list[tuple[int, FollowUp]]) -> list[tuple[int, FollowUp]]:
    """The notices a chat doesn't need: one of its follow-ups for the same session runs a
    tool, and the model reports on that, so a notice too would say it twice."""
    working = {(chat, f.ocr_session_id) for chat, f in waiting if not is_notice(f)}
    return [
        (chat, f) for chat, f in waiting if is_notice(f) and (chat, f.ocr_session_id) in working
    ]


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
    what: str = "",
) -> FollowUp:
    with sessions.begin() as session:
        row = _row(session, chat_id)
        pending = _load(row)
        # A notice isn't a step the person agreed to, only a message: it doesn't count
        steps = sum(1 for f in pending if not is_notice(f))
        if tool and steps >= MAX_FOLLOWUPS:
            raise FollowUpError(f"{MAX_FOLLOWUPS} follow-ups are already waiting in this chat.")
        new = FollowUp(uuid.uuid4().hex[:8], ocr_session_id, tool, dict(arguments), label, what)
        row.followups = json.dumps([asdict(f) for f in [*pending, new]], ensure_ascii=False)
        row.updated_at = now()
        return new


def mark_held(sessions: sessionmaker[Session], chat_id: int, ids: list[str]) -> None:
    """These follow-ups wait for the Document to be clear, and the chat has been told."""
    with sessions.begin() as session:
        row = session.get(ChatStateRow, chat_id)
        marked = [replace(f, held=True) if f.id in ids else f for f in _load(row)]
        row.followups = json.dumps([asdict(f) for f in marked], ensure_ascii=False)
        row.updated_at = now()


def hold_text(title: str, summary: dict, whats: list[str]) -> str:
    """The chat's message while a follow-up waits for the Document to be clear: what's
    still open, what waits for it, and the two ways on."""
    count = summary["open_doubts"]
    pending_parts, fixes = [], []
    if count:
        pending_parts.append(f"{count} open question{'s' if count != 1 else ''}")
        fixes.append("answer the questions in the Document")
    failed = summary["failed_pages"]
    if failed:
        pages = ", ".join(str(n) for n in failed)
        word = "page" if len(failed) == 1 else "pages"
        pending_parts.append(f"{word} {pages} couldn't be cleaned")
        fixes.append(f"clean {word} {pages} again in the OCR tab")
    waiting = ", ".join(whats)
    waiting = waiting[:1].upper() + waiting[1:]
    return (
        f"OCR · {title} isn't finished yet: {'; '.join(pending_parts)}. {waiting} waits for it: "
        f"{' and '.join(fixes)}, or export now as it is."
    )


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


def stale(sessions: sessionmaker[Session], chat_id: int) -> list[tuple[FollowUp, str, str]]:
    """The chat's follow-ups whose OCR session is over, as (follow-up, status, notice): done
    (run it), failed or gone (drop it). Their session's signal was missed if they're still
    here, e.g. the app stopped first."""
    found = []
    with sessions() as session:
        for follow in _load(session.get(ChatStateRow, chat_id)):
            ocr = session.get(OcrSessionRow, follow.ocr_session_id)
            chat = session.get(SessionRow, follow.ocr_session_id)
            title = chat.title if chat else ""
            if ocr is None:
                found.append((follow, "failed", "The OCR session is gone."))
            elif ocr.status == "done":
                found.append((follow, "done", f"OCR · {title} finished."))
            elif ocr.status == "failed":
                found.append(
                    (follow, "failed", f"OCR · {title} stopped: {ocr.error or 'it failed'}.")
                )
    return found


def notice_refs(sessions: sessionmaker[Session], ocr_session_id: int) -> list[dict]:
    """What a notice points at: the session, and its document once there is one."""
    with sessions() as session:
        chat = session.get(SessionRow, ocr_session_id)
        ocr = session.get(OcrSessionRow, ocr_session_id)
        if chat is None or ocr is None:
            return []
        refs = [{"kind": "session", "id": chat.id, "name": chat.title, "ref": None}]
        if ocr.document_artifact_id is not None:
            refs.append({"kind": "artifact", "id": ocr.document_artifact_id,
                         "name": f"{ocr.folder}/document.md", "ref": None})  # fmt: skip
        return refs


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


def _arguments(params: AfterParams, chat: SessionRow, ocr: OcrSessionRow) -> dict:
    """The follow-up's arguments. An export after OCR exports the session's document, whose
    name is its folder: the model can't know that folder, and leaves the name out or names
    the scan itself (seen: "name: Field required", then the turn gave up)."""
    arguments = dict(params.arguments)
    if params.tool.replace(".", "_") == "export_run":
        named = str(arguments.get("name") or "").strip()
        if not named or named.casefold() in (chat.title.casefold(), ocr.folder.casefold()):
            arguments["name"] = f"{ocr.folder}/"
        if not arguments.get("format"):  # a bare "Field required" left the 4B guessing
            raise ToolError(
                'Say which format to export to, e.g. arguments={"format": "docx"} '
                "(export_formats lists them)."
            )
    return arguments


def _checked(
    ctx: ToolContext, params: AfterParams, *, finished_ok: bool = False
) -> tuple[SessionRow, AgentTool, dict, bool]:
    """The session, the tool, its arguments and whether the session has finished, or a
    ToolError before any card: a follow-up never runs a tool that would have asked, and never
    waits for a session that won't finish. A finished session is refused, unless
    `finished_ok` (the card was answered after it finished)."""
    chat, ocr = _ocr_session(ctx, params.session)
    wire = params.tool.replace(".", "_")
    for waiting in pending(ctx.sessions, ctx.session_id):
        if waiting.ocr_session_id == chat.id and waiting.tool.replace(".", "_") == wire:
            raise ToolError(f"Already queued: {waiting.label}. Nothing more to do now.")
    finished = ocr.status == "done"
    if finished and not finished_ok:
        raise ToolError(f"{chat.title} has already finished: run {params.tool} now instead.")
    tool = ctx.registry.get(wire) if ctx.registry else None
    if tool is None or tool.name == "task.after":
        raise ToolError(f"{params.tool} can't be a follow-up: there's no such tool here.")
    if tool.confirm:
        raise ToolError(f"{tool.name} asks the person first, so it can't run unattended.")
    arguments = _arguments(params, chat, ocr)
    tool.validate(arguments)  # ToolError with what's wrong, now rather than later
    return chat, tool, arguments, finished


def _after_question(ctx: ToolContext, params: AfterParams) -> str:
    chat, tool, _, _ = _checked(ctx, params)
    return f"After OCR · {chat.title} finishes: {params.label} ({tool.name})?"


async def _after(ctx: ToolContext, params: AfterParams) -> ToolResult:
    chat, tool, arguments, finished = await run_in_threadpool(
        lambda: _checked(ctx, params, finished_ok=True)
    )
    if finished and await run_in_threadpool(_clear, ctx, chat.id):
        # Asked while the session ran, answered after it finished: what the person allowed
        # for then runs now (seen: the tool refused, and the 4B had to find export_run itself).
        validated = tool.validate(arguments)
        if inspect.iscoroutinefunction(tool.run):
            result = await tool.run(ctx, validated)
        else:
            result = await run_in_threadpool(tool.run, ctx, validated)
        return replace(
            result, text=f"{chat.title} had already finished, so it ran now: {result.text}"
        )
    queued = await run_in_threadpool(_queue, ctx, params, chat, tool, arguments)
    if finished:  # its Document isn't final: it waits, as an export queued before would
        return replace(
            queued,
            text=f"{chat.title} has finished, but its Document has open questions or a page "
            f"not cleaned. Queued: {params.label}. It runs once they're resolved, or the "
            "person can export now as it is.",
        )
    return queued


def _clear(ctx: ToolContext, session_id: int) -> bool:
    """Whether the session's Document is final (done, every page cleaned, no question open).
    Without the OCR jobs (tests) it counts as final, as the chat's follow-ups do."""
    if ctx.ocr_jobs is None:
        return True
    return ctx.ocr_jobs.summary(ctx.project, session_id)["clear"]


def _queue(
    ctx: ToolContext, params: AfterParams, chat: SessionRow, tool: AgentTool, arguments: dict
) -> ToolResult:
    try:
        added = add(ctx.sessions, ctx.session_id, ocr_session_id=chat.id, tool=tool.name,
                    arguments=arguments, label=f"after OCR · {chat.title}: {params.label}",
                    what=params.label)  # fmt: skip
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
