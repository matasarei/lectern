"""OCR text as it reads: markup removed for comparing, and the check for a model that went
round again (read a page, then repeated it)."""

from __future__ import annotations

import html
import re
import unicodedata

ANY_TAG = re.compile(r"<[^<>]+>")
TABLE_RULE = re.compile(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-*:?\s*\|?\s*$", re.M)  # |---|:--|
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
MARKERS = re.compile(r"[*_`|]+")
SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """The text as it reads, without markup: one space between words."""
    # Every tag is a word break: cells, paragraphs and Chandra's layout divs all end words.
    text = html.unescape(ANY_TAG.sub(" ", text))
    text = TABLE_RULE.sub("", text)
    text = HEADING.sub("", text)
    text = MARKERS.sub(" ", text)
    return SPACE.sub(" ", unicodedata.normalize("NFC", text)).strip()


LOOP_WORDS = 12  # a run of this many words seen twice means the model went round again


def looped(text: str) -> bool:
    """The output went round again: some run of LOOP_WORDS words appears twice. Words, not
    lines: a table's rows can repeat a few values ("100 1 $100"), and in HTML those are
    whole identical lines."""
    words = normalise(text).split()
    seen: set[tuple[str, ...]] = set()
    for i in range(len(words) - LOOP_WORDS + 1):
        window = tuple(words[i : i + LOOP_WORDS])
        if window in seen:
            return True
        seen.add(window)
    return False
