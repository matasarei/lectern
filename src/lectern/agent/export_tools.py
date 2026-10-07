"""The chat's export tools: export.formats and export.run, over whatever export modules are
installed (blueprint: "Before offering an export format, call export.formats()").

export.run always writes a new file, at the project's top level, named after the Document.
It runs inside the tool call: a Document of a few dozen pages exports in well under a
second. It asks first only when the format would lose something asked of it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from lectern.agent.core_tools import _artifact_by_name, _file_ref, _size
from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult
from lectern.documents.document import Document
from lectern.documents.export import (
    CAPABILITIES,
    DOCUMENT_MD,
    Exporter,
    ExportOptions,
    file_title,
    load_for_export,
    lost,
    resolved_options,
)
from lectern.projects import files
from lectern.projects.tables import ArtifactRow


class FormatsParams(BaseModel):
    pass


class RunParams(BaseModel):
    name: str = Field(
        min_length=1,
        description='What to export: an OCR session\'s folder ("Lease 1998/") or a Markdown '
        "or text file, as in project.list.",
    )
    format: str = Field(min_length=1, description='A format from export.formats, e.g. "odt".')
    open_doubts: Literal["comment", "inline"] | None = Field(
        default=None,
        description="Open questions as comments (where the format has them; the default) or "
        "as inline marks. Never written as one of their candidates.",
    )
    notes: Literal["omit", "comment"] = Field(
        default="omit", description="Page notes: left out (default), or added as comments."
    )


def export_tools(exporters: list[Exporter]) -> list[AgentTool]:
    by_format = {e.format: e for e in sorted(exporters, key=lambda e: e.format)}

    def formats(ctx: ToolContext, params: FormatsParams) -> ToolResult:
        if not by_format:
            return ToolResult("No export module is installed.")
        lines = [
            f"{e.format} · {', '.join(c for c in CAPABILITIES if c in e.capabilities)}"
            for e in by_format.values()
        ]
        return ToolResult(
            "\n".join(lines) + "\nOptions: open_doubts = comment | inline (never a guess); "
            "notes = omit | comment."
        )

    async def run(ctx: ToolContext, params: RunParams) -> ToolResult:
        exporter = by_format.get(params.format.lower().lstrip("."))
        if exporter is None:
            installed = ", ".join(by_format) or "none"
            raise ToolError(f"No {params.format} exporter is installed. Installed: {installed}.")
        source, doc = await run_in_threadpool(_load, ctx, params.name)
        opts = resolved_options(
            ExportOptions(open_doubts=params.open_doubts, notes=params.notes), exporter
        )
        missing = lost(doc, opts, exporter)
        if missing:
            question = f"Export {doc.title} to {exporter.format}? It has no {'; '.join(missing)}."
            if not await ctx.confirm(question):
                raise ToolError("The person said no; the file was not exported.")
        row = await run_in_threadpool(_write, ctx, exporter, doc, opts, source)
        return ToolResult(
            f"Saved as {row.path}, {_size(row.size)}, with {exporter.title}. {_summary(doc, opts)}",
            refs=[_file_ref(row)],
            action={"type": "open_tab", "tab": {"kind": "artifact", "id": row.id}},
        )

    return [
        AgentTool(
            name="export.formats",
            description="List the installed export formats and what each keeps (tables, "
            "comments, figures). Call it before offering a format.",
            params=FormatsParams,
            run=formats,
            max_tokens=200,
        ),
        AgentTool(
            name="export.run",
            description="Build a file (odt, docx, md…) from an OCR session's Document or a "
            "Markdown/text file. Always writes a new file; keeps the person's answers and "
            "marks open questions.",
            params=RunParams,
            run=run,
            max_tokens=300,
        ),
    ]


def _load(ctx: ToolContext, name: str) -> tuple[ArtifactRow, Document]:
    """The file to export from: a folder name means its document.md (an OCR session's)."""
    folder = name.strip().rstrip("/")
    with ctx.sessions() as session:
        try:
            row = _artifact_by_name(session, ctx, f"{folder}/{DOCUMENT_MD}", partial_ok=False)
        except ToolError:
            row = _artifact_by_name(session, ctx, name.strip())
        session.expunge(row)
    try:
        return row, load_for_export(ctx.project.root, row.path)
    except (ValueError, files.BadName) as exc:
        raise ToolError(str(exc)) from None


def _write(
    ctx: ToolContext, exporter: Exporter, doc: Document, opts: ExportOptions, source: ArtifactRow
) -> ArtifactRow:
    data = exporter.export(doc, opts)
    with ctx.sessions.begin() as session:
        row = files.write_bytes(
            ctx.project.root,
            session,
            owner_id=ctx.user.id,
            name=file_title(doc) + exporter.extension,
            data=data,
            origin=files.Origin("artifact", id=source.id),
        )
        session.expunge(row)
        return row


def _summary(doc: Document, opts: ExportOptions) -> str:
    count = len(doc.open_doubts())
    if not count:
        return "No open questions."
    if opts.open_doubts == "comment":
        how = "as a comment" if count == 1 else "as comments"
    else:
        how = "marked inline"
    return f"{count} open question{'s' if count > 1 else ''}, {how}."
