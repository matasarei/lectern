"""Export · MD: a Document as Markdown. Tables stay tables; there are no comments in
Markdown, so open questions are inline marks and page notes (when asked for) are quotes."""

from __future__ import annotations

from lectern.agent.tools import AgentTool
from lectern.documents.document import Block, Document
from lectern.documents.export import (
    ExportOptions,
    figure_placeholder,
    inline_doubt,
    note_text,
    reading_order,
    segments,
)
from lectern.modules import ToolEnv


class MarkdownExporter:
    format = "md"
    title = "Export · MD"
    extension = ".md"
    mime = "text/markdown"
    capabilities = frozenset({"tables"})

    def export(self, doc: Document, opts: ExportOptions) -> bytes:
        parts: list[str] = []
        for block, note, figure in reading_order(doc):
            if note is not None and opts.notes == "comment":
                parts.append(f"> {note_text(block.page, note)}")
            parts.append(_block(block, figure))
        return ("\n\n".join(p for p in parts if p) + "\n").encode("utf-8")


def _text(text: str, block: Block) -> str:
    return "".join(run if d is None else inline_doubt(d) for run, d in segments(text, block.doubts))


def _block(block: Block, figure: int) -> str:
    if block.type == "heading":
        return f"{'#' * (block.level or 2)} {_text(block.text, block)}"
    if block.type == "table":
        rows = [[_text(c, block).replace("|", "\\|") for c in row] for row in block.rows or []]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        return "\n".join(lines + ["| " + " | ".join(r) + " |" for r in rows[1:]])
    if block.type == "list":
        return "\n".join(
            f"- {_text(item, block)}" for item in block.text.splitlines() if item.strip()
        )
    if block.type == "figure":
        return figure_placeholder(block, figure)
    return _text(block.text, block)


class MarkdownModule:
    id = "export-md"

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        return []

    def exporters(self) -> list[MarkdownExporter]:
        return [MarkdownExporter()]


module = MarkdownModule()
