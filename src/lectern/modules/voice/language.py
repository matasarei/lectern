"""The language a reply is in, among the ones some voice speaks.

Worked out once per reply, not per sentence: a short sentence is easily taken for another
language. Ukrainian and Russian are told apart by their own letters first, which settles most
Cyrillic text whatever the statistics say; the rest is py3langid's guess (BSD-3, its model loads
as npz with pickle refused).
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache

UKRAINIAN_LETTERS = frozenset("іїєґІЇЄҐ")
# Not "ъ": one of Bulgarian's commonest letters, and rare in Russian, so it settles nothing.
RUSSIAN_LETTERS = frozenset("ыэёЫЭЁ")
MIN_LETTERS = 3  # fewer than this says nothing about a language


@cache
def _identifier():  # noqa: ANN202 (py3langid has no types)
    from py3langid.langid import MODEL_FILE, LanguageIdentifier

    return LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)


def detect(text: str, offered: Sequence[str]) -> str | None:
    """The language of the text, if it is one of `offered`; None when the text has too few
    letters to tell, or none is offered."""
    if not offered or sum(c.isalpha() for c in text) < MIN_LETTERS:
        return None
    ukrainian = sum(c in UKRAINIAN_LETTERS for c in text)
    russian = sum(c in RUSSIAN_LETTERS for c in text)
    if ukrainian != russian:
        by_letters = "uk" if ukrainian > russian else "ru"
        if by_letters in offered:
            return by_letters
    # rank() only reads the model, so threads can share the one identifier.
    ranked = _identifier().rank(text)
    return next((code for code, _ in ranked if code in offered), None)
