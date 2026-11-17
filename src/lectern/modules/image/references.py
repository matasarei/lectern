"""Reference pictures for `image.generate`: pictures in the project a new one is made from (a
person or a thing to keep, a style, a layout).

A reference is named by its path in the project and found among the person's own live files,
as every tool finds a file; someone else's is "no picture called…". It is opened as an image,
so its content decides, not its name, and only in the raster formats listed here: Pillow can
open others through outside programs (EPS through Ghostscript), and none of those is needed.
What sd-cli gets is a PNG Lectern wrote again from the pixels: upright, in RGB, without the
source's metadata, and scaled so the references together hold no more pixels than the picture
being made. sd.cpp takes FLUX.2's references at their own size, and each one's pixels are
attended to at every step, so a 12 MP photo as it is would take far more memory and time than
the picture itself.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from lectern.agent.tools import ToolContext, ToolError
from lectern.modules.image.generate import Reference
from lectern.projects import files

FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF")
MAX_SOURCE_PIXELS = 64_000_000  # a larger picture is refused before it is decoded
SIDE_STEP = 16  # sd.cpp's VAE and patching want sides in multiples of 16
MIN_SIDE = 64


@dataclass(frozen=True)
class Found:
    """A reference picture found in the project: its path there, and the file."""

    path: str
    artifact_id: int
    file: Path


def find(ctx: ToolContext, paths: Sequence[str]) -> list[Found]:
    """The person's own live files at these paths (exactly, or ignoring case when that names
    one file), in the order given, each once. ToolError for a path that names none."""
    wanted = list(dict.fromkeys(p.strip() for p in paths if p.strip()))
    with ctx.sessions() as session:
        rows = [r for r in files.live(session) if r.owner_id == ctx.user.id]
    found: list[Found] = []
    for path in wanted:
        row = next((r for r in rows if r.path == path), None)
        if row is None:
            folded = [r for r in rows if r.path.casefold() == path.casefold()]
            row = folded[0] if len(folded) == 1 else None
        if row is None:
            raise ToolError(
                f"No picture called {path!r} in this project. Use project.find to get its name."
            )
        if any(f.artifact_id == row.id for f in found):
            continue
        try:
            file = files.inside(ctx.project.root, row.path)
        except files.BadName:
            raise ToolError(f"{path!r} can't be used as a reference picture.") from None
        found.append(Found(row.path, row.id, file))
    return found


def load(found: Sequence[Found], max_pixels: int) -> list[Reference]:
    """Each found picture made ready for sd-cli: upright, RGB, written again as a PNG, and
    scaled down so they hold at most `max_pixels` together (an equal share each). ToolError for
    one that isn't a picture."""
    if not found:
        return []
    share = max_pixels // len(found)
    return [_load_one(f, share) for f in found]


def _load_one(found: Found, max_pixels: int) -> Reference:
    not_a_picture = ToolError(f"{found.path} isn't a picture that can be used as a reference.")
    try:
        with Image.open(found.file, formats=FORMATS) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ToolError(f"{found.path} is too large to use as a reference picture.")
            source.load()
            upright = ImageOps.exif_transpose(source)
            picture = _rgb(upright)
    # Pillow's ways of saying "not a picture", or a broken one
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        raise not_a_picture from None
    width, height = _fitted(picture.width, picture.height, max_pixels)
    if (width, height) != picture.size:
        picture = picture.resize((width, height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    picture.save(out, format="PNG")  # a new file: none of the source's metadata comes along
    return Reference(out.getvalue(), width, height)


def _rgb(image: Image.Image) -> Image.Image:
    """RGB, with transparent parts on white rather than black."""
    if image.mode == "RGB":
        return image.copy()
    rgba = image.convert("RGBA")
    white = Image.new("RGB", rgba.size, "white")
    white.paste(rgba, mask=rgba.getchannel("A"))
    return white


def _fitted(width: int, height: int, max_pixels: int) -> tuple[int, int]:
    """The size within `max_pixels`, keeping the shape, in multiples of 16 and never smaller
    than 64 a side; a picture already within it only loses the odd pixels at its edges."""
    scale = min(1.0, (max_pixels / (width * height)) ** 0.5)

    def side(value: float) -> int:
        return max(MIN_SIDE, int(value) // SIDE_STEP * SIDE_STEP)

    return side(width * scale), side(height * scale)
