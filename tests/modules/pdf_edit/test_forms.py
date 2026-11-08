"""Filling a PDF form: the fields listed, every kind filled, its appearance redrawn, and
the values that can't be filled refused together."""

from __future__ import annotations

import ctypes
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest

from lectern.modules.pdf_edit.engine import EditFailed
from lectern.modules.pdf_edit.forms import fill_form, form_fields

from .conftest import Run, form_pdf, make_pdf


@pytest.fixture
def form(tmp_path: Path) -> Path:
    return form_pdf(tmp_path / "form.pdf")


def _fields(data_or_path: bytes | Path, tmp_path: Path) -> dict[str, tuple[str, str]]:
    if isinstance(data_or_path, bytes):
        path = tmp_path / "filled.pdf"
        path.write_bytes(data_or_path)
    else:
        path = data_or_path
    return {f.name: (f.kind, f.value) for f in form_fields(path)}


def test_the_fields_are_listed_with_kind_value_and_options(form: Path) -> None:
    fields = {f.name: f for f in form_fields(form)}

    assert list(fields) == ["name", "date", "agree", "size", "city", "id", "print"]
    assert (fields["name"].kind, fields["name"].value, fields["name"].page) == ("text", "", 1)
    assert (fields["agree"].kind, fields["agree"].value) == ("checkbox", "no")
    assert (fields["size"].kind, fields["size"].options) == ("radio", ["S", "M"])
    assert (fields["city"].kind, fields["city"].options) == ("choice", ["Kyiv", "Lviv", "Odesa"])
    assert (fields["id"].value, fields["id"].read_only) == ("A-17", True)
    assert fields["print"].kind == "button"


def test_every_kind_of_field_is_filled(form: Path, tmp_path: Path) -> None:
    before = form.read_bytes()

    filled = fill_form(
        form,
        {"name": "Олена Шевченко", "date": "08.11.2026", "agree": "yes", "size": "M",
         "city": "Lviv"},
    )  # fmt: skip

    now = _fields(filled.data, tmp_path)
    assert now["name"] == ("text", "Олена Шевченко")
    assert now["date"] == ("text", "08.11.2026")
    assert now["agree"] == ("checkbox", "yes")
    assert now["size"] == ("radio", "M")
    assert now["city"] == ("choice", "Lviv")
    assert form.read_bytes() == before


def test_a_filled_text_field_has_its_appearance_redrawn(form: Path) -> None:
    """The value is drawn in the field's appearance stream, so a viewer that doesn't redraw
    fields shows it too."""
    filled = fill_form(form, {"date": "08.11.2026"})

    pdf = pdfium.PdfDocument(filled.data)
    try:
        page = pdf[0]
        annot = pdfium_c.FPDFPage_GetAnnot(page.raw, 1)  # "date"
        size = pdfium_c.FPDFAnnot_GetAP(annot, pdfium_c.FPDF_ANNOT_APPEARANCEMODE_NORMAL, None, 0)
        buffer = ctypes.create_string_buffer(size)
        pdfium_c.FPDFAnnot_GetAP(
            annot,
            pdfium_c.FPDF_ANNOT_APPEARANCEMODE_NORMAL,
            ctypes.cast(buffer, ctypes.POINTER(pdfium_c.FPDF_WCHAR)),
            size,
        )
        appearance = buffer.raw[: size - 2].decode("utf-16-le")
        pdfium_c.FPDFPage_CloseAnnot(annot)
        page.close()
    finally:
        pdf.close()
    assert "(08.11.2026) Tj" in appearance
    assert b"/NeedAppearances true" not in filled.data


def test_a_checkbox_can_be_unticked(form: Path, tmp_path: Path) -> None:
    ticked = tmp_path / "ticked.pdf"
    ticked.write_bytes(fill_form(form, {"agree": "так"}).data)

    unticked = fill_form(ticked, {"agree": "no"})

    assert _fields(unticked.data, tmp_path)["agree"] == ("checkbox", "no")


def test_values_that_cant_be_filled_are_all_named_and_nothing_is_filled(form: Path) -> None:
    with pytest.raises(EditFailed) as failed:
        fill_form(
            form,
            {"name": "Ok", "agree": "maybe", "size": "XL", "city": "Paris", "id": "B-1",
             "print": "x", "phone": "123"},
        )  # fmt: skip

    message = str(failed.value)
    assert message.startswith("Nothing was filled.")
    assert "“agree”: a checkbox takes yes or no, not “maybe”." in message
    assert "“size”: its options are “S”, “M”." in message
    assert "“city”: its options are “Kyiv”, “Lviv”, “Odesa”." in message
    assert "“id”: it is read-only." in message
    assert "“print”: it is a button field, which can't be filled in." in message
    assert "“phone”: there is no such field." in message
    assert "The form's fields: “name”, “date”" in message
    assert "“name”:" not in message


def test_a_pdf_without_a_form_has_no_fields(tmp_path: Path, font_files: Path) -> None:
    pdf = make_pdf(tmp_path / "plain.pdf", font_files, [[[Run("No form here")]]])

    assert form_fields(pdf) == []
    with pytest.raises(EditFailed, match="there is no such field"):
        fill_form(pdf, {"name": "x"})


def test_no_values_is_an_error(form: Path) -> None:
    with pytest.raises(EditFailed, match="No field values were given"):
        fill_form(form, {})


def test_something_that_isnt_a_pdf_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.pdf"
    path.write_bytes(b"nope")

    with pytest.raises(EditFailed, match="couldn't be opened as a PDF"):
        form_fields(path)
