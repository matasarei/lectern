"""Page images: the first phase of OCR (blueprint: "Rasterise").

Each PDF page becomes a 300 dpi PNG. A page's text layer, if it has one, is kept as a hint
for later phases. Images are kept as they are, only turned upright (EXIF) and saved as PNG so
a browser can show them; a multi-page TIFF gives one page per frame. Nothing is resized here:
each OCR model gets its own smaller copy when a page is recognised.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lectern.projects.text import PDF_PAGE_MIN_CHARS, PDFIUM_LOCK

DPI = 300
MAX_PAGES = 1000
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"})


@dataclass(frozen=True)
class PageImage:
    number: int  # from 1
    path: Path
    width: int
    height: int
    text_hint: str | None = None  # the PDF's own text for this page, when it has some


class NotAPageSource(Exception):
    """The file can't be turned into pages. The message says why, for the person."""


def page_file(number: int) -> str:
    return f"p{number:03d}.png"


def rasterise(source: Path, out_dir: Path) -> list[PageImage]:
    """Write `p001.png`, `p002.png`… into `out_dir`, one per page of `source`."""
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        out_dir.mkdir(parents=True, exist_ok=True)
        # PDFium isn't thread-safe, and this runs in worker threads.
        with PDFIUM_LOCK:
            return _pdf_pages(source, out_dir)
    if suffix in IMAGE_SUFFIXES:
        out_dir.mkdir(parents=True, exist_ok=True)
        return _image_pages(source, out_dir)
    raise NotAPageSource(f"{source.name} isn't a PDF or an image (PNG, JPEG, TIFF or WebP).")


def _too_many(name: str, count: int) -> NotAPageSource:
    return NotAPageSource(f"{name} has {count} pages; an OCR session takes at most {MAX_PAGES}.")


def _pdf_pages(source: Path, out_dir: Path) -> list[PageImage]:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(source)
    except pdfium.PdfiumError:
        raise NotAPageSource(f"{source.name} couldn't be opened as a PDF.") from None
    try:
        if len(pdf) > MAX_PAGES:
            raise _too_many(source.name, len(pdf))
        if len(pdf) == 0:
            raise NotAPageSource(f"{source.name} has no pages.")
        pages = []
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                text = textpage.get_text_bounded().replace("\r\n", "\n").strip()
                textpage.close()
                image = page.render(scale=DPI / 72).to_pil()
            finally:
                page.close()
            number = index + 1
            path = out_dir / page_file(number)
            image.save(path, dpi=(DPI, DPI))
            hint = text if len(text) >= PDF_PAGE_MIN_CHARS else None
            pages.append(PageImage(number, path, image.width, image.height, hint))
        return pages
    finally:
        pdf.close()


def _image_pages(source: Path, out_dir: Path) -> list[PageImage]:
    from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

    try:
        with Image.open(source) as image:
            frames = getattr(image, "n_frames", 1)
            if frames > MAX_PAGES:
                raise _too_many(source.name, frames)
            pages = []
            for number, frame in enumerate(ImageSequence.Iterator(image), start=1):
                upright = _flatten(ImageOps.exif_transpose(frame))
                path = out_dir / page_file(number)
                upright.save(path)
                pages.append(PageImage(number, path, upright.width, upright.height))
            return pages
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise NotAPageSource(f"{source.name} couldn't be read as an image ({exc}).") from None


def _flatten(image):  # noqa: ANN001, ANN202 — a PIL image in, a PNG-ready one out
    """Grey and colour pages stay as they are. Deep greys (16-bit and wider, common from
    scanners) become 8-bit grey; converted directly, every value above 255 would clip to
    white. Anything else (CMYK, palette, transparency) becomes RGB on white, so transparent
    parts don't turn black."""
    from PIL import Image

    if image.mode in ("1", "L", "RGB"):
        return image
    if image.mode.startswith("I;16") or image.mode in ("I", "F"):
        wide = image.convert("F")
        _low, high = wide.getextrema()
        if image.mode.startswith("I;16") or high > 255:
            scale = 255 / 65535  # a 16-bit range
        elif image.mode == "F" and high <= 1:
            scale = 255.0  # floats from 0 to 1
        else:
            scale = 1.0  # already 0-255
        return wide.point(lambda v: v * scale).convert("L")
    rgba = image.convert("RGBA")
    white = Image.new("RGB", rgba.size, "white")
    white.paste(rgba, mask=rgba.getchannel("A"))
    return white
