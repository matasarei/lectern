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


LOOP_ROUNDS = 3  # the same lines, said at least this many times running, to the end of the text…
LOOP_LINES = 20  # …with at least this many lines of it beyond the first time
LOOP_UNIT = 60  # the longest run of lines looked for as the thing repeated


def cut_trailing_loop(text: str) -> tuple[str, str, int]:
    """A reading that ran away: the model got stuck on a line, or a run of lines, and said it
    until it ran out of room ("Red" 3,451 times at the end of a scanner test page). Returns
    the text cut after the first time, the lines that were repeated, and how many more times
    they came (the last one may be cut short); or the text as it is, "" and 0.

    Only at the end, and only a long run: a page's own repeats (six blank signature lines, a
    table's equal rows) are short, and a model that escapes a loop goes on to other text."""
    lines = text.rstrip().splitlines()
    total = len(lines)
    for unit in range(1, min(LOOP_UNIT, total // LOOP_ROUNDS) + 1):
        start = total - unit  # where the tail that repeats every `unit` lines begins
        while start > 0:
            later = start - 1 + unit
            if not _repeats(lines[start - 1], lines[later], is_last=later == total - 1):
                break
            start -= 1
        again = total - start - unit  # lines of it beyond the first time
        if again >= (LOOP_ROUNDS - 1) * unit and again >= LOOP_LINES:
            first = lines[start : start + unit]
            return "\n".join(lines[: start + unit]), "\n".join(first), -(-again // unit)
    return text, "", 0


def _repeats(line: str, later: str, *, is_last: bool) -> bool:
    """`later` is `line` said again; the text's very last line may be the start of it only."""
    return line == later or (is_last and bool(later) and line.startswith(later))
