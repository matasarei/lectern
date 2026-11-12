"""How the file tab shows a project file: its preview kind, named here so the page doesn't keep
its own lists of types.

- `markdown`, `text`: the file's text, Markdown through react-markdown (no raw HTML) and
  everything else as plain text. Code, JSON, XML and logs are text.
- `html`: its source as text in the page, and the page itself in a tab of its own, served
  sandboxed (`lectern.api.view`), never under Lectern's origin.
- `image`: the raster images the content route serves inline.
- `pdf`: the browser's own PDF viewer.
- `document`: DOCX and ODT, converted to Markdown here (`document_markdown`).
- `legacy`: Word 97–2003 and RTF, which nothing here reads: a note and Download.
- None: no preview.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lectern.projects.text import (
    MARKDOWN_EXTENSIONS,
    TEXT_EXTENSIONS,
    docx_markdown,
    odt_markdown,
    read_document,
)

Kind = Literal["markdown", "text", "html", "image", "pdf", "document", "legacy"]

HTML_EXTENSIONS = {".html", ".htm"}
DOCUMENT_EXTENSIONS = {".docx", ".odt"}
LEGACY_EXTENSIONS = {".doc", ".rtf"}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
# A document's text is shown up to this many characters, about 250 pages. Measured on this
# Mac: python-docx walks ~1 M characters a second, so the whole of a long DOCX would keep the
# page waiting for many seconds; odfdo is quicker, but react-markdown has the same text to draw.
DOCUMENT_PREVIEW_CHARS = 500_000


@dataclass(frozen=True)
class DocumentPreview:
    markdown: str
    cut: bool  # only the beginning is shown; the rest is in the file


def preview_kind(name: str, mime: str) -> Kind | None:
    suffix = Path(name).suffix.lower()
    if suffix in MARKDOWN_EXTENSIONS:
        return "markdown"
    if suffix in HTML_EXTENSIONS:
        return "html"
    if suffix == ".pdf":
        return "pdf"
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    if suffix in LEGACY_EXTENSIONS:
        return "legacy"
    if mime in IMAGE_MIMES:
        return "image"
    if suffix in TEXT_EXTENSIONS or mime.startswith("text/"):
        return "text"
    return None


def document_markdown(path: Path) -> DocumentPreview:
    """A DOCX or ODT as Markdown, for the page, up to DOCUMENT_PREVIEW_CHARS. Raises
    ValueError when it can't be read, DocumentTooLarge when its text is too big to open."""
    suffix = path.suffix.lower()
    if suffix not in DOCUMENT_EXTENSIONS:
        raise ValueError(f"{path.name} isn't a DOCX or ODT file.")
    if suffix == ".docx":
        markdown = read_document(path, lambda p: docx_markdown(p, limit=DOCUMENT_PREVIEW_CHARS))
    else:
        markdown = read_document(path, odt_markdown)
    if len(markdown) <= DOCUMENT_PREVIEW_CHARS:
        return DocumentPreview(markdown, cut=False)
    end = markdown.rfind("\n", 0, DOCUMENT_PREVIEW_CHARS)
    return DocumentPreview(markdown[: end if end > 0 else DOCUMENT_PREVIEW_CHARS], cut=True)
