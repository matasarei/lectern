"""PDFs built for the PDF-editing tests, the way most PDFs carry their fonts: a font made here
(fontTools), embedded as a subset holding only the letters the PDF uses. The full font goes in
a folder the tests hand the engine as "installed", or doesn't, to test both."""

from __future__ import annotations

import ctypes
import io
import string
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest
from fontTools import subset
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from lectern.modules.pdf_edit.fonts import InstalledFonts

UKRAINIAN = "АБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯабвгґдеєжзиіїйклмнопрстуфхцчшщьюя’"
LETTERS = string.ascii_letters + string.digits + string.punctuation + " " + UKRAINIAN
SIZE = 12.0


def advance(ch: str) -> int:
    """Each letter's width in font units: varied, so a wrong font shows as a wrong width."""
    return 250 if ch == " " else 400 + (ord(ch) % 7) * 50


def build_font(path: Path, ps_name: str, *, fs_type: int = 0, windows_names: bool = True) -> Path:
    """A TrueType font with a box for every letter in LETTERS."""
    glyph = {ch: f"uni{ord(ch):04X}" for ch in LETTERS}
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", *glyph.values()])
    builder.setupCharacterMap({ord(ch): name for ch, name in glyph.items()})
    outlines, metrics = {}, {}
    for ch, name in [(None, ".notdef"), *glyph.items()]:
        width = 500 if ch is None else advance(ch)
        pen = TTGlyphPen(None)
        if ch != " ":
            pen.moveTo((50, 0))
            pen.lineTo((50, 700))
            pen.lineTo((width - 50, 700))
            pen.lineTo((width - 50, 0))
            pen.closePath()
        outlines[name] = pen.glyph()
        metrics[name] = (width, 0 if ch == " " else 50)
    builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable(
        {"familyName": "Lectern Test", "styleName": "Regular", "psName": ps_name},
        windows=windows_names,
    )
    builder.setupOS2(sTypoAscender=800, usWinAscent=800, usWinDescent=200, fsType=fs_type)
    builder.setupPost()
    builder.save(str(path))
    return path


@dataclass(frozen=True)
class Run:
    """One text object: its text, where it starts (after the previous run when None), and
    whether it is drawn in the second font or turned."""

    text: str
    x: float | None = None
    bold: bool = False
    rotated: bool = False


def width_pt(text: str, size: float = SIZE) -> float:
    return sum(advance(ch) for ch in text) * size / 1000


def _subset(font: Path, ps_name: str, text: str, tag: str) -> bytes:
    face = TTFont(font)
    options = subset.Options()
    options.name_IDs = ["*"]
    subsetter = subset.Subsetter(options)
    subsetter.populate(text=text)
    subsetter.subset(face)
    face["name"].setName(f"{tag}+{ps_name}", 6, 3, 1, 0x409)
    face["name"].setName(f"{tag}+{ps_name}", 6, 1, 0, 0)
    out = io.BytesIO()
    face.save(out)
    return out.getvalue()


def make_pdf(path: Path, fonts_dir: Path, pages: list[list[list[Run]]]) -> Path:
    """A PDF whose pages hold these lines of runs, 20 pt apart from the top-left margin, in
    subsets of TestSans (and TestSans-Bold for bold runs) cut to the letters used."""
    pdf = pdfium.PdfDocument.new()
    keep: list[object] = []
    runs = [run for page in pages for line in page for run in line]

    def load(ps_name: str, tag: str, bold: bool) -> object:
        text = "".join(r.text for r in runs if r.bold == bold) or " "
        data = _subset(fonts_dir / f"{ps_name}.ttf", ps_name, text, tag)
        buffer = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        keep.append(buffer)
        return pdfium_c.FPDFText_LoadFont(
            pdf.raw, buffer, len(data), pdfium_c.FPDF_FONT_TRUETYPE, 1
        )

    regular = load("TestSans", "AAAAAA", False)
    bold = load("TestSans-Bold", "BAAAAA", True)
    for lines in pages:
        page = pdf.new_page(612, 792)
        y = 720.0
        for line in lines:
            x = 72.0
            for run in line:
                x = run.x if run.x is not None else x
                font = bold if run.bold else regular
                obj = pdfium_c.FPDFPageObj_CreateTextObj(pdf.raw, font, SIZE)
                encoded = (run.text + "\0").encode("utf-16-le")
                buffer = ctypes.create_string_buffer(encoded, len(encoded))
                keep.append(buffer)
                pdfium_c.FPDFText_SetText(obj, ctypes.cast(buffer, pdfium_c.FPDF_WIDESTRING))
                if run.rotated:
                    pdfium_c.FPDFPageObj_Transform(obj, 0, 1, -1, 0, x, y)
                else:
                    pdfium_c.FPDFPageObj_Transform(obj, 1, 0, 0, 1, x, y)
                pdfium_c.FPDFPage_InsertObject(page.raw, obj)
                x += width_pt(run.text)
            y -= 20
        pdfium_c.FPDFPage_GenerateContent(page.raw)
        page.close()
    pdf.save(path)
    pdf.close()
    return path


@pytest.fixture
def font_files(tmp_path: Path) -> Path:
    """The full fonts the test PDFs are cut from."""
    folder = tmp_path / "font-files"
    folder.mkdir()
    build_font(folder / "TestSans.ttf", "TestSans")
    build_font(folder / "TestSans-Bold.ttf", "TestSans-Bold")
    return folder


@pytest.fixture
def no_fonts(tmp_path: Path) -> InstalledFonts:
    """A computer with none of the PDF's fonts installed."""
    return InstalledFonts([tmp_path / "nothing-installed"])


@pytest.fixture
def with_fonts(font_files: Path) -> InstalledFonts:
    """A computer with the PDF's fonts installed."""
    return InstalledFonts([font_files])


def page_text(data: bytes, index: int = 0) -> str:
    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[index]
        textpage = page.get_textpage()
        text = textpage.get_text_range()
        textpage.close()
        page.close()
        return text.replace("\r\n", "\n")
    finally:
        pdf.close()


def text_objects(data: bytes, index: int = 0) -> list[tuple[str, str, float, float]]:
    """(text, font name, left, right) of each text object on a page, in drawing order."""
    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[index]
        textpage = page.get_textpage()
        found = []
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_TEXT]):
            size = pdfium_c.FPDFTextObj_GetText(obj.raw, textpage.raw, None, 0)
            buffer = ctypes.create_string_buffer(max(size, 2))
            pdfium_c.FPDFTextObj_GetText(
                obj.raw, textpage.raw, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ushort)), size
            )
            text = buffer.raw[: max(size - 2, 0)].decode("utf-16-le")
            font = pdfium_c.FPDFTextObj_GetFont(obj.raw)
            name = ctypes.create_string_buffer(128)
            pdfium_c.FPDFFont_GetBaseFontName(font, name, 128)
            left, _, right, _ = obj.get_bounds()
            found.append((text, name.value.decode(), left, right))
        textpage.close()
        page.close()
        return found
    finally:
        pdf.close()
