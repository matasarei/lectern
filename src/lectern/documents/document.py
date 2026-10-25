"""The Document an OCR session publishes (docs/ocr.md, "The Document").

`document.json` is the source of truth: blocks (headings, paragraphs, tables, lists,
signatures, stamps, figures), each with a stable id and its source page, and the doubts in it
with their candidate readings. `document.md` is made from it for the chat to read: every block
starts with an anchor the chat can cite (`{#b3.2 p=3}`), tables stay tables, and open doubts
are marked inline (`⟦1? | open | 10 · 15 · 18⟧`) so a guess is never read as fact.

Positions on the page (`bbox`) are always null until a layout model finds them; a figure is
kept only when the text names it, and shows as a placeholder.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA = "lectern.document/1"
BlockType = Literal["heading", "paragraph", "table", "list", "signature", "stamp", "figure"]
DOUBT_MARK = re.compile(r"\{(d\d+(?:\.\d+)?)\}")  # where a doubt sits: {d3.1}


class Doubt(BaseModel):
    id: str  # "d3.1": page 3, first doubt; stays put when other pages change
    raw: str  # what the page seems to say, e.g. "1?"
    status: Literal["open", "resolved"] = "open"
    candidates: list[str] = Field(default_factory=list)
    reason: str = ""
    source: Literal["clean", "ocr"] = "clean"  # the cleanup model, or an unsure OCR word
    value: str | None = None  # the reading chosen when resolved
    resolved_by: str | None = None


class Block(BaseModel):
    id: str  # "b3.2": page 3, second block
    type: BlockType
    page: int
    pages: list[int] | None = None  # every page it spans, when it continues onto the next
    bbox: list[int] | None = None  # no layout model yet: always null
    level: int | None = None  # headings: 1-3
    text: str = ""  # {d1} marks a doubt
    rows: list[list[str]] | None = None  # tables; the first row is the header
    label: str | None = None  # figures and stamps: "stamp", "signature", "photo"
    doubts: list[Doubt] = Field(default_factory=list)


class PageInfo(BaseModel):
    n: int
    image: str  # relative to the document's folder
    size: tuple[int, int]
    note: str = ""
    blocks: list[str] = Field(default_factory=list)
    failed: str | None = None  # why the page couldn't be cleaned


class Source(BaseModel):
    artifact: str
    pages: int


class Document(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default=SCHEMA, alias="schema")
    title: str
    version: int = 1
    source: Source
    pages: list[PageInfo] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=1)

    @classmethod
    def from_json(cls, text: str) -> Document:
        return cls.model_validate_json(text)

    def open_doubts(self) -> list[Doubt]:
        return [d for b in self.blocks for d in b.doubts if d.status == "open"]


def render_markdown(doc: Document) -> str:
    """document.md: what the chat reads."""
    lines = [
        "---",
        f"document: {doc.title} · version {doc.version}",
        f"source: {doc.source.artifact} · {doc.source.pages} pages",
        f"open_questions: {len(doc.open_doubts())}",
        "---",
        "",
    ]
    blocks = {b.id: b for b in doc.blocks}
    figures = 0
    for page in doc.pages:
        if page.note:
            lines += [f"> Note for page {page.n} (from you): {page.note}", ""]
        if page.failed:
            lines += [
                f"{{#p{page.n} p={page.n}}} [Page {page.n} wasn't cleaned: {page.failed}]",
                "",
            ]
        for block_id in page.blocks:
            block = blocks.get(block_id)
            if block is None or block.page != page.n:
                continue  # a block continued from an earlier page is written there
            if block.type == "figure":
                figures += 1
            lines += [_render_block(block, figures), ""]
    return "\n".join(lines).rstrip() + "\n"


def render_page(doc: Document, number: int) -> str | None:
    """One page's blocks as document.md writes them, or None when the page isn't in the
    Document. A block that began on an earlier page is shown here too."""
    page = next((p for p in doc.pages if p.n == number), None)
    if page is None:
        return None
    if page.failed:
        return f"[Page {number} wasn't cleaned: {page.failed}]"
    blocks = {b.id: b for b in doc.blocks}
    figures = [b.id for b in doc.blocks if b.type == "figure"]
    parts = [
        _render_block(blocks[i], figures.index(i) + 1 if i in figures else 0)
        for i in page.blocks
        if i in blocks
    ]
    return "\n\n".join(parts)


def _anchor(block: Block, kind: str = "") -> str:
    pages = f"{block.page}→{block.pages[-1]}" if block.pages else str(block.page)
    return f"{{#{block.id} p={pages}{' ' + kind if kind else ''}}}"


def _render_block(block: Block, figure_number: int) -> str:
    text = _with_doubts(block.text, block.doubts)
    if block.type == "heading":
        return f"{'#' * min(max(block.level or 2, 1), 6)} {text} {_anchor(block)}"
    if block.type == "table":
        return f"{_anchor(block, 'table')}\n" + _table(block)
    if block.type == "list":
        items = [f"- {item}" for item in text.splitlines() if item.strip()]
        return f"{_anchor(block, 'list')}\n" + "\n".join(items)
    if block.type == "figure":
        label = block.label or "image"
        return f"{_anchor(block, 'figure')} [Image {figure_number}: {label} · no cut-out yet]"
    if block.type in ("signature", "stamp"):
        return f"{_anchor(block, block.type)} {text}".rstrip()
    return f"{_anchor(block)} {text}"


def _table(block: Block) -> str:
    rows = [
        [_with_doubts(cell, block.doubts).replace("|", "\\|") for cell in row]
        for row in (block.rows or [])
    ]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(out)


def _with_doubts(text: str, doubts: list[Doubt]) -> str:
    """{d1} becomes the chosen reading once resolved, or ⟦raw | open | a · b⟧ while open."""
    by_id = {d.id: d for d in doubts}

    def mark(match: re.Match[str]) -> str:
        doubt = by_id.get(match.group(1))
        if doubt is None:
            return match.group(0)
        if doubt.status == "resolved" and doubt.value is not None:
            return doubt.value
        candidates = " · ".join(doubt.candidates)
        return f"⟦{doubt.raw} | open{' | ' + candidates if candidates else ''}⟧"

    return DOUBT_MARK.sub(mark, text)
