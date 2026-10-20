"""A fetched web page as text the model can read and the project can keep: its title, and
its words as Markdown-like text (headings, paragraphs, lists, code blocks, table rows). What
isn't the page's content — scripts, styles, menus, headers and footers, forms — is dropped,
and so are the links' addresses (their words stay)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

# Not the page's content, so dropped with everything inside them: these elements, and any
# element marked as one of these roles (a <div role="navigation"> is a menu too) or hidden.
SKIP = frozenset({
    "script", "style", "noscript", "template", "nav", "header", "footer", "aside", "form",
    "svg", "iframe", "select", "button",
})  # fmt: skip
SKIP_ROLES = frozenset({"navigation", "banner", "contentinfo", "search", "complementary"})
# Never closed, so never the end of something skipped.
VOID = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
    "track", "wbr",
})  # fmt: skip
HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# Start a new paragraph.
BLOCKS = frozenset({
    "p", "div", "section", "article", "main", "blockquote", "ul", "ol", "dl", "dt", "dd",
    "table", "figure", "figcaption", "hr", "details", "summary", "address",
})  # fmt: skip
FENCE = "```"
META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.IGNORECASE)
SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Page:
    title: str
    text: str


def decode(data: bytes, charset: str | None) -> str:
    """The page's bytes as text: by the charset the server named, else the one the page's
    <meta> names, else UTF-8. Bytes that don't fit are replaced, never an error."""
    meta = META_CHARSET.search(data[:4096])
    for name in (charset, meta.group(1).decode("ascii") if meta else None):
        if name:
            try:
                return data.decode(name, errors="replace").removeprefix("﻿")
            except LookupError:  # a charset Python doesn't know
                continue
    return data.decode("utf-8", errors="replace").removeprefix("﻿")


def _dropped(tag: str, attrs: dict[str, str | None]) -> bool:
    return (
        tag in SKIP
        or (attrs.get("role") or "").lower() in SKIP_ROLES
        or "hidden" in attrs
        or (attrs.get("aria-hidden") or "").lower() == "true"
    )


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title: list[str] = []
        # The element being dropped, and how many of its kind are open inside it. Only its
        # own kind is counted: pages leave <p> and <li> unclosed, never a <div>.
        self._skip: tuple[str, int] | None = None
        self._pre = 0  # inside a <pre>: whitespace is kept
        self._in_title = False
        self._cells = 0  # cells written in this table row

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip is not None:
            if tag == self._skip[0]:
                self._skip = (tag, self._skip[1] + 1)
            return
        if tag not in VOID and _dropped(tag, dict(attrs)):
            self._skip = (tag, 1)
            return
        if tag == "title":
            self._in_title = True
        elif tag in HEADINGS:
            self.out.append(f"\n\n{'#' * int(tag[1])} ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "tr":
            self.out.append("\n")
            self._cells = 0
        elif tag in ("td", "th"):
            if self._cells:
                self.out.append(" | ")
            self._cells += 1
        elif tag == "pre":
            self.out.append(f"\n\n{FENCE}\n")
            self._pre += 1
        elif tag == "code" and not self._pre:
            self.out.append("`")
        elif tag == "br":
            self.out.append("\n")
        elif tag in BLOCKS:
            self.out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip is not None:
            if tag == self._skip[0]:
                depth = self._skip[1] - 1
                self._skip = (tag, depth) if depth else None
            return
        if tag == "title":
            self._in_title = False
        elif tag == "pre" and self._pre:
            self._pre -= 1
            self.out.append(f"\n{FENCE}\n\n")
        elif tag == "code" and not self._pre:
            self.out.append("`")
        elif tag in HEADINGS or tag in BLOCKS:
            self.out.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip is not None:
            return
        if self._in_title:
            self.title.append(data)
        elif self._pre:
            self.out.append(data)
        else:
            self.out.append(SPACES.sub(" ", data))


def _tidy(text: str) -> str:
    """Trim each line and keep at most one blank line in a row; a code block is kept as it
    was written."""
    lines: list[str] = []
    in_code = False
    for line in text.split("\n"):
        if line.strip() == FENCE:
            if in_code:  # the block's own first and last newlines aren't part of the code
                while lines[-1] == "":
                    lines.pop()
            in_code = not in_code
            lines.append(FENCE)
            continue
        if in_code:
            if line.strip() or lines[-1] != FENCE:
                lines.append(line.rstrip())
            continue
        line = SPACES.sub(" ", line).strip()
        if line == "-" or (line and not line.strip("# ")):
            continue  # a list item or heading with no words: an icon, a link to a skipped part
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def to_text(html: str) -> Page:
    """A page's title and its content as text."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    return Page(title=" ".join("".join(parser.title).split()), text=_tidy("".join(parser.out)))
