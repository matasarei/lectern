"""Changing a PDF's text in place, so it looks as it did: same font, size, colour and position
(docs/acceptance/pdf-edit.md says why it is built on PDFium).

A PDF has no words, only text objects: runs of glyphs, often a word or a few letters each,
in a font that is usually a subset holding only the letters the document uses. An edit is
found in PDFium's text page, where each character knows the object that draws it (a `find`
a letter or two off is answered with the exact text, not changed). The objects
under it are replaced by one new object, at the first one's position and in its font, size,
colour and render mode. When that font lacks a new letter, the installed copy of the same font
(same PostScript name) is embedded, cut down to the letters needed; with neither, the edit is
refused. The text after the edit on the same line moves by the change in width, up to a gap
wide enough to be a column or a table cell; running past that gap or the page's text margin
is reported, for the person to agree to.

Only text on one line is replaced: no reflow onto other lines, no new paragraphs, no styles.
If any edit can't be made, none is: the caller gets every reason at once.

Every function here holds projects.text.PDFIUM_LOCK while it uses PDFium.
"""

from __future__ import annotations

import ctypes
import difflib
import io
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from lectern.modules.pdf_edit import fonts as font_index
from lectern.projects.text import PDF_PAGE_MIN_CHARS, PDFIUM_LOCK

CLOSE_ENOUGH = 0.9  # how alike a `find` and the PDF's text must be to be named as meant
COLUMN_GAP_EMS = 1.5  # a gap this wide (in the edited text's size) ends the text that moves
MARGIN_SLACK_PT = 0.5
ZERO_WIDTH_PT = 0.05  # a letter this narrow has no width in its font
QUOTE_CHARS = 90  # how much of a line an error quotes
# A near match and the nearest lines are looked for on the page given, or the first this many:
# both compare every line, and the whole time PDFium is locked for the process.
NEAR_PAGES = 30


class EditFailed(ValueError):
    """An edit that can't be made, said so the chat model can fix its request."""


@dataclass(frozen=True)
class Edit:
    find: str
    replace: str
    page: int | None = None  # 1-based; None looks in the whole PDF
    all: bool = False  # every place `find` is, rather than exactly one


@dataclass
class Change:
    """One place changed: the line before and after, the font it was drawn in, and what the
    person should know (`notes`) or agree to (`degraded`)."""

    page: int
    before: str
    after: str
    font: str
    notes: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)


@dataclass
class Result:
    data: bytes  # the edited PDF
    changes: list[Change]

    @property
    def degraded(self) -> list[str]:
        return [f"p{c.page} “{c.after.strip()}”: {d}" for c in self.changes for d in c.degraded]


def apply_edits(
    path: Path, edits: list[Edit], fonts: font_index.InstalledFonts | None = None
) -> Result:
    """The PDF at `path` with `edits` made, in order, as new bytes; the file isn't touched.
    Raises EditFailed listing every edit that can't be made, and then makes none."""
    if not edits:
        raise EditFailed("No edits were given.")
    fonts = fonts or font_index.installed()
    fonts.load()  # the first time, before the lock: it opens every installed font file
    with PDFIUM_LOCK:
        pdf = _open(path)
        editor = _Editor(pdf, fonts)
        try:
            changes: list[Change] = []
            problems: list[str] = []
            for edit in edits:
                try:
                    changes.extend(editor.apply(edit))
                except EditFailed as exc:
                    problems.append(f"“{edit.find}” → “{edit.replace}”: {exc}")
            if problems:
                raise EditFailed("Nothing was changed. " + " ".join(problems))
            return Result(editor.save(), changes)
        finally:
            editor.close()


def _open(path: Path) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(path)
    except pdfium.PdfiumError:
        raise EditFailed(
            f"{path.name} couldn't be opened as a PDF (it may be password-protected or damaged)."
        ) from None


# --- What a page says, character by character ----------------------------------------------


@dataclass(frozen=True)
class _Char:
    text: str
    obj: int  # the text object drawing it (its address); 0 for a space PDFium added
    box: tuple[float, float, float, float]  # left, right, bottom, top


@dataclass
class _Line:
    text: str
    chars: list[_Char]


@dataclass
class _PageText:
    lines: list[_Line]
    handles: dict[int, object]  # text object address → its PDFium handle
    right: float  # the right edge of the page's text

    def chars_of(self, obj: int) -> int:
        return sum(1 for line in self.lines for c in line.chars if c.obj == obj)


def _addr(handle: object) -> int:
    return ctypes.cast(handle, ctypes.c_void_p).value or 0


def _read(textpage: pdfium.PdfTextPage) -> _PageText:
    raw = textpage.raw
    lines: list[_Line] = []
    handles: dict[int, object] = {}
    chars: list[_Char] = []
    right = 0.0
    left_, right_, bottom_, top_ = (ctypes.c_double() for _ in range(4))
    for i in range(pdfium_c.FPDFText_CountChars(raw)):
        text = chr(pdfium_c.FPDFText_GetUnicode(raw, i))
        if text in "\r\n":
            if text == "\r" or chars:
                lines.append(_Line("".join(c.text for c in chars), chars))
                chars = []
            continue
        obj = 0
        if pdfium_c.FPDFText_IsGenerated(raw, i) != 1:
            handle = pdfium_c.FPDFText_GetTextObject(raw, i)
            obj = _addr(handle)
            if obj:
                handles.setdefault(obj, handle)
        pdfium_c.FPDFText_GetCharBox(
            raw, i, *(ctypes.byref(v) for v in (left_, right_, bottom_, top_))
        )
        box = (left_.value, right_.value, bottom_.value, top_.value)
        if obj and not text.isspace():
            right = max(right, box[1])
        chars.append(_Char(text, obj, box))
    if chars:
        lines.append(_Line("".join(c.text for c in chars), chars))
    return _PageText(lines, handles, right)


# --- Finding where an edit goes --------------------------------------------------------------


@dataclass(frozen=True)
class _Spot:
    page: int  # 0-based
    line: int
    start: int
    end: int
    found: str  # the text there


def _quote(text: str) -> str:
    text = text.strip()
    return f"“{text[:QUOTE_CHARS]}…”" if len(text) > QUOTE_CHARS else f"“{text}”"


def _shared(text: str, find: str) -> int:
    """How long a stretch of `find` the text has, to rank lines by likeness."""
    return difflib.SequenceMatcher(None, text, find).find_longest_match().size


def _occurrences(text: str, find: str) -> list[int]:
    found, at = [], text.find(find)
    while at >= 0:
        found.append(at)
        at = text.find(find, at + len(find))
    return found


class _Editor:
    def __init__(self, pdf: pdfium.PdfDocument, fonts: font_index.InstalledFonts) -> None:
        self._pdf = pdf
        self._fonts = fonts
        self._pages: dict[int, pdfium.PdfPage] = {}
        self._texts: dict[int, _PageText] = {}  # a page being changed, until it changes
        self._line_texts: dict[int, list[str]] = {}  # every page searched, as text only
        self._keep: list[object] = []  # ctypes buffers PDFium reads until the save
        self._loaded = 0  # fonts embedded so far, for unique subset names

    # Pages stay open for the editor's life: an edit lives in the page until it is saved.
    def _page(self, index: int) -> pdfium.PdfPage:
        if index not in self._pages:
            self._pages[index] = self._pdf[index]
        return self._pages[index]

    def _text(self, index: int) -> _PageText:
        if index not in self._texts:
            textpage = self._page(index).get_textpage()
            try:
                self._texts[index] = _read(textpage)
            finally:
                textpage.close()
        return self._texts[index]

    def _lines(self, index: int) -> list[str]:
        """A page's lines, for finding where an edit goes. Only a page being changed keeps its
        characters and stays open, so a long PDF is searched a page at a time."""
        if index in self._texts:
            return [line.text for line in self._texts[index].lines]
        if index not in self._line_texts:
            page = self._pages.get(index) or self._pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    self._line_texts[index] = [line.text for line in _read(textpage).lines]
                finally:
                    textpage.close()
            finally:
                if index not in self._pages:
                    page.close()
        return self._line_texts[index]

    def _changed(self, index: int) -> None:
        self._texts.pop(index, None)
        self._line_texts.pop(index, None)

    def save(self) -> bytes:
        out = io.BytesIO()
        self._pdf.save(out)
        return out.getvalue()

    def close(self) -> None:
        for page in self._pages.values():
            page.close()
        self._pdf.close()

    def apply(self, edit: Edit) -> list[Change]:
        if not edit.find:
            raise EditFailed("`find` is empty.")
        if edit.find == edit.replace:
            raise EditFailed(
                "`find` and `replace` are the same. `find` is the text the PDF has now; "
                "`replace` is the new text."
            )
        if "\n" in edit.replace or "\r" in edit.replace:
            raise EditFailed(
                "`replace` has a line break. Text is changed within its line; a new line, "
                "paragraph or clause can't be added to a PDF here."
            )
        spots, notes = self._locate(edit)
        # The last place first, so an earlier place on the same line keeps its position.
        spots.sort(key=lambda s: (s.page, s.line, s.start), reverse=True)
        changes = [self._replace(spot, edit.replace) for spot in spots]
        changes.reverse()
        if changes:
            changes[0].notes[:0] = notes
        return changes

    # -- locating

    def _pages_for(self, edit: Edit) -> list[int]:
        count = len(self._pdf)
        if edit.page is None:
            return list(range(count))
        if not 1 <= edit.page <= count:
            raise EditFailed(f"There is no page {edit.page}: the PDF has {count}.")
        return [edit.page - 1]

    def _exact(self, pages: list[int], find: str) -> list[_Spot]:
        return [
            _Spot(page, li, at, at + len(find), find)
            for page in pages
            for li, line in enumerate(self._lines(page))
            for at in _occurrences(line, find)
        ]

    def _locate(self, edit: Edit) -> tuple[list[_Spot], list[str]]:
        pages = self._pages_for(edit)
        spots = self._exact(pages, edit.find)
        if edit.all and spots:
            return spots, []
        if len(spots) == 1:
            return spots, []
        if len(spots) > 1:
            raise EditFailed(self._several(spots, edit.find))
        everywhere = list(range(len(self._pdf)))
        if edit.page is not None:
            elsewhere = self._exact(everywhere, edit.find)
            if len(elsewhere) == 1 or (edit.all and elsewhere):
                where = ", ".join(sorted({str(s.page + 1) for s in elsewhere}))
                return elsewhere, [f"found on page {where}, not page {edit.page}"]
            if elsewhere:
                raise EditFailed(self._several(elsewhere, edit.find))
        if not edit.all:
            close = self._close_match(pages, edit.find)
            if close is not None:
                # Not applied: the model's `replace` is written from its own slip ("місто
                # Львів" for "місті Київ"), and would put that slip into the document.
                raise EditFailed(
                    f"It isn't in the PDF exactly; page {close.page + 1} has “{close.found}”. "
                    "Copy `find` exactly from there, and write `replace` to match it."
                )
        raise EditFailed(self._not_found(pages, edit.find))

    def _several(self, spots: list[_Spot], find: str) -> str:
        places = "; ".join(
            f"page {s.page + 1}: {_quote(self._lines(s.page)[s.line])}" for s in spots[:5]
        )
        more = f" and {len(spots) - 5} more" if len(spots) > 5 else ""
        return (
            f"It is in {len(spots)} places ({places}{more}). Give a longer `find` that is in "
            "one place only, or set `all` to change every one."
        )

    def _close_match(self, pages: list[int], find: str) -> _Spot | None:
        """The one stretch of a line within a slip of `find` (a letter or two), if exactly one
        is that close."""
        best: list[tuple[float, _Spot]] = []
        size = len(find)
        matcher = difflib.SequenceMatcher(None, b=find)
        for page in pages[:NEAR_PAGES]:
            for li, line in enumerate(self._lines(page)):
                for width in range(max(1, size - 2), size + 3):
                    for at in range(0, len(line) - width + 1):
                        window = line[at : at + width]
                        matcher.set_seq1(window)
                        if matcher.real_quick_ratio() < CLOSE_ENOUGH:
                            continue
                        if matcher.quick_ratio() < CLOSE_ENOUGH:
                            continue
                        ratio = matcher.ratio()
                        if ratio >= CLOSE_ENOUGH:
                            best.append((ratio, _Spot(page, li, at, at + width, window)))
        if not best:
            return None
        top = max(ratio for ratio, _ in best)
        winners = [spot for ratio, spot in best if ratio == top]
        # Windows over one place overlap and start within a letter or two of each other;
        # anything further apart is a second place, and then it can't tell which was meant.
        first = winners[0]
        if any(
            (s.page, s.line) != (first.page, first.line) or abs(s.start - first.start) > 2
            for s in winners
        ):
            return None
        return first

    def _not_found(self, pages: list[int], find: str) -> str:
        texts = {page: self._lines(page) for page in pages[:NEAR_PAGES]}
        joined = {page: " ".join(lines) for page, lines in texts.items()}
        if any(find in " ".join(text.split()) for text in joined.values()):
            return (
                "It runs across two lines; text can only be changed within one line, so give "
                "one edit per line."
            )
        scans = [
            page + 1
            for page, lines in texts.items()
            if sum(len(line.strip()) for line in lines) < PDF_PAGE_MIN_CHARS
        ]
        # Only said when every page was looked at: past the first NEAR_PAGES there may be text.
        if len(scans) == len(texts) and len(pages) <= NEAR_PAGES:
            pages_said = "This page has" if len(pages) == 1 else "These pages have"
            return (
                f"{pages_said} no text layer (a scan?), so there is no text to change in place: "
                "only OCR reads it."
            )
        lines = [(page, line) for page, lines in texts.items() for line in lines if line.strip()]
        lines.sort(key=lambda item: _shared(item[1], find), reverse=True)
        nearest = "; ".join(f"page {page + 1}: {_quote(line)}" for page, line in lines[:3])
        among = (
            f" (of the first {NEAR_PAGES} pages; give `page` for others)"
            if (len(pages) > NEAR_PAGES)
            else ""
        )
        return (
            f"It isn't in the PDF. The nearest lines{among}: {nearest}. Copy `find` exactly "
            "from one."
        )

    # -- replacing

    def _replace(self, spot: _Spot, replacement: str) -> Change:
        page = self._page(spot.page)
        before = self._text(spot.page)
        line = before.lines[spot.line]
        if line.text[spot.start : spot.end] != spot.found:
            raise EditFailed("The line changed under an earlier edit; make one edit per place.")
        chars = line.chars
        touched = list(dict.fromkeys(c.obj for c in chars[spot.start : spot.end] if c.obj))
        if not touched:
            raise EditFailed("That is only spacing; there is no drawn text to change.")
        start = min(spot.start, min(i for i, c in enumerate(chars) if c.obj == touched[0]))
        end = max(spot.end, max(i for i, c in enumerate(chars) if c.obj == touched[-1]) + 1)
        self._check_drawn_together(before, line, touched, start, end)

        first = before.handles[touched[0]]
        style = _Style.of(first)
        if style.rotated:
            raise EditFailed("That text is rotated or slanted; only level text can be changed.")
        new_text = line.text[start : spot.start] + replacement + line.text[spot.end : end]
        # The text page merges a space at an object's edge with its neighbour's; the objects
        # themselves still have it, and the new one keeps it so the text reads as before.
        own = self._own_text(page, first), self._own_text(page, before.handles[touched[-1]])
        lead, trail = own[0][: len(own[0]) - len(own[0].lstrip())], own[1][len(own[1].rstrip()) :]
        if lead and not new_text[:1].isspace():
            new_text = lead + new_text
        if trail and not new_text[-1:].isspace():
            new_text += trail
        # Widths are measured on the letters' ink, spaces left out: a space's box is
        # sometimes its width and sometimes nothing.
        inked = [c for c in chars[start:end] if c.obj and not c.text.isspace()]
        old_right = max(c.box[1] for c in inked)
        old_left = min(c.box[0] for c in inked)
        at = _index_of(page, first)

        degraded: list[str] = []
        notes: list[str] = []
        fonts = {_font_name(pdfium_c.FPDFTextObj_GetFont(before.handles[o])) for o in touched}
        if len(fonts) > 1:
            degraded.append(
                f"it was drawn in {len(fonts)} fonts ({', '.join(sorted(fonts))}); "
                f"all of it is now in {style.font_name}"
            )

        new = None
        font_used = font_index.postscript_name(style.font_name)
        if new_text.strip():
            new, font_used, note = self._draw(page, style, new_text, at)
            if note:
                notes.append(note)
        for obj in touched:
            self._remove(page, before.handles[obj])
        self._changed(spot.page)

        after = self._text(spot.page)
        new_right = old_left
        if new is not None:
            boxes = [
                c.box[1]
                for ln in after.lines
                for c in ln.chars
                if c.obj == _addr(new) and not c.text.isspace()
            ]
            new_right = max(boxes, default=old_left)
        delta = new_right - old_right
        degraded.extend(self._shift_rest(before, line, end, old_right, delta, style.em))
        self._changed(spot.page)
        pdfium_c.FPDFPage_GenerateContent(page.raw)
        return Change(
            page=spot.page + 1,
            before=line.text,
            after=line.text[: spot.start] + replacement + line.text[spot.end :],
            font=font_used,
            notes=notes,
            degraded=degraded,
        )

    def _check_drawn_together(
        self, text: _PageText, line: _Line, touched: list[int], start: int, end: int
    ) -> None:
        inside = Counter(c.obj for c in line.chars[start:end] if c.obj)
        if set(inside) - set(touched):
            raise EditFailed(
                "The letters there are drawn in overlapping pieces; it can't be changed in place."
            )
        for obj in touched:
            if text.chars_of(obj) != inside[obj]:
                raise EditFailed(
                    "That text is drawn together with text on another line, so it can't be "
                    "changed on its own."
                )

    def _draw(
        self, page: pdfium.PdfPage, style: _Style, text: str, at: int
    ) -> tuple[object, str, str]:
        """A new text object for `text` in the original's style, at index `at` among the
        page's objects (where the original was, so the text page reads it in its place). Its
        own font when that has every letter; else the installed font of the same name."""
        ps_name = font_index.postscript_name(style.font_name)
        obj = self._new_object(page, style, style.font, text, at)
        missing = self._missing(page, obj, text)
        if not missing:
            return obj, ps_name, ""
        self._remove(page, obj)
        letters = ", ".join(f"“{ch}”" for ch in missing)
        installed = self._fonts.find(ps_name)
        if installed is None:
            raise EditFailed(
                f"The PDF's font {ps_name} has no {letters}, and {ps_name} isn't installed on "
                "this computer, so the new text can't be drawn in it."
            )
        try:
            self._loaded += 1
            tag = font_index.subset_tag(ps_name, text, self._loaded)
            data = font_index.subset_for(installed, ps_name, text, tag)
        except font_index.NotEmbeddable as exc:
            raise EditFailed(
                f"The PDF's font {ps_name} has no {letters}, and the installed {ps_name} can't "
                f"be used: {exc}."
            ) from None
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        self._keep.append(buffer)
        font = pdfium_c.FPDFText_LoadFont(
            self._pdf.raw, buffer, len(data), pdfium_c.FPDF_FONT_TRUETYPE, 1
        )
        if not font:
            raise EditFailed(f"The installed {ps_name} couldn't be loaded.")
        try:
            obj = self._new_object(page, style, font, text, at)
        finally:
            pdfium_c.FPDFFont_Close(font)
        still = self._missing(page, obj, text)
        if still:
            self._remove(page, obj)
            raise EditFailed(f"Even the installed {ps_name} has no {', '.join(still)}.")
        return obj, ps_name, f"{letters} drawn with the installed {ps_name}"

    def _new_object(
        self, page: pdfium.PdfPage, style: _Style, font: object, text: str, at: int
    ) -> object:
        obj = pdfium_c.FPDFPageObj_CreateTextObj(self._pdf.raw, font, style.size)
        encoded = (text + "\0").encode("utf-16-le")
        buffer = ctypes.create_string_buffer(encoded, len(encoded))
        self._keep.append(buffer)
        pdfium_c.FPDFText_SetText(obj, ctypes.cast(buffer, pdfium_c.FPDF_WIDESTRING))
        pdfium_c.FPDFTextObj_SetTextRenderMode(obj, style.render_mode)
        pdfium_c.FPDFPageObj_SetFillColor(obj, *style.fill)
        pdfium_c.FPDFPageObj_SetStrokeColor(obj, *style.stroke)
        pdfium_c.FPDFPageObj_SetMatrix(obj, ctypes.byref(style.matrix))
        pdfium_c.FPDFPage_InsertObjectAtIndex(page.raw, obj, at)
        return obj

    def _own_text(self, page: pdfium.PdfPage, obj: object) -> str:
        textpage = page.get_textpage()
        try:
            return _object_text(obj, textpage)
        finally:
            textpage.close()

    def _missing(self, page: pdfium.PdfPage, obj: object, text: str) -> list[str]:
        """The letters of `text` the object can't draw. PDFium leaves out a character its
        font can't encode, so it is missing when the text is read back; and a subset font
        may encode a letter it has no width for (Quartz's do), drawn on top of the next."""
        textpage = page.get_textpage()
        try:
            got = _object_text(obj, textpage)
            flat = _zero_width(textpage, _addr(obj))
        finally:
            textpage.close()
        want = Counter(ch for ch in text if not ch.isspace())
        have = Counter(ch for ch in got if not ch.isspace())
        lost = {ch for ch in text if (want - have)[ch] > 0} | flat
        return [ch for ch in dict.fromkeys(text) if ch in lost]

    def _remove(self, page: pdfium.PdfPage, obj: object) -> None:
        pdfium_c.FPDFPage_RemoveObject(page.raw, obj)
        pdfium_c.FPDFPageObj_Destroy(obj)

    def _shift_rest(
        self, text: _PageText, line: _Line, end: int, old_right: float, delta: float, em: float
    ) -> list[str]:
        """Move the text after the edit by `delta`, up to a column-wide gap; what it runs
        into, if anything."""
        reach = old_right
        barrier: float | None = None
        followers: list[int] = []
        rest = [c for c in line.chars[end:] if c.obj]
        for obj in dict.fromkeys(c.obj for c in rest):
            own = [c for c in rest if c.obj == obj]
            left = min(c.box[0] for c in own)
            if left - reach > COLUMN_GAP_EMS * em or text.chars_of(obj) != len(own):
                barrier = left
                break
            followers.append(obj)
            reach = max(reach, max(c.box[1] for c in own))
        for obj in followers:
            pdfium_c.FPDFPageObj_Transform(text.handles[obj], 1, 0, 0, 1, delta, 0)
        if delta <= 0:
            return []
        limit = barrier - 0.25 * em if barrier is not None else text.right
        over = reach + delta - limit
        if over <= MARGIN_SLACK_PT:
            return []
        into = "into the text beside it" if barrier is not None else "past the right margin"
        return [f"the line now runs {over:.0f} pt {into}"]


# --- An object's look ------------------------------------------------------------------------


@dataclass
class _Style:
    font: object
    font_name: str
    size: float
    matrix: pdfium_c.FS_MATRIX
    fill: tuple[int, int, int, int]
    stroke: tuple[int, int, int, int]
    render_mode: int

    @property
    def rotated(self) -> bool:
        return abs(self.matrix.b) > 1e-6 or abs(self.matrix.c) > 1e-6

    @property
    def em(self) -> float:
        return self.size * abs(self.matrix.d or 1.0)

    @classmethod
    def of(cls, obj: object) -> _Style:
        font = pdfium_c.FPDFTextObj_GetFont(obj)
        size = ctypes.c_float()
        pdfium_c.FPDFTextObj_GetFontSize(obj, ctypes.byref(size))
        matrix = pdfium_c.FS_MATRIX()
        pdfium_c.FPDFPageObj_GetMatrix(obj, ctypes.byref(matrix))
        return cls(
            font=font,
            font_name=_font_name(font),
            size=size.value,
            matrix=matrix,
            fill=_colour(pdfium_c.FPDFPageObj_GetFillColor, obj),
            stroke=_colour(pdfium_c.FPDFPageObj_GetStrokeColor, obj),
            render_mode=pdfium_c.FPDFTextObj_GetTextRenderMode(obj),
        )


def _colour(getter: Callable[..., int], obj: object) -> tuple[int, int, int, int]:
    red, green, blue, alpha = (ctypes.c_uint() for _ in range(4))
    if not getter(obj, *(ctypes.byref(p) for p in (red, green, blue, alpha))):
        return (0, 0, 0, 255)
    return (red.value, green.value, blue.value, alpha.value)


def _font_name(font: object) -> str:
    size = pdfium_c.FPDFFont_GetBaseFontName(font, None, 0)
    buffer = ctypes.create_string_buffer(max(size, 1))
    pdfium_c.FPDFFont_GetBaseFontName(font, buffer, size)
    return buffer.value.decode("utf-8", errors="replace") or "an unnamed font"


def _index_of(page: pdfium.PdfPage, obj: object) -> int:
    """Where an object is in the page's drawing order."""
    target = _addr(obj)
    for index in range(pdfium_c.FPDFPage_CountObjects(page.raw)):
        if _addr(pdfium_c.FPDFPage_GetObject(page.raw, index)) == target:
            return index
    return pdfium_c.FPDFPage_CountObjects(page.raw)


def _zero_width(textpage: pdfium.PdfTextPage, obj: int) -> set[str]:
    """The letters of one text object that take no room: the next letter starts where it
    does. A glyph with no width in the font is drawn under its neighbour."""
    raw = textpage.raw
    x, y = ctypes.c_double(), ctypes.c_double()
    placed: list[tuple[str, float]] = []
    for i in range(pdfium_c.FPDFText_CountChars(raw)):
        if pdfium_c.FPDFText_IsGenerated(raw, i) == 1:
            continue
        if _addr(pdfium_c.FPDFText_GetTextObject(raw, i)) != obj:
            continue
        pdfium_c.FPDFText_GetCharOrigin(raw, i, ctypes.byref(x), ctypes.byref(y))
        placed.append((chr(pdfium_c.FPDFText_GetUnicode(raw, i)), x.value))
    return {
        ch
        for (ch, at), (_, after) in zip(placed, placed[1:], strict=False)
        if not ch.isspace() and after - at < ZERO_WIDTH_PT
    }


def _object_text(obj: object, textpage: pdfium.PdfTextPage) -> str:
    size = pdfium_c.FPDFTextObj_GetText(obj, textpage.raw, None, 0)
    if size <= 2:
        return ""
    buffer = ctypes.create_string_buffer(size)
    pdfium_c.FPDFTextObj_GetText(
        obj, textpage.raw, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ushort)), size
    )
    return buffer.raw[: size - 2].decode("utf-16-le", errors="replace")
