from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from lectern.modules.ocr import pages
from lectern.modules.ocr.pages import NotAPageSource, rasterise

from ...projects.conftest import write_pdf, write_scan_pdf


def _about(page, width: int, height: int) -> bool:
    """PDFium rounds the rendered size, so a pixel either way is still 300 dpi."""
    return abs(page.width - width) <= 1 and abs(page.height - height) <= 1


def test_a_pdf_becomes_300_dpi_pages_with_its_text_layer_as_a_hint(tmp_path: Path):
    source = write_pdf(tmp_path / "lease.pdf", ["Lease agreement of 1998, page one", "Page two"])

    result = rasterise(source, tmp_path / "pages")

    assert [p.path.name for p in result] == ["p001.png", "p002.png"]
    assert _about(result[0], 2550, 3300)  # US Letter at 300 dpi
    assert result[0].text_hint == "Lease agreement of 1998, page one"
    assert result[1].text_hint is None  # too short to count as a text layer
    with Image.open(result[0].path) as image:
        assert image.size == (result[0].width, result[0].height)
        assert round(image.info["dpi"][0]) == 300


def test_a_scan_pdf_has_no_hint(tmp_path: Path):
    result = rasterise(write_scan_pdf(tmp_path / "scan.pdf", pages=1), tmp_path / "pages")

    assert len(result) == 1 and result[0].text_hint is None
    assert _about(result[0], 2480, 3508)  # A4


def test_a_photo_is_turned_upright_and_kept_at_its_size(tmp_path: Path):
    photo = tmp_path / "photo.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90° clockwise to view
    Image.new("RGB", (400, 200), "white").save(photo, exif=exif)

    (page,) = rasterise(photo, tmp_path / "pages")

    assert (page.width, page.height) == (200, 400) and page.path.suffix == ".png"


def test_each_frame_of_a_tiff_is_a_page(tmp_path: Path):
    tiff = tmp_path / "scan.tiff"
    frames = [Image.new("L", (100, 140), shade) for shade in (0, 128, 255)]
    frames[0].save(tiff, save_all=True, append_images=frames[1:])

    result = rasterise(tiff, tmp_path / "pages")

    assert [p.number for p in result] == [1, 2, 3]
    with Image.open(result[1].path) as image:
        assert image.getpixel((0, 0)) == 128


def test_transparency_becomes_white_not_black(tmp_path: Path):
    png = tmp_path / "clip.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(png)

    (page,) = rasterise(png, tmp_path / "pages")

    with Image.open(page.path) as image:
        assert image.convert("RGB").getpixel((5, 5)) == (255, 255, 255)


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("notes.txt", b"hello", "isn't a PDF or an image"),
        ("fake.png", b"not an image", "couldn't be read as an image"),
        ("broken.pdf", b"%PDF-1.4 nonsense", "couldn't be opened as a PDF"),
    ],
)
def test_what_cant_be_paged_says_why(tmp_path: Path, name: str, content: bytes, message: str):
    source = tmp_path / name
    source.write_bytes(content)

    with pytest.raises(NotAPageSource, match=message):
        rasterise(source, tmp_path / "pages")


def test_too_many_pages_are_refused_before_any_is_written(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pages, "MAX_PAGES", 2)
    tiff = tmp_path / "long.tiff"
    frames = [Image.new("L", (10, 10)) for _ in range(3)]
    frames[0].save(tiff, save_all=True, append_images=frames[1:])

    with pytest.raises(NotAPageSource, match="has 3 pages; an OCR session takes at most 2"):
        rasterise(tiff, tmp_path / "pages")
    assert not list((tmp_path / "pages").glob("*.png"))
    with pytest.raises(NotAPageSource, match="has 3 pages"):
        rasterise(write_scan_pdf(tmp_path / "long.pdf", pages=3), tmp_path / "pages2")
