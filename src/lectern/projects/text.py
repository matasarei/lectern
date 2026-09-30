"""Text from a project file, for the chat to read and for search.

Plain text, Markdown and code are read as they are. PDFs give their text layer (a scan
without one has no text until it goes through OCR), DOCX its paragraphs and tables. The text
comes back in chunks: one per Markdown section, PDF page or ~1,500 characters, each with a
block reference `#c<n>` that search results and `document.read` use.
"""

from __future__ import annotations

import mimetypes
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

TEXT_LIMIT = 2 * 1024 * 1024  # characters of text kept per file
CHUNK_CHARS = 1500
# A PDF page with fewer characters than this is treated as having no text layer.
PDF_PAGE_MIN_CHARS = 20

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml",
    ".xml", ".html", ".htm", ".ini", ".cfg", ".log", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".css", ".sql", ".sh", ".ps1", ".bat", ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".php", ".rb", ".swift", ".tex",
}  # fmt: skip
# One PDFium call at a time, process-wide. The OCR pipeline (M2) must take it too.
PDFIUM_LOCK = threading.Lock()
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_OVERRIDES = {".md": "text/markdown", ".markdown": "text/markdown", ".docx": DOCX_MIME}


# A Document's block anchor in document.md: {#b3.2 p=3}, {#b3.3 p=3→4}, {#b3.4 p=3 figure}
ANCHOR = re.compile(r"\{#((?:b|p)\d+(?:\.\d+)?) p=[\d→]+(?: [a-z]+)?\}")
NOTE = re.compile(r"^> Note for page (\d+)")


@dataclass(frozen=True)
class Chunk:
    number: int  # 1-based; an ordinary file's block reference is #c<number>
    heading: str  # a Markdown heading, "Page 3", or ""
    body: str
    label: str | None = None  # a Document's block id ("b3.2"), cited as #b3.2 instead

    @property
    def ref(self) -> str:
        return f"#{self.label}" if self.label else f"#c{self.number}"


@dataclass
class Extracted:
    chunks: list[Chunk] = field(default_factory=list)
    note: str | None = None  # why there's no text, or that it was cut

    @property
    def has_text(self) -> bool:
        return bool(self.chunks)


def mime_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in MIME_OVERRIDES:
        return MIME_OVERRIDES[suffix]
    guessed, _ = mimetypes.guess_type(name)
    if guessed:
        return guessed
    return "text/plain" if suffix in TEXT_EXTENSIONS else "application/octet-stream"


def extract(path: Path) -> Extracted:
    """The file's text in chunks, or a note saying why there is none."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf(path)
    if suffix == ".docx":
        return _limited(_docx(path), markdown=True)
    if suffix in TEXT_EXTENSIONS or mime_for(path.name).startswith("text/"):
        text = _read_text(path)
        if text is None:
            return Extracted(note="not a text file")
        if suffix in MARKDOWN_EXTENSIONS and ANCHOR.search(text):
            return Extracted(chunks=_document_chunks(text[:TEXT_LIMIT]))
        return _limited(text, markdown=suffix in MARKDOWN_EXTENSIONS)
    return Extracted(note="no text to read in this kind of file")


def _read_text(path: Path) -> str | None:
    with path.open("rb") as f:
        raw = f.read(TEXT_LIMIT * 4 + 1)
    if b"\x00" in raw[:8192]:
        return None  # binary
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _limited(text: str, *, markdown: bool) -> Extracted:
    note = None
    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT]
        note = f"only the first {TEXT_LIMIT // (1024 * 1024)} MB of text is used"
    sections = _markdown_sections(text) if markdown else [("", text)]
    return Extracted(chunks=_number(_split(sections)), note=note)


def _document_chunks(text: str) -> list[Chunk]:
    """A Document's document.md, one chunk per block, labelled with the block's id so the
    chat reads and cites it as #b3.2. What comes before the first block (the front matter) is
    a chunk of its own, and each page note is one too (#note3)."""
    pieces: list[tuple[str, str, str | None]] = []  # heading, body, label
    heading, label, lines = "About this document", None, []

    def flush() -> None:
        if label or "".join(lines).strip():
            pieces.append((heading, "\n".join(lines).strip(), label))

    for line in text.splitlines():
        anchor = ANCHOR.search(line)
        note = NOTE.match(line)
        if anchor and line.lstrip().startswith(("{#", "#")):
            flush()
            label = anchor.group(1)
            rest = ANCHOR.sub("", line).strip()
            if rest.startswith("#"):
                heading = rest.lstrip("#").strip()
            lines = [rest] if rest else []
        elif note:
            flush()
            label, lines = f"note{note.group(1)}", [line]
        else:
            lines.append(line)
    flush()
    return [Chunk(i, h, b, lb) for i, (h, b, lb) in enumerate(pieces, start=1)]


def _markdown_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    heading, lines = "", []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            if "".join(lines).strip() or heading:
                sections.append((heading, "\n".join(lines)))
            heading, lines = match.group(1), []
        else:
            lines.append(line)
    if "".join(lines).strip() or heading:
        sections.append((heading, "\n".join(lines)))
    return sections


def _split(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Sections longer than CHUNK_CHARS are cut on paragraph boundaries (or hard, for a
    paragraph that is itself too long)."""
    pieces: list[tuple[str, str]] = []
    for heading, body in sections:
        current = ""
        for paragraph in re.split(r"\n\s*\n", body.strip()):
            while len(paragraph) > CHUNK_CHARS:
                if current:
                    pieces.append((heading, current))
                    current = ""
                pieces.append((heading, paragraph[:CHUNK_CHARS]))
                paragraph = paragraph[CHUNK_CHARS:]
            if current and len(current) + len(paragraph) + 2 > CHUNK_CHARS:
                pieces.append((heading, current))
                current = ""
            current = f"{current}\n\n{paragraph}" if current else paragraph
        if current or heading:
            pieces.append((heading, current))
    return pieces


def _number(pieces: list[tuple[str, str]]) -> list[Chunk]:
    return [Chunk(i, heading, body) for i, (heading, body) in enumerate(pieces, start=1)]


def _pdf(path: Path) -> Extracted:
    # PDFium isn't thread-safe (pypdfium2's own docs), and imports run in worker threads.
    with PDFIUM_LOCK:
        return _pdf_text(path)


def _pdf_text(path: Path) -> Extracted:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(path)
    except pdfium.PdfiumError:
        return Extracted(note="the PDF couldn't be opened")
    try:
        pages: list[tuple[str, str]] = []
        total = 0
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            text = textpage.get_text_bounded().replace("\r\n", "\n").strip()
            textpage.close()
            page.close()
            pages.append((f"Page {index + 1}", text))
            total += len(text)
            if total > TEXT_LIMIT:
                break
        if sum(len(t) >= PDF_PAGE_MIN_CHARS for _, t in pages) == 0:
            return Extracted(note="no text layer (a scan: open it as an OCR session)")
        chunks = _number(_split([(h, t) for h, t in pages if t]))
        note = None
        if len(pages) < len(pdf):
            note = f"only the first {len(pages)} of {len(pdf)} pages are used"
        return Extracted(chunks=chunks, note=note)
    finally:
        pdf.close()


def _docx(path: Path) -> str:
    """Paragraphs and tables in document order, headings as Markdown headings."""
    import docx
    from docx.table import Table

    document = docx.Document(str(path))
    lines: list[str] = []
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            for row in item.rows:
                lines.append(" | ".join(cell.text.strip() for cell in row.cells))
            lines.append("")
            continue
        text = item.text.strip()
        style = (item.style.name if item.style is not None else "") or ""
        level = re.match(r"Heading (\d)", style)
        if level and text:
            lines.append(f"{'#' * int(level.group(1))} {text}")
        elif style == "Title" and text:
            lines.append(f"# {text}")
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines)
