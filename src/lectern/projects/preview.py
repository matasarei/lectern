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

import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lectern.projects.text import MARKDOWN_EXTENSIONS, TEXT_EXTENSIONS, docx_markdown

Kind = Literal["markdown", "text", "html", "image", "pdf", "document", "legacy"]

HTML_EXTENSIONS = {".html", ".htm"}
DOCUMENT_EXTENSIONS = {".docx", ".odt"}
LEGACY_EXTENSIONS = {".doc", ".rtf"}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
# A document's text is shown up to this many characters, about 250 pages. Measured on this
# Mac: python-docx walks ~1 M characters a second, so the whole of a long DOCX would keep the
# page waiting for many seconds; odfdo is quicker, but react-markdown has the same text to draw.
DOCUMENT_PREVIEW_CHARS = 500_000
# A document whose XML parts unzip to more than this, together, isn't opened: a 1.3 MB DOCX of
# 8 M characters had a 16 MB document.xml, and a small zip can unzip to gigabytes. All its XML
# parts count, whatever their names: the main part's name is the package's to choose.
DOCUMENT_XML_LIMIT = 32 * 1024 * 1024


class DocumentTooLarge(Exception):
    """The document's text is too big to open for the page."""


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


def _is_xml(name: str) -> bool:
    return name.lower().endswith((".xml", ".rels"))


def document_markdown(path: Path) -> DocumentPreview:
    """A DOCX or ODT as Markdown, for the page, up to DOCUMENT_PREVIEW_CHARS. Raises
    ValueError when it can't be read, DocumentTooLarge when its text is too big to open."""
    from docx.opc.exceptions import OpcError

    suffix = path.suffix.lower()
    if suffix not in DOCUMENT_EXTENSIONS:
        raise ValueError(f"{path.name} isn't a DOCX or ODT file.")
    try:
        with zipfile.ZipFile(path) as archive:
            unzipped = sum(i.file_size for i in archive.infolist() if _is_xml(i.filename))
        if unzipped > DOCUMENT_XML_LIMIT:
            raise DocumentTooLarge(f"{path.name} has too much text to show here.")
        if suffix == ".docx":
            markdown = docx_markdown(path, limit=DOCUMENT_PREVIEW_CHARS)
        else:
            from odfdo import Document

            markdown = Document(path).to_markdown()
            if not isinstance(markdown, str):  # a spreadsheet's tables
                raise TypeError("not a text document")
    # What a broken file raises, seen on junk bytes, a zip without the document's part, bad
    # XML and a damaged or cut-off compressed part: zipfile's BadZipFile, a KeyError for the
    # missing part, python-docx's OpcError, odfdo's TypeError for a file that isn't a zip,
    # lxml's syntax errors (a SyntaxError), zlib.error and EOFError from the compressed part,
    # odfdo's NotImplementedError for a drawing or a presentation saved as .odt.
    except (
        OpcError, zipfile.BadZipFile, KeyError, TypeError, SyntaxError, zlib.error, EOFError,
        NotImplementedError, OSError,
    ) as exc:  # fmt: skip
        raise ValueError(
            f"{path.name} couldn't be read as a document ({type(exc).__name__})."
        ) from None
    if len(markdown) <= DOCUMENT_PREVIEW_CHARS:
        return DocumentPreview(markdown, cut=False)
    end = markdown.rfind("\n", 0, DOCUMENT_PREVIEW_CHARS)
    return DocumentPreview(markdown[: end if end > 0 else DOCUMENT_PREVIEW_CHARS], cut=True)
