"""A 12-page image-only PDF that looks scanned: Ukrainian lease text, a table, page numbers.

Throwaway, for the M4 Windows run. Writes only the PDF named on the command line.
"""

import random
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

DPI = 200
W, H = int(8.27 * DPI), int(11.69 * DPI)
FONT = ImageFont.truetype(r"C:\Windows\Fonts\times.ttf", 36)
BOLD = ImageFont.truetype(r"C:\Windows\Fonts\timesbd.ttf", 44)
SMALL = ImageFont.truetype(r"C:\Windows\Fonts\times.ttf", 28)

PARAGRAPHS = [
    "Орендодавець передає, а Орендар приймає у тимчасове платне користування нежитлове "
    "приміщення загальною площею 48,6 кв. м, що розташоване за адресою: м. Київ, "
    "вул. Січових Стрільців, 21, офіс 14.",
    "Орендна плата становить 18 500,00 грн (вісімнадцять тисяч п'ятсот гривень 00 копійок) "
    "на місяць і сплачується не пізніше п'ятого числа поточного місяця.",
    "Орендар зобов'язаний використовувати приміщення виключно за призначенням, підтримувати "
    "його в належному стані та своєчасно сплачувати комунальні послуги.",
    "Сторони звільняються від відповідальності за часткове або повне невиконання зобов'язань, "
    "якщо воно стало наслідком обставин непереборної сили.",
    "Усі спори, що виникають з цього Договору, вирішуються шляхом переговорів, а в разі "
    "недосягнення згоди — у судовому порядку відповідно до чинного законодавства України.",
]
ROWS = [
    ("Місяць", "Сума, грн", "Термін оплати"),
    ("Січень", "18 500,00", "05.01.2026"),
    ("Лютий", "18 500,00", "05.02.2026"),
    ("Березень", "19 200,00", "05.03.2026"),
    ("Квітень", "19 200,00", "05.04.2026"),
]


def wrap(draw: ImageDraw.ImageDraw, text: str, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=FONT) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line]


def page(n: int, rng: random.Random) -> Image.Image:
    img = Image.new("L", (W, H), 250)
    d = ImageDraw.Draw(img)
    x, y, width = 160, 180, W - 320
    d.text((x, 90), "Договір оренди № 17/2026", font=SMALL, fill=90)  # running header
    d.text((x, y), f"{n}. Розділ {n}", font=BOLD, fill=20)
    y += 90
    for i in range(3):
        for line in wrap(d, PARAGRAPHS[(n + i) % len(PARAGRAPHS)], width):
            d.text((x, y), line, font=FONT, fill=25)
            y += 50
        y += 30
    if n % 3 == 0:
        cols = (x, x + 420, x + 800, x + width)
        for r, row in enumerate(ROWS):
            top = y + r * 60
            d.line((x, top, x + width, top), fill=40, width=2)
            for c, cell in enumerate(row):
                d.text((cols[c] + 15, top + 12), cell, font=FONT, fill=25)
        d.line((x, y + len(ROWS) * 60, x + width, y + len(ROWS) * 60), fill=40, width=2)
        for cx in cols:
            d.line((cx, y, cx, y + len(ROWS) * 60), fill=40, width=2)
    d.text((W // 2 - 10, H - 120), str(n), font=SMALL, fill=60)  # page number
    for _ in range(400):  # specks
        px, py = rng.randrange(W), rng.randrange(H)
        d.point((px, py), fill=rng.randrange(80, 200))
    img = img.rotate(rng.uniform(-0.6, 0.6), fillcolor=250, resample=Image.BICUBIC)
    return img.filter(ImageFilter.GaussianBlur(0.6)).convert("RGB")


def main() -> None:
    rng = random.Random(4)
    pages = [page(n, rng) for n in range(1, 13)]
    pages[0].save(sys.argv[1], save_all=True, append_images=pages[1:], resolution=DPI)


if __name__ == "__main__":
    main()
