"""The OCR models on a real llama-server: each downloaded one reads a generated page of
known text. Needs a Lectern home with OCR models in it:

    LECTERN_TEST_OCR_HOME=<a Lectern home> uv run pytest -m gpu -s tests/modules/ocr

It prints what each model cost (prompt tokens at its max_side, time), which is what the
catalog's max_side values are tuned from.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from lectern.dev.agent_check import CLAUSE_3_3, LEASE_CLAUSES
from lectern.dev.ocr_check import candidates, cer
from lectern.modules.ocr.recognise import recognise

HOME = os.environ.get("LECTERN_TEST_OCR_HOME")
FONTS = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]
LINES = [
    "ДОГОВІР ОРЕНДИ НЕЖИТЛОВОГО ПРИМІЩЕННЯ",
    *(f"{title}. {body}" for title, body in LEASE_CLAUSES[:2]),
    f"3.3. {CLAUSE_3_3}",
    "Payment reference: LEASE-1998/07, IBAN UA00 0000 0000 0000 0000 0000 000",
]

pytestmark = [
    pytest.mark.gpu,
    pytest.mark.skipif(not HOME, reason="needs LECTERN_TEST_OCR_HOME with OCR models downloaded"),
]


def _page(path: Path) -> Path:
    """An A4 page at 300 dpi, 12 pt text wrapped to the margins."""
    font_file = next((f for f in FONTS if Path(f).is_file()), None)
    if font_file is None:
        pytest.skip("no font with Cyrillic letters found")
    font = ImageFont.truetype(font_file, 50)
    page = Image.new("L", (2480, 3508), 255)
    draw = ImageDraw.Draw(page)
    y = 300
    for line in LINES:
        words, row = line.split(), ""
        for word in words:
            if draw.textlength(f"{row} {word}", font=font) > 2480 - 2 * 250:
                draw.text((250, y), row, font=font, fill=0)
                y, row = y + 75, word
            else:
                row = f"{row} {word}".strip()
        draw.text((250, y), row, font=font, fill=0)
        y += 110
    page.save(path, dpi=(300, 300))
    return path


@pytest.fixture(scope="module")
def runtime():
    from lectern.cli import _local_runtime

    with pytest.MonkeyPatch.context() as env:
        env.setenv("LECTERN_HOME", str(Path(HOME).expanduser()))
        rt = _local_runtime()
        yield rt
        asyncio.run(rt.stop_all())


@pytest.mark.parametrize(("key", "entry"), candidates(), ids=lambda v: getattr(v, "id", v))
def test_each_downloaded_ocr_model_reads_a_clean_page(runtime, tmp_path, key, entry):
    from lectern.runtime.supervisor import NotAvailable

    page = _page(tmp_path / "p001.png")

    async def go():  # noqa: ANN202
        async with runtime.lease(key) as lease:
            return await recognise(
                lease.client, page, prompt=entry.ocr.prompt, max_side=entry.ocr.max_side
            )

    try:
        text = asyncio.run(go())
    except NotAvailable as exc:
        pytest.skip(str(exc))
    error = cer(text.text, "\n".join(LINES))
    print(  # noqa: T201 — the numbers the catalog's max_side is tuned from
        f"\n{entry.id}: CER {error:.1%}, prompt {text.prompt_tokens} tokens at max_side "
        f"{entry.ocr.max_side}, {text.completion_tokens} out, {text.seconds}s"
    )
    assert not text.cut_off
    if key.startswith("ocr@"):  # the printed-text model; handwriting models are scored too
        assert error < 0.05, text.text
