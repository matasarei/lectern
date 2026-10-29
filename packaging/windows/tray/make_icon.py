"""Make the Windows app's icon, lectern.ico: the Mac app's book (packaging/macos/tray/Icon.swift),
drawn the way Windows draws its own. No plate behind it: the book is the icon, filling the
square, with a soft shadow under it; and each size is drawn for itself, the smallest with less
on the page, rather than one picture shrunk. Run again after the book changes:

    uv run python packaging/windows/tray/make_icon.py
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZES = [16, 20, 24, 32, 40, 48, 64, 256]
SUPERSAMPLE = 4

# Icon.swift's colours.
COVER = (0x3B, 0x24, 0x16)
PAGE_LIGHT = (0xFB, 0xF5, 0xE6)
PAGE_DARK = (0xE6, 0xD9, 0xBC)
WRITING = (0x8C, 0x7B, 0x5C)
INITIAL = (0x8A, 0x1C, 0x1C)

# Icon.swift draws on a 1024-point canvas, y upwards, the spine at x = 512, the book between
# x 186 and 838. Here it's scaled up, in the Mac's proportions, to fill the square's width;
# y runs down.
SCALE = 1.45
MIDDLE_Y = 500


def main() -> None:
    images = [draw(size) for size in SIZES]
    images[-1].save(
        Path(__file__).with_name("lectern.ico"),
        sizes=[(size, size) for size in SIZES],
        append_images=images[:-1],
    )


def draw(size: int) -> Image.Image:
    """The icon at one size, drawn larger and scaled down so its edges are smooth."""
    canvas = size * SUPERSAMPLE
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))

    def at(x: float, y: float) -> tuple[float, float]:
        return (
            (512 + (x - 512) * SCALE) * canvas / 1024,
            (512 - (y - MIDDLE_Y) * SCALE) * canvas / 1024,
        )

    def points(path: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [at(x, y) for x, y in path]

    if size >= 32:  # below that a shadow is a smudge
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        left, top = at(200, 300)
        right, bottom = at(824, 250)
        ImageDraw.Draw(shadow).ellipse((left, top, right, bottom), fill=(0, 0, 0, 90))
        image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(canvas / 40)))

    paint = ImageDraw.Draw(image)
    for mirrored in (False, True):  # the cover, showing under the pages
        paint.polygon(points(page(672, 690, 312, 294, 186, mirrored)), fill=COVER)
    for mirrored in (False, True):  # the pages, lighter towards their outer edges
        outline = points(page(690, 706, 344, 326, 208, mirrored))
        image.alpha_composite(gradient(image.size, outline, toward_spine=PAGE_DARK))
    # ImageDraw replaces pixels, alpha and all: what's see-through is drawn on a layer of its
    # own and laid over, or it would make the pages under it see-through too.
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    fold(layer, at)
    paint = ImageDraw.Draw(layer)
    rows = 6 if size >= 32 else 3
    width = (11 if rows == 6 else 26) * SCALE * canvas / 1024
    gap = 48 if rows == 6 else 96
    for row in range(rows):
        y = 630 - row * gap
        start = (610 if row == 0 else 566, y - 4)
        end = (700 if row == rows - 1 else 772, y)
        line = points(cubic(start, (640, y + 8), (720, y + 6), end))
        paint.line(line, fill=(*WRITING, 220), width=round(width), joint="curve")
        for x, y in (line[0], line[-1]):  # round ends
            paint.ellipse(
                (x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=(*WRITING, 220)
            )
    image.alpha_composite(layer)

    paint = ImageDraw.Draw(image)
    paint.polygon(points(initial()), fill=INITIAL)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def page(
    spine_top: float,
    outer_top: float,
    outer_bottom: float,
    spine_bottom: float,
    outer_x: float,
    mirrored: bool,
) -> list[tuple[float, float]]:
    """One page, as Icon.swift's page(): from the spine's top over its swell to the outer edge,
    down, and back along the bottom."""

    def x(value: float) -> float:
        return 1024 - value if mirrored else value

    top = cubic(
        (x(512), spine_top),
        (x(440), spine_top + 46),
        (x(300), outer_top + 30),
        (x(outer_x), outer_top),
    )
    bottom = cubic(
        (x(outer_x), outer_bottom),
        (x(300), outer_bottom + 30),
        (x(440), spine_bottom + 46),
        (x(512), spine_bottom),
    )
    return top + bottom


def gradient(
    size: tuple[int, int], outline: list[tuple[float, float]], toward_spine: tuple[int, int, int]
) -> Image.Image:
    """A page filled from PAGE_LIGHT at its outer edge to the darker colour at the spine."""
    xs = [x for x, _ in outline]
    outer, spine = (
        (min(xs), max(xs)) if outline[0][0] > outline[len(outline) // 2][0] else (max(xs), min(xs))
    )
    fill = Image.new("RGBA", size, (0, 0, 0, 0))
    paint = ImageDraw.Draw(fill)
    for column in range(int(min(xs)), int(max(xs)) + 1):
        share = min(max((column - outer) / (spine - outer), 0), 1)
        colour = tuple(
            round(a + (b - a) * share) for a, b in zip(PAGE_LIGHT, toward_spine, strict=True)
        )
        paint.line([(column, 0), (column, size[1])], fill=(*colour, 255))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(outline, fill=255)
    shaped = Image.new("RGBA", size, (0, 0, 0, 0))
    shaped.paste(fill, mask=mask)
    return shaped


def fold(image: Image.Image, at: Callable[[float, float], tuple[float, float]]) -> None:
    """The shadow in the fold between the pages, strongest at the spine."""
    left, top = at(488, 698)
    right, bottom = at(536, 326)
    paint = ImageDraw.Draw(image)
    middle = (left + right) / 2
    for column in range(int(left), int(right) + 1):
        alpha = round(90 * (1 - abs(column - middle) / ((right - left) / 2)))
        paint.line([(column, top), (column, bottom)], fill=(*COVER, max(alpha, 0)))


def initial() -> list[tuple[float, float]]:
    """A serif capital L on the first page, where Icon.swift sets its initial: a stem with a
    bracketed head serif, and a foot whose end turns up."""
    return [
        (300, 652), (404, 652), (404, 640), (378, 632), (378, 418),  # the head serif, the stem
        (430, 418), (452, 470), (466, 470), (458, 392),  # the foot, its end turned up
        (300, 392), (300, 404), (326, 412), (326, 632), (300, 640),
    ]  # fmt: skip


def cubic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    steps: int = 32,
) -> list[tuple[float, float]]:
    """Points along a cubic Bézier curve, as the paths in Icon.swift are."""
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
