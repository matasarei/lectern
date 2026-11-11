"""The chat's PDF tools: pdf.edit changes text in place, pdf.fill fills a form. Each writes a
new file, "<name> (edited).pdf" at the project's top level pointing back at the original,
and opens it; the original is never touched. They run inside the tool call (a few edits take
well under a second), and ask first only when an edit would change how the page looks: text
running into the next column or past the margin, or text drawn in a font it wasn't in.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from lectern.agent.core_tools import _artifact_by_name, _file_ref, _size
from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult
from lectern.modules.pdf_edit import engine, forms
from lectern.projects import files
from lectern.projects.tables import ArtifactRow

EDITED = " (edited)"
MAX_EDITS = 50
SHOWN_CHANGES = 12  # changes listed in the result; the rest are counted


class EditItem(BaseModel):
    find: str = Field(
        min_length=1,
        description="The text to change, copied exactly from one line of the PDF; long "
        "enough to be in one place only.",
    )
    replace: str = Field(description="The new text, in the style of the original.")
    page: int | None = Field(default=None, ge=1, description="The page it is on (1 = first).")
    all: bool = Field(default=False, description="Change every place `find` is.")


class EditParams(BaseModel):
    name: str = Field(min_length=1, description="The PDF, as in project.list.")
    edits: list[EditItem] = Field(min_length=1, max_length=MAX_EDITS)


class FillParams(BaseModel):
    name: str = Field(min_length=1, description="The PDF form, as in project.list.")
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="Field name → value. Leave empty to list the form's fields first. "
        "Checkboxes take yes or no; a choice takes one of its options.",
    )


def pdf_tools() -> list[AgentTool]:
    return [
        AgentTool(
            name="pdf.edit",
            description="Change words, numbers, dates or names in a PDF that has a text layer, "
            "keeping its fonts, sizes and layout. Read the PDF first (document_read) and copy "
            "`find` from its text: the words there now, not the new ones. Each edit replaces "
            "`find` with `replace`. A number the person didn't give, such as a new total, "
            'comes from code_run first. Writes a new file "<name> (edited).pdf"; the '
            "original stays. Only text within a line: not new paragraphs, colours or fonts, "
            "and not a scan (OCR reads those).",
            params=EditParams,
            run=_edit,
            max_tokens=600,
            writes=_edit_writes,
        ),
        AgentTool(
            name="pdf.fill",
            description="Fill a PDF form's fields. Call it with no fields to list them (name, "
            "kind, value, options), then with fields {name: value}. Writes a new file "
            '"<name> (edited).pdf"; the original stays.',
            params=FillParams,
            run=_fill,
            max_tokens=600,
            writes=lambda params: (" ".join(params.fields.values()), ""),
        ),
    ]


def _edit_writes(params: EditParams) -> tuple[str, str]:
    """The new text, and the text it replaces: a number in `find` is the PDF's own."""
    return (
        " ".join(e.replace for e in params.edits),
        " ".join(e.find for e in params.edits),
    )


async def _edit(ctx: ToolContext, params: EditParams) -> ToolResult:
    source, path = await run_in_threadpool(_pdf, ctx, params.name)
    edits = [engine.Edit(e.find, e.replace, e.page, e.all) for e in params.edits]
    try:
        result = await run_in_threadpool(engine.apply_edits, path, edits)
    except engine.EditFailed as exc:
        raise ToolError(str(exc)) from None
    if result.degraded:
        question = (
            f"Save the edited {source.path}? It won't look quite like the original: "
            + "; ".join(result.degraded)
            + "."
        )
        if not await ctx.confirm(question):
            raise ToolError("The person said no; no edited PDF was saved.")
    row = await run_in_threadpool(_write, ctx, source, result.data)
    return _saved(row, _changes(result.changes))


async def _fill(ctx: ToolContext, params: FillParams) -> ToolResult:
    source, path = await run_in_threadpool(_pdf, ctx, params.name)
    if not params.fields:
        fields = await run_in_threadpool(forms.form_fields, path)
        if not fields:
            raise ToolError(f"{source.path} has no form fields. To change its text, use pdf.edit.")
        return ToolResult("\n".join(_field_line(f) for f in fields))
    try:
        filled = await run_in_threadpool(forms.fill_form, path, params.fields)
    except engine.EditFailed as exc:
        raise ToolError(str(exc)) from None
    row = await run_in_threadpool(_write, ctx, source, filled.data)
    by_name = {f.name: f for f in filled.fields}
    lines = [_field_line(by_name[name]) for name in params.fields if name in by_name]
    return _saved(row, lines)


def _pdf(ctx: ToolContext, name: str) -> tuple[ArtifactRow, Path]:
    """The person's PDF with this name, and where it is."""
    with ctx.sessions() as session:
        row = _artifact_by_name(session, ctx, name.strip())
        session.expunge(row)
    if PurePosixPath(row.path).suffix.lower() != ".pdf":
        raise ToolError(f"{row.path} isn't a PDF.")
    try:
        path = files.inside(ctx.project.root, row.path)
    except files.BadName as exc:
        raise ToolError(str(exc)) from None
    if not path.is_file():
        raise ToolError(f"{row.path} is listed but its file is missing.")
    return row, path


def _write(ctx: ToolContext, source: ArtifactRow, data: bytes) -> ArtifactRow:
    stem = PurePosixPath(source.path).stem
    name = (stem if stem.endswith(EDITED) else stem + EDITED) + ".pdf"
    with ctx.sessions.begin() as session:
        row = files.write_bytes(
            ctx.project.root,
            session,
            owner_id=ctx.user.id,
            name=name,
            data=data,
            origin=files.Origin("artifact", id=source.id),
        )
        session.expunge(row)
        return row


def _saved(row: ArtifactRow, lines: list[str]) -> ToolResult:
    return ToolResult(
        f"Saved as {row.path}, {_size(row.size)}.\n" + "\n".join(lines),
        refs=[_file_ref(row)],
        action={"type": "open_tab", "tab": {"kind": "artifact", "id": row.id}},
    )


def _changes(changes: list[engine.Change]) -> list[str]:
    lines = []
    for change in changes[:SHOWN_CHANGES]:
        line = f"p{change.page}: “{change.after.strip()}”"
        said = change.notes + change.degraded
        lines.append(line + (f" ({'; '.join(said)})" if said else ""))
    if len(changes) > SHOWN_CHANGES:
        lines.append(f"…and {len(changes) - SHOWN_CHANGES} more places.")
    return lines


def _field_line(f: forms.Field) -> str:
    line = f"“{f.name}” · {f.kind} · p{f.page} · “{f.value}”"
    if f.options:
        line += " · options: " + ", ".join(f"“{o}”" for o in f.options)
    if f.read_only:
        line += " · read-only"
    return line
