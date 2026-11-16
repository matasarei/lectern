"""Which language a reply is in, among the languages some voice speaks."""

from __future__ import annotations

import pytest

from lectern.modules.voice.language import detect

EVERY = ["en", "ru", "uk", "de", "pl", "fr", "es", "it", "cs", "nl"]


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("The meeting is moved to Friday; please bring the signed lease.", "en"),
        ("Встреча перенесена на пятницу, возьмите подписанный договор.", "ru"),
        ("Зустріч перенесено на п'ятницю, візьміть підписаний договір.", "uk"),
        ("Das Treffen wurde auf Freitag verschoben, bitte bringen Sie den Vertrag mit.", "de"),
        ("Spotkanie przeniesiono na piątek, proszę zabrać podpisaną umowę.", "pl"),
        ("La réunion est reportée à vendredi, apportez le bail signé.", "fr"),
    ],
)
def test_a_reply_is_found_in_its_language(text, language):
    assert detect(text, EVERY) == language


def test_ukrainian_and_russian_letters_decide_between_the_two():
    # Short and mostly letters both share: the statistics alone could go either way.
    assert detect("Так, їх три.", EVERY) == "uk"
    assert detect("Да, их три. Объём.", EVERY) == "ru"


def test_the_letters_decide_only_among_the_languages_offered():
    assert detect("Привіт, як справи? Це тест.", ["en", "ru"]) == "ru"


def test_only_an_offered_language_is_returned():
    assert detect("Das Treffen wurde auf Freitag verschoben.", ["en", "ru"]) in {"en", "ru"}


@pytest.mark.parametrize("text", ["", "  ", "42 + 17 = 59", "ok"])
def test_text_with_too_few_letters_has_no_language(text):
    assert detect(text, EVERY) is None


def test_no_language_offered_means_none():
    assert detect("The meeting is moved to Friday.", []) is None


@pytest.mark.parametrize(
    "text",
    [
        "Договорът изтича на трети май, а наемът се плаща всеки месец.",
        "Българският език е официален език.",
    ],
)
def test_bulgarian_isnt_taken_for_russian_for_its_hard_sign(text):
    # "ъ" is one of Bulgarian's commonest letters and rare in Russian: it can't settle Russian.
    assert detect(text, [*EVERY, "bg"]) == "bg"
