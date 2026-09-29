"""The core's agent tools: find and read what's in the project, write new files, move files
to the trash (after asking), and check or open sessions.

Files are named the way the person sees them in Overview. A name that matches more than one
file, or none, is an error listing the candidates, so the model can pick again.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lectern.agent import state
from lectern.agent.tools import AgentTool, Registry, ToolContext, ToolError, ToolResult, page
from lectern.projects import files, search
from lectern.projects.tables import ArtifactRow, MessageRow, SessionRow
from lectern.projects.text import TEXT_EXTENSIONS, Chunk

WRITE_LIMIT = 200_000  # characters in one artifact.write


def _artifacts(session: Session, ctx: ToolContext) -> list[ArtifactRow]:
    return [r for r in files.live(session) if r.owner_id == ctx.user.id]


def _chats(session: Session, ctx: ToolContext) -> list[SessionRow]:
    return list(
        session.scalars(
            select(SessionRow).where(SessionRow.owner_id == ctx.user.id).order_by(SessionRow.id)
        )
    )


def _artifact_by_name(
    session: Session, ctx: ToolContext, name: str, *, partial_ok: bool = True
) -> ArtifactRow:
    """The file with this name: exact, then ignoring case, then (for tools that only read) a
    unique partial match. Tools that ask first pass partial_ok=False, so the card names the
    very file that will change."""
    rows = _artifacts(session, ctx)
    exact = [r for r in rows if r.path == name]
    if len(exact) == 1:
        return exact[0]
    folded = [r for r in rows if r.path.casefold() == name.casefold()]
    if len(folded) == 1:
        return folded[0]
    if not partial_ok:
        raise ToolError(f"No file called exactly {name!r}. Use project.find to get its full name.")
    partial = [r for r in rows if name.casefold() in r.path.casefold()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        names = ", ".join(r.path for r in partial[:10])
        raise ToolError(f"{name!r} matches several files: {names}. Use the full name.")
    raise ToolError(f"No file called {name!r}. Use project.find or project.list to look.")


def _describe(row: ArtifactRow, titles: dict[int, str], names: dict[int, str]) -> str:
    text = {
        "indexed": "has text",
        "none": row.text_note or "no text",
        "failed": "text unreadable",
    }[row.text_status]
    origin = {
        "import": "imported",
        "session": f"from chat {titles.get(row.from_id or 0, '?')!r}",
        "artifact": f"from {names.get(row.from_id or 0, '?')}",
    }[row.from_kind]
    return f"{row.path} · file · {_size(row.size)} · {text} · {origin}"


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n} B"


def _file_ref(row: ArtifactRow, ref: str | None = None) -> dict:
    return {"kind": "artifact", "id": row.id, "name": row.path, "ref": ref}


# --- project.list / project.find -----------------------------------------------------------


class ListParams(BaseModel):
    kind: Literal["all", "sessions", "files"] = Field(
        default="all", description="What to list: everything, only sessions, or only files."
    )
    cursor: str | None = Field(default=None, description="From a previous, cut-off result.")


def _lines(session: Session, ctx: ToolContext, kind: str) -> list[str]:
    chats = _chats(session, ctx)
    rows = _artifacts(session, ctx)
    titles = {c.id: c.title for c in chats}
    names = {r.id: r.path for r in rows}
    lines = []
    if kind in ("all", "sessions"):
        lines += [f"{c.title} · chat session" for c in chats]
    if kind in ("all", "files"):
        lines += [_describe(r, titles, names) for r in rows]
    return lines


def _list(ctx: ToolContext, params: ListParams) -> ToolResult:
    with ctx.sessions() as session:
        lines = _lines(session, ctx, params.kind)
    if not lines:
        return ToolResult("The project is empty.")
    text, cursor = page("\n".join(lines), params.cursor, LIST.max_tokens)
    return ToolResult(text, cursor=cursor)


LIST = AgentTool(
    name="project.list",
    description="List the sessions and files in this project, with where each file came from.",
    params=ListParams,
    run=_list,
)


class FindParams(BaseModel):
    query: str = Field(min_length=1, max_length=200, description="Part of a name to look for.")
    kind: Literal["all", "sessions", "files"] = "all"


def _find(ctx: ToolContext, params: FindParams) -> ToolResult:
    needle = params.query.casefold()
    words = needle.split()
    with ctx.sessions() as session:
        chats = _chats(session, ctx)
        rows = _artifacts(session, ctx)
        titles = {c.id: c.title for c in chats}
        names = {r.id: r.path for r in rows}

    def hit(name: str) -> bool:
        folded = name.casefold()
        return needle in folded or all(w in folded for w in words)

    found: list[str] = []
    refs: list[dict] = []
    if params.kind in ("all", "sessions"):
        for chat in chats:
            if hit(chat.title):
                found.append(f"{chat.title} · chat session")
                refs.append({"kind": "session", "id": chat.id, "name": chat.title, "ref": None})
    if params.kind in ("all", "files"):
        for row in rows:
            if hit(row.path):
                found.append(_describe(row, titles, names))
                refs.append(_file_ref(row))
    if not found:
        return ToolResult(f"Nothing in this project matches {params.query!r}.")
    text, _ = page("\n".join(found), None, FIND.max_tokens)
    return ToolResult(text, refs=refs[:20])


FIND = AgentTool(
    name="project.find",
    description="Find sessions and files in this project by name (case-insensitive, partial).",
    params=FindParams,
    run=_find,
)


# --- project.search ------------------------------------------------------------------------


class SearchParams(BaseModel):
    query: str = Field(
        min_length=1, max_length=300, description="Words to find in the text of the files."
    )


def _search(ctx: ToolContext, params: SearchParams) -> ToolResult:
    with ctx.sessions() as session:
        rows = {r.id: r for r in _artifacts(session, ctx)}
        hits = search.search(session, params.query, limit=10, artifact_ids=list(rows))
    if not hits:
        return ToolResult(f"No text in this project matches {params.query!r}.")
    lines = [
        f"{rows[h.artifact_id].path} {h.ref}{f' ({h.heading})' if h.heading else ''}: {h.snippet}"
        for h in hits
    ]
    text, _ = page(
        "\n".join(lines) + "\nRead a block with document.read(name, refs=[...]).",
        None,
        SEARCH.max_tokens,
    )
    return ToolResult(text, refs=[_file_ref(rows[h.artifact_id], h.ref) for h in hits])


SEARCH = AgentTool(
    name="project.search",
    description=(
        "Search the text of every file in this project. Returns file names and block "
        "references such as #c3, not whole files."
    ),
    params=SearchParams,
    run=_search,
)


# --- document.outline / document.read ------------------------------------------------------


class OutlineParams(BaseModel):
    name: str = Field(min_length=1, description="The file's name, as in project.list.")


def _chunks_of(ctx: ToolContext, name: str) -> tuple[ArtifactRow, list[Chunk]]:
    with ctx.sessions() as session:
        row = _artifact_by_name(session, ctx, name)
        chunks = search.chunks(session, row.id)
    if not chunks:
        raise ToolError(f"{row.path} has no text to read ({row.text_note or 'none found'}).")
    return row, chunks


def _outline(ctx: ToolContext, params: OutlineParams) -> ToolResult:
    row, chunks = _chunks_of(ctx, params.name)
    lines = [
        f"{c.ref} {c.heading or c.body[:60].replace(chr(10), ' ')} ({len(c.body)} chars)"
        for c in chunks
    ]
    text, _ = page(f"{row.path}: {len(chunks)} blocks\n" + "\n".join(lines), None, 1200)
    return ToolResult(text, refs=[_file_ref(row)])


OUTLINE = AgentTool(
    name="document.outline",
    description="Show a file's outline: its blocks (#c1, #c2…) with their headings.",
    params=OutlineParams,
    run=_outline,
    max_tokens=1200,
)


class ReadParams(BaseModel):
    name: str = Field(min_length=1, description="The file's name, as in project.list.")
    refs: list[str] | None = Field(
        default=None,
        description='Blocks to read, e.g. ["#c2", "#c3"]. Omit to read from the start.',
    )
    heading: str | None = Field(default=None, description="Read the blocks under this heading.")
    cursor: str | None = Field(default=None, description="From a previous, cut-off result.")


def _read(ctx: ToolContext, params: ReadParams) -> ToolResult:
    row, chunks = _chunks_of(ctx, params.name)
    chosen = chunks
    if params.refs:
        wanted = {r.strip().lstrip("#").lower() for r in params.refs}
        chosen = [c for c in chunks if f"c{c.number}" in wanted]
        if not chosen:
            raise ToolError(f"{row.path} has blocks #c1–#c{len(chunks)}; none of {params.refs}.")
    elif params.heading:
        wanted_heading = params.heading.casefold()
        chosen = [c for c in chunks if wanted_heading in c.heading.casefold()]
        if not chosen:
            headings = ", ".join(sorted({c.heading for c in chunks if c.heading})[:15])
            raise ToolError(
                f"No heading like {params.heading!r} in {row.path}. Headings: {headings}"
            )
    body = "\n\n".join(
        f"[{row.path} {c.ref}{f' — {c.heading}' if c.heading else ''}]\n{c.body}" for c in chosen
    )
    text, cursor = page(body, params.cursor, READ.max_tokens)
    return ToolResult(text, refs=[_file_ref(row, c.ref) for c in chosen[:20]], cursor=cursor)


READ = AgentTool(
    name="document.read",
    description=(
        "Read a file's text, exactly as written: specific blocks, the blocks under a heading, "
        "or from the start. Long results come in pages with a cursor."
    ),
    params=ReadParams,
    run=_read,
    max_tokens=1500,
)


# --- artifact.write / artifact.trash -------------------------------------------------------


class WriteParams(BaseModel):
    name: str = Field(
        min_length=1, max_length=180, description='File name with extension, e.g. "reply.md".'
    )
    text: str = Field(max_length=WRITE_LIMIT, description="The whole content of the new file.")


def _write(ctx: ToolContext, params: WriteParams) -> ToolResult:
    suffix = "." + params.name.rsplit(".", 1)[-1].lower() if "." in params.name else ""
    if suffix not in TEXT_EXTENSIONS:
        raise ToolError("Only text files can be written, e.g. .md, .txt, .csv, .json.")
    try:
        with ctx.sessions.begin() as session:
            row = files.write_text(
                ctx.project.root,
                session,
                owner_id=ctx.user.id,
                name=params.name,
                text=params.text,
                origin=files.Origin("session", id=ctx.session_id),
            )
    except files.BadName as exc:
        raise ToolError(str(exc)) from None
    requested = files.safe_name(params.name)
    renamed = f" ({requested} already existed)" if row.path != requested else ""
    return ToolResult(f"Saved as {row.path}{renamed}, {_size(row.size)}.", refs=[_file_ref(row)])


WRITE = AgentTool(
    name="artifact.write",
    description=(
        "Save text you wrote as a new file in the project. Never replaces a file: a taken "
        "name gets a number."
    ),
    params=WriteParams,
    run=_write,
    max_tokens=200,
)


class TrashParams(BaseModel):
    name: str = Field(min_length=1, description="The file's name, as in project.list.")


def _trash_question(ctx: ToolContext, params: TrashParams) -> str:
    with ctx.sessions() as session:
        row = _artifact_by_name(session, ctx, params.name, partial_ok=False)
    return f"Move {row.path} to the trash?"


def _trash(ctx: ToolContext, params: TrashParams) -> ToolResult:
    with ctx.sessions.begin() as session:
        row = _artifact_by_name(session, ctx, params.name, partial_ok=False)
        files.trash(ctx.project.root, session, row.id)
        name = row.path
    return ToolResult(
        f"Moved {name} to the trash. It's kept there for {files.TRASH_DAYS} days.",
        refs=[{"kind": "artifact", "id": row.id, "name": name, "ref": None}],
    )


TRASH = AgentTool(
    name="artifact.trash",
    description="Move a file to the project's trash. Always asks the person first.",
    params=TrashParams,
    run=_trash,
    confirm=True,
    confirm_text=_trash_question,
    max_tokens=100,
)


# --- session.status / tab.open -------------------------------------------------------------


class SessionParams(BaseModel):
    title: str = Field(min_length=1, description="The session's title, as in project.list.")


def _session_by_title(session: Session, ctx: ToolContext, title: str) -> SessionRow:
    chats = _chats(session, ctx)
    exact = [c for c in chats if c.title.casefold() == title.casefold()]
    matches = exact or [c for c in chats if title.casefold() in c.title.casefold()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ToolError(
            f"{title!r} matches several sessions: {', '.join(c.title for c in matches)}."
        )
    raise ToolError(f"No session called {title!r}.")


def _status(ctx: ToolContext, params: SessionParams) -> ToolResult:
    with ctx.sessions() as session:
        chat = _session_by_title(session, ctx, params.title)
        messages = session.scalar(
            select(func.count())
            .select_from(MessageRow)
            .where(MessageRow.session_id == chat.id, MessageRow.role != "tool")
        )
    state = ctx.turn_state(chat.id)
    return ToolResult(
        f"{chat.title} · chat session · {state} · {messages} messages · "
        f"last active {chat.updated_at:%Y-%m-%d %H:%M} UTC",
        refs=[{"kind": "session", "id": chat.id, "name": chat.title, "ref": None}],
    )


STATUS = AgentTool(
    name="session.status",
    description="How a session is doing: whether it's working, and how many messages it has.",
    params=SessionParams,
    run=_status,
    max_tokens=100,
)


class OpenParams(BaseModel):
    name: str = Field(min_length=1, description="A file name or a session title.")


def _open(ctx: ToolContext, params: OpenParams) -> ToolResult:
    with ctx.sessions() as session:
        try:
            row = _artifact_by_name(session, ctx, params.name)
            tab, label = {"kind": "artifact", "id": row.id}, row.path
        except ToolError:
            chat = _session_by_title(session, ctx, params.name)
            tab, label = {"kind": "session", "id": chat.id}, chat.title
    return ToolResult(
        f"Opened {label} in a tab.",
        refs=[{**tab, "name": label, "ref": None}],
        action={"type": "open_tab", "tab": tab},
    )


OPEN = AgentTool(
    name="tab.open",
    description="Open a file or a session in a tab for the person.",
    params=OpenParams,
    run=_open,
    max_tokens=60,
)


# --- chat.pin / chat.unpin -------------------------------------------------------------------


class PinParams(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=state.PIN_CHARS,
        description='What must hold for the rest of the chat, e.g. "Answer in Ukrainian".',
    )
    kind: Literal["instruction", "fact"] = Field(
        description="instruction: how to work; fact: something the person confirmed."
    )


def _pin(ctx: ToolContext, params: PinParams) -> ToolResult:
    try:
        pinned = state.pin(ctx.sessions, ctx.session_id, params.text, params.kind)
    except state.PinError as exc:
        raise ToolError(str(exc)) from None
    return ToolResult(f"Pinned ({pinned.kind}): {pinned.text}", action={"type": "pinned"})


PIN = AgentTool(
    name="chat.pin",
    description=(
        "Pin a standing instruction the person gave, or a fact they confirmed, so it holds for "
        "the rest of this chat, word for word. The person can unpin it."
    ),
    params=PinParams,
    run=_pin,
    max_tokens=100,
)


class UnpinParams(BaseModel):
    text: str = Field(min_length=1, description="The pinned item's text (or its id).")


def _unpin(ctx: ToolContext, params: UnpinParams) -> ToolResult:
    try:
        removed = state.unpin(ctx.sessions, ctx.session_id, params.text)
    except state.PinError as exc:
        raise ToolError(str(exc)) from None
    return ToolResult(f"Unpinned: {removed.text}", action={"type": "pinned"})


UNPIN = AgentTool(
    name="chat.unpin",
    description="Remove a pinned item when the person says it no longer holds.",
    params=UnpinParams,
    run=_unpin,
    max_tokens=100,
)


CORE_TOOLS = [LIST, FIND, SEARCH, OUTLINE, READ, WRITE, TRASH, STATUS, OPEN, PIN, UNPIN]


def core_registry() -> Registry:
    return Registry(CORE_TOOLS)
