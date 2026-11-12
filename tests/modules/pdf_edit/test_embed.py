"""Completing the fonts PDFium writes into a filled form: a Cyrillic value is drawn in a font
PDFium names but doesn't embed, and embed_fonts adds an installed font's file to it."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont
from pypdf import PdfReader

from lectern.modules.pdf_edit.embed import STANDARD_14, embed_fonts
from lectern.modules.pdf_edit.forms import fill_form

from .conftest import form_pdf

NAME = "Євген Матасар, вул. Ґонти 5"


@pytest.fixture
def form(tmp_path: Path) -> Path:
    return form_pdf(tmp_path / "form.pdf")


def _appearance_fonts(data: bytes) -> list[dict]:
    """Each font the filled widgets' appearances use, as a plain dict."""
    found = {}
    for page in PdfReader(io.BytesIO(data)).pages:
        for annot in page.get("/Annots", []):
            normal = annot.get_object().get("/AP", {}).get("/N")
            if normal is None:
                continue
            normal = normal.get_object()
            streams = [normal] if hasattr(normal, "get_data") else list(normal.values())
            for stream in streams:
                fonts = stream.get_object().get("/Resources", {}).get("/Font", {})
                for ref in fonts.values():
                    found[ref.idnum] = ref.get_object()
    return list(found.values())


def _embedded(font: dict) -> bytes | None:
    descriptor = font.get("/FontDescriptor")
    file = descriptor.get_object().get("/FontFile2") if descriptor else None
    return file.get_object().get_data() if file else None


def _pdfium_only(form: Path, value: str) -> bytes:
    """The form as PDFium alone fills it."""
    return fill_form(form, {"name": value}).data


def test_a_font_pdfium_didnt_embed_gets_the_installed_fonts_file(form, with_fonts) -> None:  # noqa: ANN001
    data = _pdfium_only(form, NAME)
    named = [f for f in _appearance_fonts(data) if _embedded(f) is None and
             f["/BaseFont"][1:] not in STANDARD_14]  # fmt: skip
    assert named  # PDFium wrote one it didn't embed
    pdfium_name = named[0]["/BaseFont"][1:]

    done = embed_fonts(data, NAME, with_fonts, aliases={pdfium_name: "TestSans"}, fallbacks=())

    assert done.missing == []
    fonts = [f for f in _appearance_fonts(done.data) if f["/BaseFont"][1:] not in STANDARD_14]
    assert fonts and all(_embedded(f) for f in fonts)
    for font in fonts:
        assert font["/BaseFont"].endswith("+TestSans")
        face = TTFont(io.BytesIO(_embedded(font)))
        drawn = {ch for ch in NAME if ord(ch) > 0x7F}
        assert drawn <= {chr(u) for u in face.getBestCmap()}  # the value's letters are in it


def test_pdfiums_bytes_stay_as_they_were(form, with_fonts) -> None:  # noqa: ANN001
    data = _pdfium_only(form, NAME)

    done = embed_fonts(data, NAME, with_fonts, aliases={}, fallbacks=("TestSans",))

    assert done.data[: len(data)] == data and len(done.data) > len(data)


def test_a_latin_fill_is_left_as_it_is(form, with_fonts) -> None:  # noqa: ANN001
    data = _pdfium_only(form, "Jane Doe")

    done = embed_fonts(data, "Jane Doe", with_fonts, aliases={}, fallbacks=("TestSans",))

    assert done.data == data and done.missing == []
    assert all(f["/BaseFont"][1:] in STANDARD_14 for f in _appearance_fonts(data))


def test_each_embedded_font_has_its_own_subset_tag(tmp_path: Path, with_fonts) -> None:  # noqa: ANN001
    form = form_pdf(tmp_path / "form.pdf")
    data = fill_form(form, {"name": NAME, "date": "12 листопада"}).data

    done = embed_fonts(data, NAME + "12 листопада", with_fonts, aliases={}, fallbacks=("TestSans",))

    names = [str(f["/BaseFont"]) for f in _appearance_fonts(done.data) if _embedded(f)]
    assert names and len(names) == len(set(names))
