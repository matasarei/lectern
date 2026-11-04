"""Assemble the cleaned pages into one Document: the fourth phase of OCR (docs/ocr.md,
"How a session runs"). Code only, no model:

- each block gets its id, `b<page>.<n>` in the order cleanup gave it, so a page cleaned again
  never renumbers another page's blocks;
- running headers and footers repeated across pages, and bare page numbers, are dropped;
- text that runs across a page break is joined (a hyphenated word is put back together);
- a table that continues at the top of the next page is merged, unless a page note says not
  to ("don't merge");
- doubts get their ids, `d<page>.<n>` like the blocks' (so a page cleaned again never
  renumbers another page's doubts): cleanup's own, then unsure OCR words still in the text,
  then a word one letter off the same word in lines repeated through the document.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from lectern.documents.document import Block, Document, Doubt, PageInfo, Source
from lectern.modules.ocr.clean import PAGE_NUMBER, CleanBlock, CleanedPage
from lectern.modules.ocr.doubts import EDGE, ocr_doubts

DIGITS = re.compile(r"\d+")
NO_MERGE = re.compile(r"\b(?:don'?t|do not|never)\s+merge\b|не\s+об['’]?єдну", re.I)
SENTENCE_END = re.compile(r"[.!?:;…»\"')\]]\s*$")
LEFT_MARK = re.compile(r"⟨([^⟨⟩]+)\?⟩")  # an unsure-word mark cleanup should have removed
CELL_GAP = re.compile(r"\s{2,}|\t")
REPEATS = 3  # a line at the top or bottom of this many pages is a running header or footer
RUNNING_LINE_CHARS = 100  # a running header or footer is a short line, never a paragraph
PATTERN_LINE_CHARS = 60  # a line repeated through a document, digits aside: a heading, a label
PATTERN_WORD_LETTERS = 4  # shorter, one letter off is another word as often as a misreading
REPEAT_REASON = 'Other lines like this one have "{}" here.'
# A label or text naming a page number, as a small model types one for a figure: "Page number",
# "page_number_1". A printed number itself is PAGE_NUMBER's.
PAGE_NUMBER_NAME = re.compile(
    r"^(?:page[\s_-]*(?:num(?:ber)?|no\.?)|номер\s+сторінки)(?:[\s_-]*\d+)?$", re.I
)


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
    _add_repeated_line_doubts(per_page, counters)
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


def _pattern_line(line: str) -> bool:
    return len(line) <= PATTERN_LINE_CHARS and bool(DIGITS.search(line)) and "{d" not in line


def _one_letter_off(word: str, other: str) -> bool:
    """One letter changed, added or taken away; a change of case doesn't count."""
    if word.casefold() == other.casefold():
        return False
    if len(word) == len(other):
        return sum(a != b for a, b in zip(word, other, strict=True)) == 1
    short, long = sorted((word, other), key=len)
    if len(long) - len(short) != 1:
        return False
    return any(long[:i] + long[i + 1 :] == short for i in range(len(long)))


def _commoner_word(pattern: str, counts: Counter[str]) -> tuple[str, str] | None:
    """The (word, commoner word) when a pattern ("#. Розділі #") differs by one word, one letter
    off, from the commonest pattern that's enough commoner than it ("#. Розділ #")."""
    words = pattern.split()
    for other, count in counts.most_common():
        if count < REPEATS or count <= counts[pattern]:
            break
        theirs = other.split()
        differ = [(a, b) for a, b in zip(words, theirs, strict=False) if a != b]
        if len(words) != len(theirs) or len(differ) != 1:
            continue
        word, commoner = (EDGE.sub("", w) for w in differ[0])
        if "#" in differ[0][0] or not word.isalpha() or len(word) < PATTERN_WORD_LETTERS:
            continue  # digits stuck to it ("Розділі5") aren't a word to offer another for
        if _one_letter_off(word, commoner):
            return word, commoner
    return None


def _add_repeated_line_doubts(
    per_page: dict[int, list[Block]], counters: dict[int, _Counter]
) -> None:
    """A question on "Розділі" in "4. Розділі 4", when more lines read "5. Розділ 5". A misreading
    can be a real word the OCR model is sure of, and only the other pages show it: the M4 lease
    has it on 5 headings of 12. Lines only, not words anywhere: Ukrainian's endings make many
    words one letter off another that's right (плата, плати). The text stays as read until the
    person answers."""
    blocks = [
        (n, block)
        for n, page in per_page.items()
        for block in page
        if block.type in ("paragraph", "heading") and block.text
    ]
    counts = Counter(
        DIGITS.sub("#", line.strip())
        for _, block in blocks
        for line in block.text.split("\n")
        if _pattern_line(line.strip())
    )
    for n, block in blocks:
        lines = block.text.split("\n")
        for i, line in enumerate(lines):
            if not _pattern_line(line.strip()):
                continue
            found = _commoner_word(DIGITS.sub("#", line.strip()), counts)
            if found is None:
                continue
            word, commoner = found
            doubt_id = counters[n].take()
            pattern = rf"(?<!\w){re.escape(word)}(?!\w)"
            lines[i] = re.sub(pattern, f"{{{doubt_id}}}", line, count=1)
            block.doubts.append(
                Doubt(id=doubt_id, raw=word, candidates=[commoner], source="repeat",
                      reason=REPEAT_REASON.format(commoner))
            )  # fmt: skip
        block.text = "\n".join(lines)


def _page_number(block: Block) -> bool:
    """A bare page number, whatever the model called it: a small model has typed "стор. 2 з 3"
    as a signature or a stamp, and "8" as a figure, or one with no text labelled "Page number"
    (m4.md, the 9B). Every export would print that as an image that doesn't exist."""
    if block.type == "table":
        return False
    return (
        PAGE_NUMBER.match(block.text) is not None
        or PAGE_NUMBER_NAME.match(block.text.strip()) is not None
        or PAGE_NUMBER_NAME.match((block.label or "").strip()) is not None
    )


def _edge_line(block: Block, edge: int) -> str:
    """A paragraph's or heading's first (edge 0) or last (-1) line, if it's short enough to be a
    running header or footer; "" otherwise."""
    if block.type not in ("paragraph", "heading") or not block.text:
        return ""
    line = block.text.split("\n")[edge]
    return line if len(line) <= RUNNING_LINE_CHARS else ""


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


def _running_keys(per_page: dict[int, list[Block]], edge: int) -> set[str]:
    """The lines (digits aside) at this edge of REPEATS pages or more, sections numbered in
    turn left out: "4. Розділ 4", "5. Розділ 5" are alike only with their numbers set aside.
    Any page typing one as a heading is enough, since cleanup doesn't type them the same way
    every time, and a running line left in can be removed in the review, where a lost heading
    can't even be noticed."""
    found: dict[str, list[tuple[str, str]]] = {}
    for blocks in per_page.values():
        if blocks and (line := _edge_line(blocks[edge], edge)):
            found.setdefault(_line_key(line), []).append((line, blocks[edge].type))
    return {
        key
        for key, lines in found.items()
        if len(lines) >= REPEATS
        and not (len({line for line, _ in lines}) > 1 and any(t == "heading" for _, t in lines))
    }


def _drop_running_lines(per_page: dict[int, list[Block]], total: int) -> None:
    """Bare page numbers at a page's top or bottom, whatever the model typed them as, or as a
    table's last row, and a short first or last line that repeats (digits aside) at the top or
    bottom of REPEATS pages or more. A shorter document keeps its lines: two pages that happen
    to start alike aren't a running header.

    Counted line by line: a header cleanup merged into the block below counts with the rest,
    and only that line goes. A block of several lines is never dropped whole: "Договір
    оренди № 17/2026\\n4. Розділі 4" is alike on four pages once digits are set aside, and its
    heading went with it (m4.md, the re-run)."""
    for edge in (0, -1):
        running = _running_keys(per_page, edge)
        for blocks in per_page.values():
            if not blocks:
                continue
            block = blocks[edge]
            numbered = _page_number(block)
            # A page's only block is its content, however alike the pages are.
            repeated = (
                len(blocks) > 1
                and "\n" not in block.text
                and _line_key(_edge_line(block, edge)) in running
            )
            if numbered or repeated:
                blocks.pop(edge)
        for blocks in per_page.values():
            if blocks and blocks[edge].type in ("paragraph", "heading"):
                _strip_running_line(blocks[edge], edge, running)
    for page, blocks in per_page.items():  # a number left at an edge after a header went
        for edge in (0, -1):
            if blocks and _page_number(blocks[edge]):
                blocks.pop(edge)
        _drop_page_number_row(blocks, page)


def _drop_page_number_row(blocks: list[Block], page: int) -> None:
    """A last row holding only the page's own number, on the table that ends the page: the 4B
    put "12" in page 12's table (m4.md). Narrow on purpose, since a wrong one would lose a value
    with nothing to show for it: a printed number that isn't the page's own stays."""
    rows = (blocks[-1].rows or []) if blocks and blocks[-1].type == "table" else []
    if len(rows) < 2 or max(len(row) for row in rows) < 2:
        return
    if [cell.strip() for cell in rows[-1] if cell.strip()] == [str(page)]:
        rows.pop()


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
