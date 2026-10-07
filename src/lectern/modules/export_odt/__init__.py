"""Export · ODT: a Document as an OpenDocument text file, with odfdo (Apache-2.0). Tables are
tables; an open question is its text with a comment (an ODF annotation) listing the
candidates; page notes, when asked for, are comments at the start of the page's first
block."""

from __future__ import annotations

import io

from odfdo import Annotation, Header, List, ListItem, Paragraph, Table
from odfdo import Document as OdfDocument

from lectern.agent.tools import AgentTool
from lectern.documents.document import Block, Document
from lectern.documents.export import (
    ExportOptions,
    doubt_comment,
    figure_placeholder,
    inline_doubt,
    note_text,
    reading_order,
    segments,
)
from lectern.modules import ToolEnv

AUTHOR = "Lectern"


class OdtExporter:
    format = "odt"
    title = "Export · ODT"
    extension = ".odt"
    mime = "application/vnd.oasis.opendocument.text"
    capabilities = frozenset({"tables", "comments"})

    def export(self, doc: Document, opts: ExportOptions) -> bytes:
        out = OdfDocument("text")
        out.body.clear()
        out.meta.title = doc.title
        tables = 0
        for block, note, figure in reading_order(doc):
            lead = None
            if note is not None and opts.notes == "comment":
                lead = note_text(block.page, note)
            if block.type == "table":
                tables += 1
            out.body.append(_block(out, block, figure, opts, lead, tables))
        buffer = io.BytesIO()
        out.save(buffer)
        return buffer.getvalue()


def _fill(
    out: OdfDocument,
    paragraph: Paragraph,
    text: str,
    block: Block,
    opts: ExportOptions,
    lead: str | None = None,
) -> Paragraph:
    """Write `text` into the paragraph; `lead` is a comment placed at its start (a note)."""
    if lead is not None:
        paragraph.append(Annotation(lead, creator=AUTHOR, parent=out.body))
    for run_text, doubt in segments(text, block.doubts):
        if doubt is None:
            paragraph.append(run_text)
        elif opts.open_doubts == "comment":
            paragraph.append(Annotation(doubt_comment(doubt), creator=AUTHOR, parent=out.body))
            paragraph.append(doubt.raw)
        else:
            paragraph.append(inline_doubt(doubt))
    return paragraph


def _block(  # noqa: ANN202
    out: OdfDocument,
    block: Block,
    figure: int,
    opts: ExportOptions,
    lead: str | None,
    tables: int,
):
    if block.type == "heading":
        return _fill(out, Header(min(block.level or 2, 10)), block.text, block, opts, lead)
    if block.type == "table":
        rows = block.rows or []
        width = max((len(r) for r in rows), default=1)
        # Sized up front: odfdo's Table always starts with a row, even at height 0.
        table = Table(f"Table{tables}", width=width, height=max(len(rows), 1))
        for r, row in enumerate(rows):
            for c in range(width):
                cell = table.get_cell((c, r))
                text = row[c] if c < len(row) else ""
                note = lead if (r, c) == (0, 0) else None
                cell.append(_fill(out, Paragraph(), text, block, opts, note))
                table.set_cell((c, r), cell)
        return table
    if block.type == "list":
        items = List()
        for i, item in enumerate(t for t in block.text.splitlines() if t.strip()):
            entry = ListItem()
            entry.append(_fill(out, Paragraph(), item, block, opts, lead if i == 0 else None))
            items.append(entry)
        return items
    if block.type == "figure":
        paragraph = _fill(out, Paragraph(), "", block, opts, lead)  # the note, if any
        paragraph.append(figure_placeholder(block, figure))
        return paragraph
    return _fill(out, Paragraph(), block.text, block, opts, lead)


class OdtModule:
    id = "export-odt"

    def tools(self, env: ToolEnv) -> list[AgentTool]:
        return []

    def exporters(self) -> list[OdtExporter]:
        return [OdtExporter()]


module = OdtModule()
