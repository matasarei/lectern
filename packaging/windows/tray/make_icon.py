"""Make the Windows app's icon, lectern.ico: the Mac app's idea (packaging/macos/tray/Icon.swift),
an open book with a red initial L, drawn the way Windows 11 draws its icons. Simple rounded
shapes lit from above, a soft shadow under them, and no plate: the book is the icon. Lectern's
green, the Mac icon's plate, is the cover here. The small sizes are drawn simpler, not shrunk.
Run again after changing it:

    uv run python packaging/windows/tray/make_icon.py
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SIZES = [16, 20, 24, 32, 40, 48, 64, 256]
SUPERSAMPLE = 8

Colour = tuple[int, int, int]
Point = tuple[float, float]

COVER_TOP: Colour = (0x3A, 0x85, 0x70)  # Lectern's green, as the Mac icon's plate
COVER_BOTTOM: Colour = (0x1F, 0x4F, 0x41)
EDGES: Colour = (0xE4, 0xD8, 0xC0)  # the pages under the open two, seen at their edges
PAGE_OUTER: Colour = (0xFF, 0xFD, 0xF8)
PAGE_SPINE: Colour = (0xEF, 0xE6, 0xD3)
INITIAL_TOP: Colour = (0xC2, 0x34, 0x2B)
INITIAL_BOTTOM: Colour = (0x8A, 0x1C, 0x1C)
WRITING: Colour = (0xB4, 0xA6, 0x8C)


def main() -> None:
    images = [draw(size) for size in SIZES]
    images[-1].save(
        Path(__file__).with_name("lectern.ico"),
        sizes=[(size, size) for size in SIZES],
        append_images=images[:-1],
    )


def draw(size: int) -> Image.Image:
    """The icon at one size. Everything is laid out on a 256-unit square, y downwards, the
    spine at x = 128; it's drawn larger than asked and scaled down, so its edges are smooth."""
    canvas = size * SUPERSAMPLE
    unit = canvas / 256
    small = size <= 24
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))

    def at(path: list[Point]) -> list[Point]:
        return [(x * unit, y * unit) for x, y in path]

    cover = at(book(outer_x=14, outer_top=58, outer_bottom=206, spine_top=72, spine_bottom=222))
    edges = at(book(outer_x=21, outer_top=52, outer_bottom=199, spine_top=66, spine_bottom=214))
    pages = at(book(outer_x=27, outer_top=44, outer_bottom=191, spine_top=60, spine_bottom=205))

    if not small:  # Windows 11's shadow: soft, below, a little apart
        shadow = Image.new("L", image.size, 0)
        ImageDraw.Draw(shadow).polygon(at(book(20, 70, 216, 84, 230)), fill=70)
        shadow = shadow.filter(ImageFilter.GaussianBlur(10 * unit))
        black = Image.new("L", image.size, 0)
        image.alpha_composite(Image.merge("RGBA", (black, black, black, shadow)))

    shade(image, cover, vertical(image.size, COVER_TOP, COVER_BOTTOM, 58 * unit, 222 * unit))
    shade(image, edges, solid(image.size, EDGES))
    shade(image, pages, across(image.size, unit))

    if not small:  # the fold: the pages darken into the spine
        fold = Image.new("RGBA", image.size, (0, 0, 0, 0))
        paint = ImageDraw.Draw(fold)
        for column in range(round(112 * unit), round(144 * unit)):
            depth = 1 - abs(column / unit - 128) / 16
            paint.line([(column, 0), (column, canvas)], fill=(*COVER_BOTTOM, round(70 * depth**2)))
        shade(image, pages, fold)

    initial(image, unit, small)
    writing(image, size, unit, small)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def book(
    outer_x: float, outer_top: float, outer_bottom: float, spine_top: float, spine_bottom: float
) -> list[Point]:
    """Both halves of an open book's outline: from the spine's top up over each page's swell to
    its rounded outer corner, down its edge, and back to the spine along a gentle curve."""

    def half(mirrored: bool) -> list[Point]:
        def x(value: float) -> float:
            return 256 - value if mirrored else value

        corner = 9
        path = cubic(
            (x(128), spine_top),
            (x(100), outer_top - 2),
            (x(60), outer_top - 6),
            (x(outer_x + corner), outer_top),
        )
        path += cubic(
            (x(outer_x + corner), outer_top),
            (x(outer_x + 2), outer_top + 1),
            (x(outer_x), outer_top + 4),
            (x(outer_x), outer_top + corner),
        )
        path += [(x(outer_x), outer_bottom - corner)]
        path += cubic(
            (x(outer_x), outer_bottom - corner),
            (x(outer_x), outer_bottom - 3),
            (x(outer_x + 3), outer_bottom),
            (x(outer_x + corner), outer_bottom),
        )
        path += cubic(
            (x(outer_x + corner), outer_bottom),
            (x(64), outer_bottom - 2),
            (x(104), spine_bottom - 6),
            (x(128), spine_bottom),
        )
        return path

    return half(mirrored=False) + half(mirrored=True)[::-1]


def initial(image: Image.Image, unit: float, small: bool) -> None:
    """The L on the first page, as Windows 11 sets a letter in an icon: two rounded bars, no
    serifs, lit from above like the rest. Bolder when the icon is small."""
    weight = 24 if small else 20
    left, right, top, bottom = 52, 106, 82, 166
    bars = [
        (left, top, left + weight, bottom),  # the stem
        (left, bottom - weight, right, bottom),  # the foot
    ]
    mask = Image.new("L", image.size, 0)
    paint = ImageDraw.Draw(mask)
    for x0, y0, x1, y1 in bars:
        radius = min(x1 - x0, y1 - y0) / 2 * 0.45
        box = (x0 * unit, y0 * unit, x1 * unit, y1 * unit)
        paint.rounded_rectangle(box, radius=radius * unit, fill=255)
    colour = vertical(image.size, INITIAL_TOP, INITIAL_BOTTOM, top * unit, bottom * unit)
    letter = Image.new("RGBA", image.size, (0, 0, 0, 0))
    letter.paste(colour, mask=mask)
    image.alpha_composite(letter)


def writing(image: Image.Image, size: int, unit: float, small: bool) -> None:
    """Lines of writing on the second page, the first and the last short: fewer and bolder as
    the icon gets smaller, and each on a whole pixel, so they stay sharp."""
    rows = 3 if small else 4 if size <= 48 else 5
    thickness = max((9 if small else 7) * size / 256, 1.5)  # in the icon's own pixels
    top, bottom = 92, 160
    paint = ImageDraw.Draw(image)
    for row in range(rows):
        y = top + row * (bottom - top) / (rows - 1)
        pixel = round(y * size / 256 - thickness / 2) + thickness / 2
        left = 152 if row == 0 else 142
        right = 192 if row == rows - 1 else 214
        y0 = (pixel - thickness / 2) * SUPERSAMPLE
        y1 = (pixel + thickness / 2) * SUPERSAMPLE
        box = (left * unit, y0, right * unit, y1)
        paint.rounded_rectangle(box, radius=(y1 - y0) / 2, fill=(*WRITING, 255))


def shade(image: Image.Image, outline: list[Point], fill: Image.Image) -> None:
    """Lay `fill` over the image inside `outline`, keeping what's see-through in it."""
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).polygon(outline, fill=255)
    layer = fill.copy()
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    image.alpha_composite(layer)


def solid(size: tuple[int, int], colour: Colour) -> Image.Image:
    return Image.new("RGBA", size, (*colour, 255))


def vertical(
    size: tuple[int, int], top: Colour, bottom: Colour, start: float, end: float
) -> Image.Image:
    """From `top` at y = start to `bottom` at y = end: the light from above."""
    return gradient(size, lambda _x, y: (y - start) / (end - start), top, bottom, along_x=False)


def across(size: tuple[int, int], unit: float) -> Image.Image:
    """The pages: brightest at their outer edges, warmer towards the spine."""
    return gradient(
        size, lambda x, _y: 1 - abs(x / unit - 128) / 100, PAGE_OUTER, PAGE_SPINE, along_x=True
    )


def gradient(
    size: tuple[int, int],
    share: Callable[[float, float], float],
    start: Colour,
    end: Colour,
    along_x: bool,
) -> Image.Image:
    """A fill from `start` to `end` as `share` goes from 0 to 1, drawn a column (along_x) or a
    row at a time: the gradients here each run one way only."""
    width, height = size
    image = Image.new("RGBA", size)
    paint = ImageDraw.Draw(image)

    def colour(t: float) -> tuple[int, int, int, int]:
        t = min(max(t, 0), 1)
        r, g, b = (round(a + (z - a) * t) for a, z in zip(start, end, strict=True))
        return (r, g, b, 255)

    if along_x:
        for x in range(width):
            paint.line([(x, 0), (x, height)], fill=colour(share(x, 0)))
    else:
        for y in range(height):
            paint.line([(0, y), (width, y)], fill=colour(share(0, y)))
    return image


def cubic(p0: Point, p1: Point, p2: Point, p3: Point, steps: int = 32) -> list[Point]:
    """Points along a cubic Bézier curve."""
    out = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out.append(
            (
                u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
            )
        )
    return out


if __name__ == "__main__":
    main()
