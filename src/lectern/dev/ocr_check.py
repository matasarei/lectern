"""The OCR check (blueprint, M2): score candidate OCR models on your own pages.

Each model's text is compared with a reference text you corrected by hand. The scores are the
character and word error rates (CER, WER): the edits needed to turn the model's text into the
reference, divided by the reference's length. Both texts are normalised first, so markup the
models add (Markdown markers, HTML table tags, spacing) doesn't count as an error; the words
and characters do.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Hashable, Sequence

BREAK_TAGS = re.compile(r"<\s*(br|/p|/tr|/li|/h[1-6])\s*/?\s*>", re.I)
CELL_TAGS = re.compile(r"<\s*/t[dh]\s*>", re.I)
ANY_TAG = re.compile(r"<[^<>]+>")
TABLE_RULE = re.compile(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-*:?\s*\|?\s*$", re.M)  # |---|:--|
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
MARKERS = re.compile(r"[*_`|]+")
SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """The text as it reads, without markup: one space between words."""
    text = BREAK_TAGS.sub("\n", text)
    text = CELL_TAGS.sub(" ", text)
    text = html.unescape(ANY_TAG.sub("", text))
    text = TABLE_RULE.sub("", text)
    text = HEADING.sub("", text)
    text = MARKERS.sub(" ", text)
    return SPACE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def distance(a: Sequence[Hashable], b: Sequence[Hashable]) -> int:
    """Levenshtein distance. Myers' bit-parallel algorithm (Hyyrö's form): one pass over `a`
    with Python's big integers as bit vectors over `b`, so a page of a few thousand characters
    takes milliseconds rather than the seconds a plain table would."""
    if len(b) > len(a):
        a, b = b, a
    m = len(b)
    if m == 0:
        return len(a)
    peq: dict[Hashable, int] = {}
    for i, item in enumerate(b):
        peq[item] = peq.get(item, 0) | (1 << i)
    full = (1 << m) - 1
    last = 1 << (m - 1)
    pv, mv, score = full, 0, m
    for item in a:
        eq = peq.get(item, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | ~(xh | pv)
        mh = pv & xh
        if ph & last:
            score += 1
        elif mh & last:
            score -= 1
        ph = (ph << 1) | 1
        mh = mh << 1
        pv = (mh | ~(xv | ph)) & full
        mv = ph & xv & full
    return score


def cer(output: str, reference: str) -> float:
    ref, out = normalise(reference), normalise(output)
    return distance(out, ref) / max(len(ref), 1)


def wer(output: str, reference: str) -> float:
    ref, out = normalise(reference).split(), normalise(output).split()
    return distance(out, ref) / max(len(ref), 1)
