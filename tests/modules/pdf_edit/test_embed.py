"""Completing the fonts PDFium writes into a filled form: a Cyrillic value is drawn in a font
PDFium names but doesn't embed, and embed_fonts adds an installed font's file to it."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fontTools.agl import toUnicode
from fontTools.ttLib import TTFont
from pypdf import PdfReader, PdfWriter

from lectern.modules.pdf_edit.embed import ENCRYPTED, STANDARD_14, embed_fonts
from lectern.modules.pdf_edit.fonts import InstalledFonts
from lectern.modules.pdf_edit.forms import fill_form

from .conftest import build_font, form_pdf

NAME = "Євген Матасар, вул. Ґонти 5"
# fill_form embeds from the computer's fonts; these tests start from what PDFium alone wrote.
PDFIUM_ONLY = InstalledFonts([Path("/nothing-installed")])


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
    return fill_form(form, {"name": value}, fonts=PDFIUM_ONLY).data


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
    data = fill_form(form, {"name": NAME, "date": "12 листопада"}, fonts=PDFIUM_ONLY).data

    done = embed_fonts(data, NAME + "12 листопада", with_fonts, aliases={}, fallbacks=("TestSans",))

    names = [str(f["/BaseFont"]) for f in _appearance_fonts(done.data) if _embedded(f)]
    assert names and len(names) == len(set(names))


def test_a_fallback_stands_in_with_its_own_widths(form, with_fonts, font_files) -> None:  # noqa: ANN001
    done = embed_fonts(_pdfium_only(form, NAME), NAME, with_fonts, aliases={},
                       fallbacks=("NotInstalled", "TestSans"))  # fmt: skip

    face = TTFont(font_files / "TestSans.ttf")
    glyphs, per_em = face.getBestCmap(), face["head"].unitsPerEm
    embedded = [f for f in _appearance_fonts(done.data) if _embedded(f)]
    assert embedded and done.missing == []
    for font in embedded:
        assert font["/BaseFont"].endswith("+TestSans")
        first, widths = int(font["/FirstChar"]), font["/Widths"]
        codes = _codes(font)
        for letter in {ch for ch in NAME if ord(ch) > 0x7F}:
            own = round(face["hmtx"][glyphs[ord(letter)]][0] * 1000 / per_em)
            assert widths[codes[letter] - first] == own


def test_with_no_font_to_embed_the_font_is_named_and_the_pdf_left_as_it_is(form, no_fonts) -> None:  # noqa: ANN001
    data = _pdfium_only(form, NAME)

    done = embed_fonts(data, NAME, no_fonts, fallbacks=("TestSans",))

    assert done.data == data
    assert done.missing and all(name for name in done.missing)


def test_a_font_whose_licence_forbids_embedding_is_named_not_embedded(form, tmp_path) -> None:  # noqa: ANN001
    folder = tmp_path / "locked"
    folder.mkdir()
    build_font(folder / "Locked.ttf", "Locked", fs_type=0x0002)
    data = _pdfium_only(form, NAME)

    done = embed_fonts(data, NAME, InstalledFonts([folder]), aliases={}, fallbacks=("Locked",))

    assert done.data == data and done.missing


def test_an_encrypted_form_is_left_as_it_is_and_says_so(form, with_fonts, tmp_path) -> None:  # noqa: ANN001
    locked = PdfWriter(clone_from=str(form))
    locked.encrypt(user_password="", owner_password="owner", algorithm="RC4-128")  # noqa: S106 (a test form)
    locked.write(str(tmp_path / "locked.pdf"))
    data = fill_form(tmp_path / "locked.pdf", {"name": NAME}, fonts=PDFIUM_ONLY).data

    done = embed_fonts(data, NAME, with_fonts, aliases={}, fallbacks=("TestSans",))

    assert done.data == data and done.missing == [ENCRYPTED]


def _codes(font: dict) -> dict[str, int]:
    """Letter → character code, from the font's /Differences."""
    codes, code = {}, 0
    for item in font["/Encoding"].get_object().get("/Differences", []):
        if isinstance(item, int):
            code = item
        else:
            codes[toUnicode(item[1:])] = code
            code += 1
    return codes


def _field_fonts(data: bytes, field: str) -> list[dict]:
    """The fonts the named field's normal appearance uses."""
    found = []
    for page in PdfReader(io.BytesIO(data)).pages:
        for annot in page.get("/Annots", []):
            annot = annot.get_object()
            if annot.get("/T") != field:
                continue
            resources = annot["/AP"]["/N"].get_object().get("/Resources", {})
            found += [ref.get_object() for ref in resources.get("/Font", {}).values()]
    return found


def test_a_font_another_field_is_drawn_in_keeps_that_fields_letters(
    form, with_fonts, tmp_path
) -> None:  # noqa: ANN001
    earlier = tmp_path / "earlier.pdf"  # a form whose "date" was filled before, font unembedded
    earlier.write_bytes(fill_form(form, {"date": "12 листопада"}, fonts=PDFIUM_ONLY).data)
    data = fill_form(earlier, {"name": "Jane Doe"}, fonts=PDFIUM_ONLY).data

    done = embed_fonts(data, "Jane Doe", with_fonts, aliases={}, fallbacks=("TestSans",))

    for font in _field_fonts(done.data, "date"):
        if font["/BaseFont"][1:] in STANDARD_14:
            continue
        assert _embedded(font) is not None
        drawn = {chr(u) for u in TTFont(io.BytesIO(_embedded(font))).getBestCmap()}
        assert set("листопада") <= drawn  # the date still has every letter it shows
