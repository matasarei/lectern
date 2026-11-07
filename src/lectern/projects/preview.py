"""How the file tab shows a project file: its preview kind, named here so the page doesn't keep
its own lists of types.

- `markdown`, `text`: the file's text, Markdown through react-markdown (no raw HTML) and
  everything else as plain text. Code, JSON, XML and logs are text.
- `html`: its source as text in the page, and the page itself in a tab of its own, served
  sandboxed (`lectern.api.view`), never under Lectern's origin.
- `image`: the raster images the content route serves inline.
- `pdf`: the browser's own PDF viewer.
- `document`: DOCX and ODT, converted to Markdown for the page.
- `legacy`: Word 97–2003 and RTF, which nothing here reads: a note and Download.
- None: no preview.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from lectern.projects.text import MARKDOWN_EXTENSIONS, TEXT_EXTENSIONS

Kind = Literal["markdown", "text", "html", "image", "pdf", "document", "legacy"]

HTML_EXTENSIONS = {".html", ".htm"}
DOCUMENT_EXTENSIONS = {".docx", ".odt"}
LEGACY_EXTENSIONS = {".doc", ".rtf"}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


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
