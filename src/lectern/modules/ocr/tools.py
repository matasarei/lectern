"""The OCR module's chat tools (blueprint, "Tools"):
- ocr.start: an OCR session for a PDF or image, opened in a new tab. It asks first, with a
  card giving the pages, the time it takes and whether the chat model is unloaded meanwhile,
  and it can queue an export for when the session finishes ("convert this scan to ODT").
- ocr.note: a page's note, which cleanup follows ("on page 4, ignore the handwriting").
"""

from __future__ import annotations

import contextlib

from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from lectern.agent import followups, model_download
from lectern.agent.core_tools import _artifact_by_name
from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult
from lectern.catalog import catalog
from lectern.catalog.catalog import CatalogEntry
from lectern.modules.ocr import store as ocr_store
from lectern.modules.ocr.jobs import page_dict
from lectern.modules.ocr.pages import IMAGE_SUFFIXES, NotAPageSource, page_count
from lectern.projects import files
from lectern.projects.tables import ArtifactRow, OcrSessionRow, SessionRow

# Reading plus cleanup, per page, on the minimum tier (docs/acceptance/m3a.md: ~1.5 s + ~12 s)
SECONDS_PER_PAGE = 15
NOTE_LIMIT = 5000


class StartParams(BaseModel):
    name: str = Field(min_length=1, description="The PDF or image, as in project.list.")
    then_export: str | None = Field(
        default=None,
        description='A format to export the finished document to, e.g. "odt" (see '
        "export.formats). The export runs when the session finishes.",
    )
    keep_layout: bool = Field(
        default=False,
        description="true: read a PDF that has text with OCR anyway, to keep its tables and "
        "layout, or to read the pages that are scans.",
    )


def _source(ctx: ToolContext, name: str, *, keep_layout: bool = False) -> ArtifactRow:
    with ctx.sessions() as session:
        row = _artifact_by_name(session, ctx, name, partial_ok=False)
        session.expunge(row)
    suffix = "." + row.path.rsplit(".", 1)[-1].lower() if "." in row.path else ""
    if suffix != ".pdf" and suffix not in IMAGE_SUFFIXES:
        raise ToolError(f"{row.path} isn't a PDF or an image, so it can't be read as pages.")
    if row.text_status == "indexed" and not keep_layout:
        raise ToolError(
            f"{row.path} has text: read it with document_read, or export it directly with "
            "export_run; no OCR is needed. To keep its tables and layout, or to read its "
            "scanned pages, call ocr_start with keep_layout=true."
        )
    return row


def _ocr_model(ctx: ToolContext) -> CatalogEntry | None:
    """The model an OCR session reads pages with on this computer: the "ocr" role's default.
    A page later marked handwritten uses "ocr.hand", which this doesn't fetch."""
    if ctx.runtime is None:
        return None
    return catalog.load().defaults_for(ctx.runtime.tier).get("ocr")


def _start_question(ctx: ToolContext, params: StartParams) -> str:
    row = _source(ctx, params.name, keep_layout=params.keep_layout)
    try:
        pages = page_count(files.inside(ctx.project.root, row.path))
    except NotAPageSource as exc:
        raise ToolError(str(exc)) from None
    minutes = max(1, round(pages * SECONDS_PER_PAGE / 60))
    why = (
        "This PDF has text; OCR reads it again to keep its tables and layout. "
        if row.text_status == "indexed"
        else ""
    )
    question = (
        f"Start an OCR session for {_name(row)}? {why}{pages} page{'s' if pages != 1 else ''} · "
        f"about {minutes} minute{'s' if minutes != 1 else ''} on this PC · opens in a new tab."
    )
    entry = _ocr_model(ctx)
    if entry is not None and not model_download.on_disk(ctx, entry):
        # The runtime can't size a model that isn't on disk; the card says what happens first.
        question += (
            f" It downloads {entry.title} first ({entry.size / 1e9:.1f} GB, {entry.licence})."
        )
        if ctx.runtime.resident_bytes() > 0:  # likely a swap, though it can't be sized yet
            question += (
                " The chat model may be unloaded while pages are read, so replies may wait "
                "until then."
            )
    elif ctx.runtime is not None and ctx.runtime.needs_room("ocr"):
        question += " The chat model is unloaded while pages are read, so replies wait until then."
    if params.then_export:
        question += f" When it finishes: export to {params.then_export.upper()}."
    return question


async def _start(ctx: ToolContext, params: StartParams) -> ToolResult:
    if ctx.ocr_jobs is None:
        raise ToolError("OCR sessions can't be started here.")
    row = await run_in_threadpool(lambda: _source(ctx, params.name, keep_layout=params.keep_layout))
    entry = _ocr_model(ctx)
    if entry is not None and not model_download.on_disk(ctx, entry):
        # The person allowed it on the start card; a session started now would only fail.
        state = await model_download.download(ctx, entry)
        if state.status == "failed":
            raise ToolError(f"{entry.title}'s download failed: {state.error}")
        if state.status == "paused":
            raise ToolError(
                f"{entry.title}'s download was stopped before it finished; starting the OCR "
                "session again resumes it."
            )
    try:
        chat, ocr = await run_in_threadpool(
            lambda: ocr_store.create_session(
                ctx.project.root, ctx.sessions, owner_id=ctx.user.id, artifact_id=row.id
            )
        )
    except ocr_store.CannotOpen as exc:
        raise ToolError(str(exc)) from None
    await ctx.ocr_jobs.start(ctx.project, chat.id)
    # The folder is the name to export later (export_run or task_after); the model can't
    # guess it from the scan's name.
    text = (
        f"Started an OCR session for {_name(row)}; it's in a new tab, and this chat shows its "
        f'progress. Its document will be "{ocr.folder}/" when it finishes.'
    )
    if params.then_export:
        fmt = params.then_export.lower().lstrip(".")
        await run_in_threadpool(
            lambda: followups.add(
                ctx.sessions, ctx.session_id, ocr_session_id=chat.id, tool="export.run",
                arguments={"name": f"{ocr.folder}/", "format": fmt},
                label=f"after OCR · {_name(row)}: export to {fmt.upper()}",
                what=f"export to {fmt.upper()}",
            )
        )  # fmt: skip
        # Said so a small model doesn't try to queue it again (seen: task.after, 7 times).
        text += (
            f" The export to {fmt.upper()} is queued: it runs by itself when the session "
            "finishes, so there's nothing more to do now."
        )
    else:
        # No export asked: the chat says when it's done, without a model turn. A task.after
        # export queued later makes this notice redundant; it's dropped then.
        with contextlib.suppress(followups.FollowUpError):  # 5 waiting: the line still shows
            await run_in_threadpool(
                lambda: followups.add(
                    ctx.sessions, ctx.session_id, ocr_session_id=chat.id, tool="", arguments={},
                    label=f"after OCR · {_name(row)}: say it's done",
                )
            )  # fmt: skip
    tab = {"kind": "session", "id": chat.id}
    return ToolResult(
        text,
        refs=[{**tab, "name": chat.title, "ref": None}],
        # Added without taking focus: the person stays in the chat, where any next card (an
        # export to queue) waits for them, and the reply card shows the session's progress.
        action={"type": "open_tab", "tab": tab, "focus": False},
    )


START = AgentTool(
    name="ocr.start",
    description="Read a scanned PDF or image (no text layer) as an OCR session, opened in a "
    "new tab; keep_layout=true reads a PDF with text too, to keep its tables and layout. Asks "
    "the person first. then_export queues an export for when it finishes.",
    params=StartParams,
    run=_start,
    confirm=True,
    confirm_text=_start_question,
    max_tokens=200,
)


class NoteParams(BaseModel):
    session: str = Field(min_length=1, description="The OCR session's title or folder.")
    page: int = Field(ge=1, description="The page number, from 1.")
    note: str = Field(max_length=NOTE_LIMIT, description="The note; empty removes it.")


def _ocr_session(ctx: ToolContext, name: str) -> tuple[SessionRow, OcrSessionRow]:
    wanted = name.strip().rstrip("/").casefold()
    with ctx.sessions() as session:
        rows = session.execute(
            select(SessionRow, OcrSessionRow)
            .join(OcrSessionRow, OcrSessionRow.session_id == SessionRow.id)
            .where(SessionRow.owner_id == ctx.user.id, SessionRow.kind == "ocr")
        ).all()
    found = [
        (chat, ocr) for chat, ocr in rows
        if wanted in (chat.title.casefold(), ocr.folder.casefold())
    ]  # fmt: skip
    if not found:
        raise ToolError(f"No OCR session called {name!r}. Use project.list to look.")
    return found[-1]


def _note(ctx: ToolContext, params: NoteParams) -> ToolResult:
    chat, ocr = _ocr_session(ctx, params.session)
    page = ocr_store.set_note(ctx.sessions, chat.id, params.page, params.note.strip())
    if page is None:
        pages = ocr_store.pages(ctx.sessions, chat.id)
        raise ToolError(f"{chat.title} has pages 1–{len(pages)}." if pages else
                        f"{chat.title} has no pages yet; try again in a moment.")  # fmt: skip
    if ctx.ocr_jobs is not None:
        ctx.ocr_jobs.channel((ctx.project.id, chat.id)).publish({"type": "page", **page_dict(page)})
    text = f"Saved the note for page {params.page} of {chat.title}."
    if page.clean_status == "done":
        text += (
            " The page is already cleaned: the note applies when it's cleaned again (the OCR "
            "tab offers that)."
        )
    return ToolResult(
        text, refs=[{"kind": "session", "id": chat.id, "name": chat.title, "ref": None}]
    )


NOTE = AgentTool(
    name="ocr.note",
    description='Set a page note in an OCR session ("on page 4, ignore the handwriting"). '
    "Cleanup follows it.",
    params=NoteParams,
    run=_note,
    max_tokens=200,
)


def _name(row: ArtifactRow) -> str:
    return row.path.rsplit("/", 1)[-1]
