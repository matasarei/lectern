from __future__ import annotations

from lectern.modules.ocr.doubts import mark_unsure, ocr_doubts, unsure_words


def test_unsure_tokens_are_grouped_into_the_words_they_belong_to():
    tokens = [
        ("Оп", -0.01), ("лата", -0.02), (" до", -0.1), (" 1", -0.05), ("5", -4.4),
        (" числа", -0.01), (",", -0.01), (" Хр", -0.01), ("е", -3.7), ("щатик", -0.02), (".", -0.3),
    ]  # fmt: skip
    assert unsure_words(tokens) == ["15", "Хрещатик"]  # the punctuation stays out
    assert unsure_words(tokens, threshold=-5.0) == []


def test_a_reading_without_logprobs_has_no_unsure_words():
    assert unsure_words(None) == [] and unsure_words([]) == []


def test_unsure_words_are_marked_for_cleanup_once_each():
    marked = mark_unsure("Оплата до 15 числа, вул. Хрещатик, 26.", ["15", "Хрещатик"])
    assert marked == "Оплата до ⟨15?⟩ числа, вул. ⟨Хрещатик?⟩, 26."


def test_an_unsure_word_still_in_the_text_becomes_a_doubt_and_a_changed_one_doesnt():
    ids = iter(["d3.2", "d3.3", "d3.4"])
    text, doubts = ocr_doubts(
        "Оплата до 15 числа {d3.1}, вул. Хрещатик.", ["15", "Хрещатик", "2?0"], lambda: next(ids)
    )
    assert text == "Оплата до {d3.2} числа {d3.1}, вул. {d3.3}."
    assert [(d.id, d.raw, d.source) for d in doubts] == [
        ("d3.2", "15", "ocr"),
        ("d3.3", "Хрещатик", "ocr"),
    ]
    assert "wasn't sure" in doubts[0].reason
    whole = ocr_doubts("не 150, а 15", ["15"], lambda: "d1.1")[0]
    assert whole == "не 150, а {d1.1}"  # whole words only
    assert ocr_doubts("{d3.15} та 15", ["15"], lambda: "d3.16")[0] == "{d3.15} та {d3.16}"
