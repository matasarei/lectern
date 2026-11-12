"""Filling a PDF form (AcroForm): its fields listed, and values put in through PDFium's form
environment, the way a person typing in a viewer would. That writes each field's value and
redraws its appearance, so the filled form shows in every viewer, not only in those that redraw
fields themselves.

A text field takes the text; a checkbox yes or no; a radio group or a list one of its
options. Buttons, signatures and read-only fields can't be filled. Like the text engine, every
value is put in or none is, and the file at `path` is never touched.

PDFium draws letters the field's own font can't (Cyrillic in a Helvetica field) in a system
font it doesn't embed; embed.embed_fonts then gives that font an installed font's file.

Every function here holds projects.text.PDFIUM_LOCK while it uses PDFium.
"""

from __future__ import annotations

import ctypes
import io
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from lectern.modules.pdf_edit import embed
from lectern.modules.pdf_edit import fonts as font_index
from lectern.modules.pdf_edit.engine import EditFailed
from lectern.projects.text import PDFIUM_LOCK

KINDS = {
    pdfium_c.FPDF_FORMFIELD_TEXTFIELD: "text",
    pdfium_c.FPDF_FORMFIELD_CHECKBOX: "checkbox",
    pdfium_c.FPDF_FORMFIELD_RADIOBUTTON: "radio",
    pdfium_c.FPDF_FORMFIELD_COMBOBOX: "choice",
    pdfium_c.FPDF_FORMFIELD_LISTBOX: "choice",
    pdfium_c.FPDF_FORMFIELD_PUSHBUTTON: "button",
    pdfium_c.FPDF_FORMFIELD_SIGNATURE: "signature",
}
FILLABLE = {"text", "checkbox", "radio", "choice"}
YES = {"yes", "y", "true", "on", "1", "x", "✓", "✔", "checked", "так", "+"}
NO = {"no", "n", "false", "off", "0", "", "unchecked", "ні", "-"}
SPACE = 0x20


@dataclass
class Field:
    name: str
    kind: str  # text | checkbox | radio | choice | button | signature | other
    value: str
    page: int  # 1-based, where its first widget is
    options: list[str] = field(default_factory=list)  # a radio group's or a list's
    read_only: bool = False


@dataclass
class Filled:
    data: bytes  # the filled PDF
    fields: list[Field]  # the fields as they are now, the filled ones included
    unembedded: list[str] = field(default_factory=list)  # fonts a viewer must have to show it


def form_fields(path: Path) -> list[Field]:
    """The form's fields in page order, one entry per field (a field may have a widget on
    several pages). Empty when the PDF has no form."""
    with PDFIUM_LOCK, _form(path) as form:
        return [f for f, _ in form.fields()]


def fill_form(
    path: Path, values: dict[str, str], fonts: font_index.InstalledFonts | None = None
) -> Filled:
    """The PDF with `values` (field name → value) filled in, as new bytes, the fonts its
    values are drawn in embedded from `fonts` (this computer's by default) where they can be.
    Raises EditFailed naming every value that can't be filled, and then fills none."""
    if not values:
        raise EditFailed("No field values were given.")
    with PDFIUM_LOCK, _form(path) as form:
        entries = form.fields()
        by_name = {f.name: (f, widgets) for f, widgets in entries}
        problems = []
        for name, value in values.items():
            found = by_name.get(name)
            if found is None:
                problems.append(f"“{name}”: there is no such field.")
                continue
            try:
                form.set(*found, value)
            except EditFailed as exc:
                problems.append(f"“{name}”: {exc}")
        if problems:
            names = ", ".join(f"“{f.name}”" for f, _ in entries)
            raise EditFailed(
                "Nothing was filled. " + " ".join(problems) + f" The form's fields: {names}."
            )
        data, fields = form.save(), [f for f, _ in form.fields()]
    # After the lock: the font index may be built here, and that must never block PDFium.
    done = embed.embed_fonts(data, "".join(values.values()), fonts or font_index.installed())
    return Filled(done.data, fields, done.missing)


@dataclass(frozen=True)
class _Widget:
    page: int  # 0-based
    index: int  # among the page's annotations


class _Form:
    def __init__(self, pdf: pdfium.PdfDocument) -> None:
        self._pdf = pdf
        self._env = pdf.init_forms()
        self._pages: dict[int, pdfium.PdfPage] = {}
        self._keep: list[object] = []

    @property
    def _handle(self) -> object:
        return self._pdf.formenv.raw

    def _page(self, index: int) -> pdfium.PdfPage:
        if index not in self._pages:
            self._pages[index] = self._pdf[index]
        return self._pages[index]

    @contextmanager
    def _annot(self, widget: _Widget) -> Iterator[object]:
        annot = pdfium_c.FPDFPage_GetAnnot(self._page(widget.page).raw, widget.index)
        try:
            yield annot
        finally:
            pdfium_c.FPDFPage_CloseAnnot(annot)

    def fields(self) -> list[tuple[Field, list[_Widget]]]:
        found: dict[str, tuple[Field, list[_Widget]]] = {}
        for page_index in range(len(self._pdf)):
            page = self._page(page_index)
            for index in range(pdfium_c.FPDFPage_GetAnnotCount(page.raw)):
                widget = _Widget(page_index, index)
                with self._annot(widget) as annot:
                    if pdfium_c.FPDFAnnot_GetSubtype(annot) != pdfium_c.FPDF_ANNOT_WIDGET:
                        continue
                    name = _wide(pdfium_c.FPDFAnnot_GetFormFieldName, self._handle, annot)
                    if not name:
                        continue
                    if name in found:
                        found[name][1].append(widget)
                        self._add_option(found[name][0], annot)
                        continue
                    kind = KINDS.get(pdfium_c.FPDFAnnot_GetFormFieldType(self._handle, annot))
                    flags = pdfium_c.FPDFAnnot_GetFormFieldFlags(self._handle, annot)
                    entry = Field(
                        name=name,
                        kind=kind or "other",
                        value=self._value(kind, annot),
                        page=page_index + 1,
                        read_only=bool(flags & pdfium_c.FPDF_FORMFLAG_READONLY),
                    )
                    self._add_option(entry, annot)
                    found[name] = (entry, [widget])
        return list(found.values())

    def _add_option(self, entry: Field, annot: object) -> None:
        if entry.kind == "radio":
            export = _wide(pdfium_c.FPDFAnnot_GetFormFieldExportValue, self._handle, annot)
            if export and export not in entry.options:
                entry.options.append(export)
            if pdfium_c.FPDFAnnot_IsChecked(self._handle, annot):
                entry.value = export
        elif entry.kind == "choice" and not entry.options:
            count = pdfium_c.FPDFAnnot_GetOptionCount(self._handle, annot)
            entry.options = [
                _wide(pdfium_c.FPDFAnnot_GetOptionLabel, self._handle, annot, i)
                for i in range(max(count, 0))
            ]

    def _value(self, kind: str | None, annot: object) -> str:
        if kind == "checkbox":
            return "yes" if pdfium_c.FPDFAnnot_IsChecked(self._handle, annot) else "no"
        if kind == "radio":
            return ""  # set from the checked widget in _add_option
        return _wide(pdfium_c.FPDFAnnot_GetFormFieldValue, self._handle, annot)

    def set(self, entry: Field, widgets: list[_Widget], value: str) -> None:
        if entry.read_only:
            raise EditFailed("it is read-only.")
        if entry.kind not in FILLABLE:
            raise EditFailed(f"it is a {entry.kind} field, which can't be filled in.")
        if entry.kind == "text":
            self._type(widgets[0], value)
        elif entry.kind == "checkbox":
            self._check(widgets[0], _yes_or_no(value))
        elif entry.kind == "radio":
            self._choose_radio(entry, widgets, value)
        else:
            self._choose(entry, widgets[0], value)

    def _type(self, widget: _Widget, value: str) -> None:
        page = self._page(widget.page)
        encoded = (value + "\0").encode("utf-16-le")
        buffer = ctypes.create_string_buffer(encoded, len(encoded))
        self._keep.append(buffer)
        with self._annot(widget) as annot:
            pdfium_c.FORM_SetFocusedAnnot(self._handle, annot)
            pdfium_c.FORM_SelectAllText(self._handle, page.raw)
            pdfium_c.FORM_ReplaceSelection(
                self._handle, page.raw, ctypes.cast(buffer, pdfium_c.FPDF_WIDESTRING)
            )
            pdfium_c.FORM_ForceToKillFocus(self._handle)
            got = _wide(pdfium_c.FPDFAnnot_GetFormFieldValue, self._handle, annot)
        if got != value:
            raise EditFailed(f"it took only “{got}” (it may limit its length or format).")

    def _check(self, widget: _Widget, checked: bool) -> None:
        with self._annot(widget) as annot:
            if bool(pdfium_c.FPDFAnnot_IsChecked(self._handle, annot)) != checked:
                self._press_space(widget, annot)
            if bool(pdfium_c.FPDFAnnot_IsChecked(self._handle, annot)) != checked:
                raise EditFailed("it didn't change.")

    def _choose_radio(self, entry: Field, widgets: list[_Widget], value: str) -> None:
        for widget in widgets:
            with self._annot(widget) as annot:
                export = _wide(pdfium_c.FPDFAnnot_GetFormFieldExportValue, self._handle, annot)
                if export.casefold() != value.strip().casefold():
                    continue
                if not pdfium_c.FPDFAnnot_IsChecked(self._handle, annot):
                    self._press_space(widget, annot)
                if not pdfium_c.FPDFAnnot_IsChecked(self._handle, annot):
                    raise EditFailed("it didn't change.")
                return
        raise EditFailed(f"its options are {_listed(entry.options)}.")

    def _choose(self, entry: Field, widget: _Widget, value: str) -> None:
        wanted = value.strip().casefold()
        matches = [i for i, label in enumerate(entry.options) if label.casefold() == wanted]
        if not matches:
            raise EditFailed(f"its options are {_listed(entry.options)}.")
        page = self._page(widget.page)
        with self._annot(widget) as annot:
            pdfium_c.FORM_SetFocusedAnnot(self._handle, annot)
            pdfium_c.FORM_SetIndexSelected(self._handle, page.raw, matches[0], True)
            pdfium_c.FORM_ForceToKillFocus(self._handle)
            if not pdfium_c.FPDFAnnot_IsOptionSelected(self._handle, annot, matches[0]):
                raise EditFailed("it didn't change.")

    def _press_space(self, widget: _Widget, annot: object) -> None:
        """A checkbox or radio button toggles on the space key, as in a viewer."""
        page = self._page(widget.page)
        pdfium_c.FORM_SetFocusedAnnot(self._handle, annot)
        pdfium_c.FORM_OnChar(self._handle, page.raw, SPACE, 0)
        pdfium_c.FORM_ForceToKillFocus(self._handle)

    def save(self) -> bytes:
        out = io.BytesIO()
        self._pdf.save(out)
        return out.getvalue()

    def close(self) -> None:
        for page in self._pages.values():
            page.close()
        self._pdf.close()


@contextmanager
def _form(path: Path) -> Iterator[_Form]:
    try:
        pdf = pdfium.PdfDocument(path)
    except pdfium.PdfiumError:
        raise EditFailed(
            f"{path.name} couldn't be opened as a PDF (it may be password-protected or damaged)."
        ) from None
    form = _Form(pdf)
    try:
        yield form
    finally:
        form.close()


def _yes_or_no(value: str) -> bool:
    said = value.strip().casefold()
    if said in YES:
        return True
    if said in NO:
        return False
    raise EditFailed(f"a checkbox takes yes or no, not “{value}”.")


def _listed(options: list[str]) -> str:
    return ", ".join(f"“{o}”" for o in options) or "none"


def _wide(getter: Callable[..., int], *args: object) -> str:
    """A PDFium string getter's answer: asked once for the size, then for the text."""
    size = getter(*args, None, 0)
    if size <= 2:
        return ""
    buffer = ctypes.create_string_buffer(size)
    getter(*args, ctypes.cast(buffer, ctypes.POINTER(pdfium_c.FPDF_WCHAR)), size)
    return buffer.raw[: size - 2].decode("utf-16-le", errors="replace")
