"""Exporting a Document: the contract every export module follows, and what they share.

An exporter takes a Document and nothing else: never raw OCR text, never another
exporter's output (blueprint, "Export modules"). It declares what it can represent
(`capabilities`); what it can't is printed as a visible placeholder, never dropped:
- an open question is a comment holding its candidates where the format has comments, and
  an inline mark (⟦1? → 10 · 15 · 18⟧) otherwise. It is never written as one of its
  candidates: a guess is not a fact;
- an answered question is its value;
- a figure is "[Image 1: stamp, page 3]" until images are cut out of scans;
- page notes are left out unless asked for; then they are comments, or quoted paragraphs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from lectern.documents.document import DOUBT_MARK, Block, Document, Doubt
from lectern.documents.from_text import parse_markdown, parse_text
from lectern.projects import files

CAPABILITIES = ("tables", "comments", "figures")
DOCUMENT_JSON = "document.json"
DOCUMENT_MD = "document.md"


class ExportOptions(BaseModel):
    """What the agent may choose (its JSON schema is shown by export.formats)."""

    open_doubts: Literal["comment", "inline"] | None = None  # None: comment where possible
    notes: Literal["omit", "comment"] = "omit"


@runtime_checkable
class Exporter(Protocol):
    format: str  # "odt"
    title: str  # "Export · ODT", the From column of the files it writes
    extension: str  # ".odt"
    mime: str
    capabilities: frozenset[str]  # from CAPABILITIES

    def export(self, doc: Document, opts: ExportOptions) -> bytes: ...


def resolved_options(opts: ExportOptions, exporter: Exporter) -> ExportOptions:
    """The options with their defaults filled in for this exporter."""
    if opts.open_doubts is not None:
        return opts
    default = "comment" if "comments" in exporter.capabilities else "inline"
    return opts.model_copy(update={"open_doubts": default})


def lost(doc: Document, opts: ExportOptions, exporter: Exporter) -> list[str]:
    """What this format can't keep of what was asked, in words for the confirmation card.
    Empty when nothing is lost; export.run asks first only when it isn't."""
    missing = []
    if "comments" not in exporter.capabilities:
        if opts.open_doubts == "comment" and doc.open_doubts():
            missing.append("comments: the open questions become inline marks")
        if opts.notes == "comment" and any(p.note.strip() for p in doc.pages):
            missing.append("comments: the page notes become paragraphs")
    return missing


def segments(text: str, doubts: list[Doubt]) -> list[tuple[str, Doubt | None]]:
    """A block's text (or a cell's) as plain runs and open questions. An answered question
    reads as its value; a {dN} with no such doubt stays as written."""
    by_id = {d.id: d for d in doubts}
    pieces: list[tuple[str, Doubt | None]] = []

    def plain(run: str) -> None:
        if not run:
            return
        if pieces and pieces[-1][1] is None:
            pieces[-1] = (pieces[-1][0] + run, None)
        else:
            pieces.append((run, None))

    at = 0
    for match in DOUBT_MARK.finditer(text):
        plain(text[at : match.start()])
        doubt = by_id.get(match.group(1))
        if doubt is None:
            plain(match.group(0))
        elif doubt.status == "resolved" and doubt.value is not None:
            plain(doubt.value)
        else:
            pieces.append((doubt.raw, doubt))
        at = match.end()
    plain(text[at:])
    return pieces


def inline_doubt(doubt: Doubt) -> str:
    """An open question written into the text, where there are no comments."""
    candidates = " · ".join(doubt.candidates)
    return f"⟦{doubt.raw} → {candidates}⟧" if candidates else f"⟦{doubt.raw}⟧"


def doubt_comment(doubt: Doubt) -> str:
    """An open question as a comment's text."""
    text = "Open question."
    if doubt.candidates:
        text += f" Could be: {' · '.join(doubt.candidates)}."
    if doubt.reason:
        text += f" {doubt.reason}"
    return text


def figure_placeholder(block: Block, number: int) -> str:
    return f"[Image {number}: {block.label or 'image'}, page {block.page}]"


def note_text(page: int, note: str) -> str:
    return f"Note for page {page}: {note.strip()}"


def stray_block(block: Block) -> Block | None:
    """A paragraph holding the block's open questions whose mark isn't anywhere the block is
    printed (a table's text: tables print their rows; a figure), or None. A Document
    assembled before that was fixed still has such questions; they come out after their
    block, never dropped."""
    printed = (
        " ".join(c for row in block.rows or [] for c in row) if block.type == "table" else
        "" if block.type == "figure" else block.text
    )  # fmt: skip
    strays = [d for d in block.doubts if d.status == "open" and f"{{{d.id}}}" not in printed]
    if not strays:
        return None
    marks = " ".join(f"{{{d.id}}}" for d in strays)
    return Block(id=block.id, type="paragraph", page=block.page, text=f"Not shown above: {marks}",
                 doubts=strays)  # fmt: skip


def reading_order(doc: Document) -> list[tuple[Block, str | None, int]]:
    """Each block once, in order, with the note of the page it starts (when that page has
    one and this is its first block) and its figure number (0 for other blocks). A block
    with open questions it doesn't print is followed by them (`stray_block`)."""
    notes = {p.n: p.note for p in doc.pages if p.note.strip()}
    seen_pages: set[int] = set()
    figures = 0
    order = []
    for block in doc.blocks:
        note = None
        if block.page not in seen_pages:
            seen_pages.add(block.page)
            note = notes.get(block.page)
        if block.type == "figure":
            figures += 1
        order.append((block, note, figures if block.type == "figure" else 0))
        if (stray := stray_block(block)) is not None:
            order.append((stray, None, 0))
    return order


def load_for_export(root: Path, path: str) -> Document:
    """The Document an export is made from: an OCR session's (its document.md, whose
    document.json beside it is the source), or a Markdown or plain text file parsed into
    one. Raises ValueError with a reason the agent can pass on."""
    target = files.inside(root, path)
    name = Path(path)
    if name.name == DOCUMENT_MD and (target.parent / DOCUMENT_JSON).is_file():
        return Document.from_json((target.parent / DOCUMENT_JSON).read_text(encoding="utf-8"))
    suffix = name.suffix.lower()
    if suffix in (".md", ".markdown", ".txt") and target.is_file():
        text = target.read_text(encoding="utf-8", errors="replace")
        parse = parse_text if suffix == ".txt" else parse_markdown
        return parse(name.stem, text, source=name.name)
    raise ValueError(
        f"{path} isn't a Document or a text file: export an OCR session's document "
        "(its folder) or a Markdown or text file."
    )


def file_title(doc: Document) -> str:
    """The exported file's name, without its extension: the Document's title, made safe."""
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", doc.title).strip(" .")
    return title or "document"
