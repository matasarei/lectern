"""Words the OCR model wasn't sure of, from the log-probabilities of its tokens.

A word counts as unsure when any token in it is less likely than LOW_CONFIDENCE (a natural
log: -3.0 is about a 5% chance). Unsure words are marked in the text cleanup reads, so the
model weighs them; a marked word still in a block afterwards becomes a doubt of its own
(source "ocr"). A reading with no log-probabilities (a runner that doesn't give them) has no
unsure words: its doubts come from cleanup alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from lectern.documents.document import Doubt

# Tuned in M3a's acceptance run (12 pages, 1017 words, 3 misread): PaddleOCR-VL's misreads sat
# between -1.0 and -0.5, among many words it read right, and cleanup doubted all three on its
# own. A mark there only made it echo correct words as doubts; only a far less likely word
# (a bad scan, handwriting) is worth one.
LOW_CONFIDENCE = -3.0
WORD = re.compile(r"\S+")
EDGE = re.compile(r"^[\W_]+|[\W_]+$")  # punctuation around a word, kept out of it
OCR_REASON = "The OCR model wasn't sure of this word."


def unsure_words(
    tokens: list[tuple[str, float]] | None, threshold: float = LOW_CONFIDENCE
) -> list[str]:
    """The words, in reading order and each once, that hold a token below the threshold."""
    if not tokens:
        return []
    text, unsure_spans, at = "", [], 0
    for token, logprob in tokens:
        if logprob < threshold and token.strip():
            unsure_spans.append((at, at + len(token)))
        text += token
        at += len(token)
    words: list[str] = []
    for match in WORD.finditer(text):
        start, end = match.span()
        if not any(s < end and e > start for s, e in unsure_spans):
            continue
        word = EDGE.sub("", match.group(0))
        if word and word not in words:
            words.append(word)
    return words


def mark_unsure(text: str, words: list[str]) -> str:
    """The raw page with unsure words marked ⟨word?⟩ for cleanup to consider."""
    for word in words:
        text = re.sub(rf"(?<![\w⟨]){re.escape(word)}(?!\w)", f"⟨{word}?⟩", text, count=1)
    return text


def ocr_doubts(text: str, words: list[str], new_id: Callable[[], str]) -> tuple[str, list[Doubt]]:
    """Doubts for the unsure words cleanup left as they were: each one's first place in
    `text` becomes {id}. Words cleanup changed, or already inside a doubt, are skipped."""
    doubts: list[Doubt] = []
    for word in words:
        pattern = re.compile(rf"(?<![\w{{.]){re.escape(word)}(?!\w)")
        match = pattern.search(text)
        if match is None:
            continue
        doubt_id = new_id()
        text = text[: match.start()] + f"{{{doubt_id}}}" + text[match.end() :]
        doubts.append(Doubt(id=doubt_id, raw=word, source="ocr", reason=OCR_REASON))
    return text, doubts
