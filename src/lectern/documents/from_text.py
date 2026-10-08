"""Text files as Documents, so "convert reply-letter.md to ODT" goes through the same exporter
as an OCR Document. A text file is one page; its blocks are b1.1, b1.2… A PDF's text layer
keeps its pages (parse_pages).

Markdown: headings (#–###), paragraphs, lists (-, *, 1.) and pipe tables. Anything else
stays as a paragraph's text, word for word. Plain text: paragraphs, split on blank lines.
"""

from __future__ import annotations

import re

from lectern.documents.document import Block, Document, PageInfo, Source

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
CELL_SPLIT = re.compile(r"(?<!\\)\|")


def parse_markdown(title: str, text: str, *, source: str = "") -> Document:
    blocks: list[Block] = []
    lines = text.splitlines()
    i = 0

    def add(kind: str, body: str = "", **fields: object) -> None:
        blocks.append(
            Block(id=f"b1.{len(blocks) + 1}", type=kind, page=1, text=body, **fields)  # type: ignore[arg-type]
        )

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if heading := HEADING.match(line):
            add("heading", heading.group(2), level=min(len(heading.group(1)), 3))
            i += 1
        elif TABLE_ROW.match(line) and i + 1 < len(lines) and TABLE_RULE.match(lines[i + 1]):
            rows = [_cells(line)]
            i += 2
            while i < len(lines) and TABLE_ROW.match(lines[i]):
                rows.append(_cells(lines[i]))
                i += 1
            add("table", rows=rows)
        elif LIST_ITEM.match(line):
            items = []
            while i < len(lines) and (item := LIST_ITEM.match(lines[i])):
                items.append(item.group(1).strip())
                i += 1
            add("list", "\n".join(items))
        else:
            paragraph = []
            while i < len(lines) and lines[i].strip() and not _starts_block(lines, i):
                paragraph.append(lines[i].strip())
                i += 1
            add("paragraph", "\n".join(paragraph))
    return _document(title, source, blocks)


def parse_text(title: str, text: str, *, source: str = "") -> Document:
    return _document(title, source, _paragraphs(text, page=1))


def parse_pages(title: str, pages: list[str], *, source: str = "") -> Document:
    """A PDF's text layer, page by page: page n's paragraphs are b<n>.1, b<n>.2… so citations
    and page notes match the PDF. A page without text keeps its place, with no blocks."""
    blocks_by_page = [_paragraphs(text, page=n) for n, text in enumerate(pages, start=1)]
    return Document(
        title=title,
        source=Source(artifact=source or title, pages=len(pages)),
        pages=[
            PageInfo(n=n, image="", size=(0, 0), blocks=[b.id for b in blocks])
            for n, blocks in enumerate(blocks_by_page, start=1)
        ],
        blocks=[b for blocks in blocks_by_page for b in blocks],
    )


def _paragraphs(text: str, *, page: int) -> list[Block]:
    """Paragraphs split on blank lines; a paragraph keeps its line breaks (a statement's rows)."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [
        Block(
            id=f"b{page}.{n}",
            type="paragraph",
            page=page,
            text="\n".join(x.strip() for x in p.splitlines()),
        )
        for n, p in enumerate(paragraphs, start=1)
    ]


def _starts_block(lines: list[str], i: int) -> bool:
    line = lines[i]
    if HEADING.match(line) or LIST_ITEM.match(line):
        return True
    return bool(TABLE_ROW.match(line) and i + 1 < len(lines) and TABLE_RULE.match(lines[i + 1]))


def _cells(line: str) -> list[str]:
    inner = line.strip().removeprefix("|").removesuffix("|")
    return [c.strip().replace("\\|", "|") for c in CELL_SPLIT.split(inner)]


def _document(title: str, source: str, blocks: list[Block]) -> Document:
    return Document(
        title=title,
        source=Source(artifact=source or title, pages=1),
        pages=[PageInfo(n=1, image="", size=(0, 0), blocks=[b.id for b in blocks])],
        blocks=blocks,
    )
