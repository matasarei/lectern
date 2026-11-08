"""An HTML page with the files it links to put inside it, to open in a tab of its own.

The tab (`lectern.api.view`) can fetch nothing: a one-use link opens the page, and its policy
blocks every request. So the stylesheets, scripts, images and fonts a page names beside it in
its project are put into it before it is sent, the way they would have loaded from disk:
a stylesheet as a <style> block, the rest as data: URLs in the attribute that named them, so
a script keeps its defer/async/module timing. Only those kinds of file, never a project's
text, data or documents, so a page can't take them in and send them on (a project's images
it can: see `core.security.VIEW_CSP`).

What can't be done this way stays as written and fails to load, as it would from a folder
missing that file: anything a script asks for while it runs, `@import`, a file past the
limits, an address on the internet.

The rest of the page is left exactly as written: tags are replaced where they stand in the
source, not re-serialised. A page that gets anything put in it is sent as UTF-8, its own
<meta charset> changed to say so; one that doesn't is left alone (None).
"""

from __future__ import annotations

import base64
import codecs
import html
import posixpath
import re
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

FILE_LIMIT = 10 * 1024**2
TOTAL_LIMIT = 25 * 1024**2
PAGE_LIMIT = 10 * 1024**2  # a bigger page is sent as it is, not read into memory to parse

STYLE = {".css": "text/css"}
SCRIPT = {".js": "text/javascript", ".mjs": "text/javascript"}
IMAGE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
FONT = {".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf"}
INLINE_SUFFIXES = frozenset(STYLE | SCRIPT | IMAGE | FONT)

Read = Callable[[str], "bytes | None"]
"""A file by its path from the page's folder ("css/site.css", "../x.png"), or None when it
isn't there or isn't the page's to take: the caller decides what is in reach."""

# url(x), url('x'), url("x") in CSS
CSS_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s'"]*))\s*\)""", re.IGNORECASE)
# <meta charset=x> and <meta http-equiv=content-type content="…; charset=x">
META_CHARSET = re.compile(rb"<meta[^>]*?charset\s*=\s*[\"']?\s*([\w.:-]+)", re.IGNORECASE)
STYLE_END = re.compile(r"</(style)", re.IGNORECASE)
# one candidate in a srcset, as browsers split it: the URL runs to whitespace (commas and all,
# as in a data: URL) less any commas that end it, then the descriptor runs to the next comma
SRCSET_CANDIDATE = re.compile(r"([^\s,](?:\S*[^\s,])?)(?:,+|\s+([^,]*))?")
# an attribute's name, as written, in a start tag after its tag name
ATTR_NAME = re.compile(r"""[\s/]*([^\s/>"'=]+)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*))?""")
UTF8_META = '<meta charset="utf-8">'


def inline_page(page: bytes, read: Read) -> bytes | None:
    """The page as UTF-8 with its side files inside it, or None when it links to none that
    could be had."""
    parser = _Page(_decode(page), _Files(read))
    parser.run()
    if not parser.edits:
        return None
    pieces, done = [], 0
    for start, end, replacement in sorted(parser.edits + parser.charsets):
        pieces += [parser.text[done:start], replacement]
        done = end
    pieces.append(parser.text[done:])
    return "".join(pieces).encode("utf-8")


class _Files:
    """The side files read so far, and how much has gone into the page."""

    def __init__(self, read: Read) -> None:
        self._read = read
        self._read_already: dict[str, bytes | None] = {}
        self._total = 0

    def take(self, base: str, url: str, kinds: dict[str, str]) -> tuple[str, bytes] | None:
        """The path and bytes of the file a URL names from the folder `base`, when it is one
        of `kinds` and within the limits; counted toward the total."""
        path = _local_path(base, url)
        if path is None or posixpath.splitext(path)[1].lower() not in kinds:
            return None
        if path not in self._read_already:
            content = self._read(path)
            fits = content is not None and len(content) <= FILE_LIMIT
            self._read_already[path] = content if fits else None
        content = self._read_already[path]
        if content is None or self._total + len(content) > TOTAL_LIMIT:
            return None
        self._total += len(content)
        return path, content

    def data_url(self, base: str, url: str, kinds: dict[str, str]) -> str | None:
        found = self.take(base, url, kinds)
        if found is None:
            return None
        path, content = found
        mime = kinds[posixpath.splitext(path)[1].lower()]
        return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"

    def css(self, css: str, base: str) -> str:
        """CSS with the images and fonts its url()s name put inside it, resolved from `base`,
        the folder of the file the CSS came from."""

        def swap(match: re.Match[str]) -> str:
            url = next(group for group in match.groups() if group is not None)
            inlined = self.data_url(base, url, IMAGE | FONT)
            return match.group(0) if inlined is None else f"url({inlined})"

        return CSS_URL.sub(swap, css)


def _local_path(base: str, url: str) -> str | None:
    """The path a relative URL names from the folder `base`, or None for anything else: an
    address with a scheme or host, a path from the root, a bare #fragment or ?query."""
    parts = urlsplit(url.strip().replace("\\", "/"))
    if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
        return None
    return posixpath.normpath(posixpath.join(base, unquote(parts.path)))


class _Page(HTMLParser):
    """Finds what to put in the page, as (start, end, replacement) edits of its text."""

    def __init__(self, text: str, files: _Files) -> None:
        super().__init__(convert_charrefs=True)
        self.text = text
        self.edits: list[tuple[int, int, str]] = []
        self.charsets: list[tuple[int, int, str]] = []
        self._files = files
        self._lines = [0] + [m.end() for m in re.finditer("\n", text)]
        self._style_at: int | None = None

    def run(self) -> None:
        self.feed(self.text)
        self.close()

    def _at(self) -> int:
        line, column = self.getpos()
        return self._lines[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style" and self._style_at is not None:
            start, end = self._style_at, self._at()
            css = self.text[start:end]
            inlined = self._files.css(css, "")
            if inlined != css:
                self.edits.append((start, end, inlined))
            self._style_at = None

    def _tag(self, tag: str, attrs: list[tuple[str, str | None]], closed: bool) -> None:
        start = self._at()
        end = start + len(self.get_starttag_text() or "")
        values = dict(attrs)
        rel = (values.get("rel") or "").lower().split()

        if tag == "style" and not closed:
            self._style_at = end
            return
        if tag == "meta" and _names_charset(values):
            self.charsets.append((start, end, UTF8_META))
            return
        if tag == "link" and "stylesheet" in rel and "alternate" not in rel:
            stylesheet = self._stylesheet(values)
            if stylesheet is not None:
                self.edits.append((start, end, stylesheet))
            return

        changed = False
        new: list[tuple[str, str | None]] = []
        for name, value in attrs:
            swapped = None if value is None else self._attribute(tag, rel, name, value)
            changed = changed or swapped is not None
            new.append((name, value if swapped is None else swapped))
        if changed:
            source = self.get_starttag_text() or ""
            self.edits.append((start, end, _start_tag(source, tag, new, closed)))

    def _attribute(self, tag: str, rel: list[str], name: str, value: str) -> str | None:
        """The attribute's value with its file put inside it, or None to leave it."""
        files = self._files
        if name == "style":
            css = files.css(value, "")
            return None if css == value else css
        if (tag, name) == ("script", "src"):
            return files.data_url("", value, SCRIPT)
        if (tag, name) in {("img", "src"), ("source", "src"), ("video", "poster")}:
            return files.data_url("", value, IMAGE)
        if (tag, name) == ("link", "href") and "icon" in rel:
            return files.data_url("", value, IMAGE)
        if tag in {"img", "source"} and name == "srcset":
            return self._srcset(value)
        return None

    def _srcset(self, srcset: str) -> str | None:
        candidates, changed = [], False
        for url, descriptor in SRCSET_CANDIDATE.findall(srcset):
            inlined = self._files.data_url("", url, IMAGE)
            changed = changed or inlined is not None
            candidates.append(" ".join(filter(None, [inlined or url, descriptor.strip()])))
        return ", ".join(candidates) if changed else None

    def _stylesheet(self, values: dict[str, str | None]) -> str | None:
        found = self._files.take("", values.get("href") or "", STYLE)
        if found is None:
            return None
        path, content = found
        css = self._files.css(
            content.decode("utf-8-sig", errors="replace"), posixpath.dirname(path)
        )
        media = values.get("media")
        opening = "<style>" if media is None else f'<style media="{html.escape(media)}">'
        # The file can't end the block it's put in; "\/" is "/" to CSS.
        return f"{opening}{STYLE_END.sub(r'<\\/\1', css)}</style>"


def _names_charset(values: dict[str, str | None]) -> bool:
    if "charset" in values:
        return True
    equiv = (values.get("http-equiv") or "").lower()
    return equiv == "content-type" and "charset" in (values.get("content") or "").lower()


def _start_tag(source: str, tag: str, attrs: list[tuple[str, str | None]], closed: bool) -> str:
    """The tag rebuilt with `attrs`, its names spelled as `source` spells them: the parser
    lowercases them, and inside inline SVG viewBox is not viewbox."""
    written = source[1 : 1 + len(tag)]
    tag = written if written.lower() == tag else tag
    names = ATTR_NAME.findall(source, 1 + len(tag))
    if [name.lower() for name in names] != [name for name, _ in attrs]:
        names = [name for name, _ in attrs]  # read differently from the parser: keep its names
    pairs = zip(names, (value for _, value in attrs), strict=True)
    parts = [tag] + [n if v is None else f'{n}="{html.escape(v)}"' for n, v in pairs]
    # Closed stays closed: inside inline SVG, <rect … /> without the slash would wrap the rest.
    return f"<{' '.join(parts)}{' /' if closed else ''}>"


def _decode(page: bytes) -> str:
    """The page's text, read the way a browser reads a local file: a BOM, then the charset
    its <meta> names near the top, then UTF-8 if it is valid, then windows-1252."""
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if page.startswith(bom):
            return page[len(bom) :].decode(encoding, errors="replace")
    declared = _declared_encoding(page[:1024])
    if declared is not None:
        return page.decode(declared, errors="replace")
    try:
        return page.decode("utf-8")
    except UnicodeDecodeError:
        return page.decode("cp1252", errors="replace")


def _declared_encoding(head: bytes) -> str | None:
    match = META_CHARSET.search(head)
    if match is None:
        return None
    try:
        name = codecs.lookup(match.group(1).decode("ascii")).name
        # base64, zlib and the like are codecs but not text encodings (b"" isn't checked)
        b"a".decode(name, errors="ignore")
    except (LookupError, UnicodeDecodeError):
        return None
    # As browsers do: Latin-1 and ASCII mean windows-1252, and a page that says UTF-16 in
    # bytes it could read as ASCII is not UTF-16.
    if name in {"iso8859-1", "ascii"}:
        return "cp1252"
    if name.startswith(("utf-16", "utf-32")):
        return "utf-8"
    return name
