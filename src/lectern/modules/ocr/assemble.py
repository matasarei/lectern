"""Assemble the cleaned pages into one Document: the fourth phase of OCR (docs/ocr.md,
"How a session runs"). Code only, no model:

- each block gets its id, `b<page>.<n>` in the order cleanup gave it, so a page cleaned again
  never renumbers another page's blocks;
- running headers and footers repeated across pages, and bare page numbers, are dropped;
- text that runs across a page break is joined (a hyphenated word is put back together);
- a table that continues at the top of the next page is merged, unless a page note says not
  to ("don't merge");
- doubts get their ids, `d<page>.<n>` like the blocks' (so a page cleaned again never
  renumbers another page's doubts): cleanup's own, then unsure OCR words still in the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lectern.documents.document import Block, Document, Doubt, PageInfo, Source
from lectern.modules.ocr.clean import PAGE_NUMBER, CleanBlock, CleanedPage
from lectern.modules.ocr.doubts import ocr_doubts

DIGITS = re.compile(r"\d+")
NO_MERGE = re.compile(r"\b(?:don'?t|do not|never)\s+merge\b|не\s+об['’]?єдну", re.I)
SENTENCE_END = re.compile(r"[.!?:;…»\"')\]]\s*$")
LEFT_MARK = re.compile(r"⟨([^⟨⟩]+)\?⟩")  # an unsure-word mark cleanup should have removed
CELL_GAP = re.compile(r"\s{2,}|\t")
REPEATS = 3  # a line at the top or bottom of this many pages is a running header or footer
RUNNING_LINE_CHARS = 100  # a running header or footer is a short line, never a paragraph


@dataclass
class PageInput:
    n: int
    image: str
    size: tuple[int, int]
    note: str = ""
    cleaned: CleanedPage | None = None
    failed: str | None = None  # why cleanup failed; the page gets a placeholder
    unsure: list[str] = field(default_factory=list)  # words the OCR model wasn't sure of


def assemble(title: str, source: str, pages: list[PageInput], *, version: int = 1) -> Document:
    counters = {p.n: _Counter(p.n) for p in pages}
    per_page: dict[int, list[Block]] = {}
    for page in pages:
        per_page[page.n] = _blocks(page, counters[page.n]) if page.cleaned else []
    _drop_running_lines(per_page, len(pages))
    notes = {p.n: p.note for p in pages}
    order = [p.n for p in pages]
    for before, after in zip(order, order[1:], strict=False):
        _join_across(per_page[before], per_page[after], notes[before] + " " + notes[after])
    for page in pages:
        for block in per_page[page.n]:
            _add_ocr_doubts(block, page.unsure, counters[page.n])
    infos = [
        PageInfo(
            n=p.n,
            image=p.image,
            size=p.size,
            note=p.note,
            failed=p.failed,
            blocks=[b.id for b in per_page[p.n]]
            + [b.id for q in order if q < p.n for b in per_page[q] if p.n in (b.pages or [])],
        )
        for p in pages
    ]
    return Document(
        title=title,
        version=version,
        source=Source(artifact=source, pages=len(pages)),
        pages=infos,
        blocks=[b for n in order for b in per_page[n]],
    )


class _Counter:
    """A page's doubt ids: d3.1, d3.2…"""

    def __init__(self, page: int) -> None:
        self.page = page
        self.next = 1

    def peek(self) -> str:
        return f"d{self.page}.{self.next}"

    def take(self) -> str:
        doubt_id = f"d{self.page}.{self.next}"
        self.next += 1
        return doubt_id


def _blocks(page: PageInput, counter: _Counter) -> list[Block]:
    cleaned = page.cleaned
    blocks = [
        Block(
            id=f"b{page.n}.{i}",
            type=b.type,
            page=page.n,
            level=b.level if b.type == "heading" else None,
            text=LEFT_MARK.sub(r"\1", b.text),
            rows=[[LEFT_MARK.sub(r"\1", c) for c in row] for row in _rows(b)]
            if b.type == "table"
            else None,
            label=b.label if b.type in ("figure", "stamp") else None,
        )
        for i, b in enumerate(cleaned.blocks, start=1)
    ]
    for doubt in cleaned.doubts:
        block = blocks[doubt.block]
        doubt_id = counter.peek()
        if not _place(block, doubt.raw, doubt_id):
            continue  # cleanup checked it was there; nothing to place, so no id used
        counter.take()
        block.doubts.append(
            Doubt(id=doubt_id, raw=doubt.raw, candidates=doubt.candidates, reason=doubt.reason)
        )
    return blocks


def _rows(block: CleanBlock) -> list[list[str]]:
    """A table's rows. A small model sometimes puts a table's text in `text` instead: its
    lines are rows, split into cells on "|" or on runs of two or more spaces."""
    if block.rows:
        return block.rows
    rows = []
    for line in block.text.splitlines():
        line = line.strip().strip("|")
        if not line or set(line) <= set("-|: "):
            continue  # blank, or a Markdown rule (|---|---|)
        cells = line.split("|") if "|" in line else CELL_GAP.split(line)
        rows.append([c.strip() for c in cells])
    return rows


def _place(block: Block, raw: str, doubt_id: str) -> bool:
    """Put {dN} where `raw` first appears in the block's cells or text. Cells first: a table
    is shown from its rows, and cleanup sometimes gives its text as well, where a mark would
    never be seen."""
    mark = f"{{{doubt_id}}}"
    for row in block.rows or []:
        for i, cell in enumerate(row):
            if raw in cell:
                row[i] = cell.replace(raw, mark, 1)
                return True
    if raw in block.text:
        block.text = block.text.replace(raw, mark, 1)
        return True
    return False


def _add_ocr_doubts(block: Block, unsure: list[str], counter: _Counter) -> None:
    unsure = [w for w in unsure if w not in {d.raw for d in block.doubts}]  # cleanup's own
    if not unsure or block.type in ("figure", "stamp"):
        return
    block.text, doubts = ocr_doubts(block.text, unsure, counter.take)
    for row in block.rows or []:
        for i, cell in enumerate(row):
            row[i], more = ocr_doubts(cell, unsure, counter.take)
            doubts += more
    block.doubts += doubts


def _page_number(block: Block) -> bool:
    """A bare page number, whatever the model called it: a small model has typed "стор. 2 з 3"
    as a signature or a stamp."""
    return block.type not in ("table", "figure") and PAGE_NUMBER.match(block.text) is not None


def _running_candidate(block: Block) -> bool:
    return (
        block.type in ("paragraph", "heading")
        and bool(block.text)
        and len(block.text) <= RUNNING_LINE_CHARS
    )


def _key(block: Block) -> str:
    return _line_key(block.text)


def _line_key(text: str) -> str:
    return DIGITS.sub("#", " ".join(text.split())).casefold()


def _strip_running_line(block: Block, edge: int, running: set[str]) -> None:
    """Take a running header (edge 0) or footer (-1) off a block that cleanup merged it into
    ("Договір оренди № 17/2026\\n4. Розділ 4", m4.md), keeping the rest. A line holding a
    question ({d4.1}) never matches a header's words, so it stays."""
    lines = block.text.split("\n")
    if len(lines) < 2 or _line_key(lines[edge]) not in running:
        return
    block.text = "\n".join(lines[1:] if edge == 0 else lines[:-1])


def _drop_running_lines(per_page: dict[int, list[Block]], total: int) -> None:
    """Bare page numbers at a page's top or bottom, and a short first or last line that
    repeats (digits aside) at the top or bottom of REPEATS pages or more. A shorter document
    keeps its lines: two pages that happen to start alike aren't a running header."""
    for edge in (0, -1):
        seen: dict[str, int] = {}
        for blocks in per_page.values():
            if blocks and _running_candidate(blocks[edge]):
                seen[_key(blocks[edge])] = seen.get(_key(blocks[edge]), 0) + 1
        for blocks in per_page.values():
            if not blocks:
                continue
            block = blocks[edge]
            numbered = _page_number(block)
            # A page's only block is its content, however alike the pages are.
            repeated = (
                len(blocks) > 1
                and _running_candidate(block)
                and seen.get(_key(block), 0) >= REPEATS
            )
            if numbered or repeated:
                blocks.pop(edge)
        running = {key for key, count in seen.items() if count >= REPEATS}
        for blocks in per_page.values():
            if blocks and blocks[edge].type in ("paragraph", "heading"):
                _strip_running_line(blocks[edge], edge, running)
    for blocks in per_page.values():  # a number left at an edge after a header went
        for edge in (0, -1):
            if blocks and _page_number(blocks[edge]):
                blocks.pop(edge)


def _join_across(before: list[Block], after: list[Block], notes: str) -> None:
    if not before or not after:
        return
    last, first = before[-1], after[0]
    if last.type == first.type == "table" and not NO_MERGE.search(notes):
        width = len((last.rows or [[]])[0])
        rows = first.rows or []
        if rows and len(rows[0]) == width:
            if last.rows and rows[0] == last.rows[0]:
                rows = rows[1:]  # the header repeated on the next page
            last.rows = (last.rows or []) + rows
            _span(last, first)
            after.pop(0)
        return
    if last.type == first.type == "paragraph" and first.text[:1].islower():
        if last.text.rstrip().endswith(("-", "­")):  # a word hyphenated across the break
            last.text = last.text.rstrip().rstrip("-­") + first.text
        elif not SENTENCE_END.search(last.text):
            last.text = last.text.rstrip() + " " + first.text
        else:
            return
        _span(last, first)
        after.pop(0)


def _span(kept: Block, joined: Block) -> None:
    kept.pages = sorted({*(kept.pages or [kept.page]), *(joined.pages or [joined.page])})
    kept.doubts += joined.doubts
