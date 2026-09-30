from __future__ import annotations

import random

import pytest

from lectern.dev.ocr_check import cer, distance, normalise, wer


def _plain_distance(a, b) -> int:  # noqa: ANN001
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, start=1):
        previous, row[0] = row[0], i
        for j, y in enumerate(b, start=1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (x != y))
    return row[-1]


def test_the_fast_distance_matches_the_plain_one():
    rng = random.Random(7)  # noqa: S311 — seeded test data, not security
    for _ in range(300):
        a = "".join(rng.choice("абвг ab") for _ in range(rng.randint(0, 90)))
        b = "".join(rng.choice("абвг ab") for _ in range(rng.randint(0, 90)))
        assert distance(a, b) == _plain_distance(a, b), (a, b)
    assert distance("kitten", "sitting") == 3
    assert distance(["one", "two"], ["one", "three", "two"]) == 1


def test_identical_text_scores_zero_and_one_wrong_character_in_100_scores_001():
    reference = ("Орендар сплачує пеню " * 5)[:99] + "."  # 100 characters, as normalised
    assert len(normalise(reference)) == 100
    wrong = reference[:50] + "x" + reference[51:]

    assert cer(reference, reference) == 0 and wer(reference, reference) == 0
    assert cer(wrong, reference) == pytest.approx(0.01)


def test_markup_the_models_add_is_not_an_error():
    reference = "3. Орендна плата\n\nберезень 1998 250,00\nквітень 1998 250,00"
    markdown = "## 3. **Орендна плата**\n\n| березень 1998 | 250,00 |\n|---|---|\n| квітень 1998 | 250,00 |"  # noqa: E501
    html_table = (
        "# 3. Орендна плата\n<table><tr><td>березень 1998</td><td>250,00</td></tr>"
        "<tr><td>квітень&nbsp;1998</td><td>250,00</td></tr></table>"
    )

    assert cer(markdown, reference) == 0
    assert cer(html_table, reference) == 0
    assert wer("  " + reference.replace(" ", "   "), reference) == 0


def test_composed_and_decomposed_letters_are_the_same():
    composed, decomposed = "Київ, й", "Київ, й"
    assert normalise(decomposed) == composed and cer(decomposed, composed) == 0


def test_an_empty_reference_counts_every_character_of_the_output():
    assert cer("", "") == 0
    assert cer("abc", "") == 3  # divided by 1, not by zero
    assert wer("", "two words") == 1
