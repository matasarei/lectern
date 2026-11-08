"""Which models a language is offered: measured, fitting this computer, and better than the
chat model."""

from __future__ import annotations

import textwrap

import pytest

from lectern.catalog import catalog
from lectern.hardware.tiers import Tier
from lectern.modules.translate import languages
from lectern.modules.translate.languages import chat_score, options

SALAMANDRA = "salamandrata-2b"


@pytest.fixture(autouse=True)
def measured():
    """The real scores loader here: each test sets its own table, or reads the file."""


@pytest.fixture
def chat():
    return catalog.load().get("qwen3.5-4b")


def scored(monkeypatch, table: dict[str, dict[str, float]]) -> None:
    monkeypatch.setattr(languages, "scores", lambda: table)


def test_a_model_a_point_better_than_the_chat_model_is_offered(monkeypatch, chat):
    scored(monkeypatch, {SALAMANDRA: {"uk": 52.0}, "qwen3.5-4b": {"uk": 51.0}})
    assert [o.entry.id for o in options("uk", Tier.MINIMUM, chat)] == [SALAMANDRA]


def test_a_model_measured_better_than_the_chat_model_is_offered(monkeypatch, chat):
    scored(monkeypatch, {SALAMANDRA: {"uk": 58.0}, "qwen3.5-4b": {"uk": 51.0}})
    [option] = options("uk", Tier.MINIMUM, chat)
    assert (option.entry.id, option.score, option.best, option.open_licence) == (
        SALAMANDRA,
        58.0,
        True,
        True,
    )
    assert chat_score("uk", chat) == 51.0


@pytest.mark.parametrize("translator", [51.0, 49.5, 51.9])
def test_a_model_no_better_than_the_chat_model_by_a_point_is_not(monkeypatch, chat, translator):
    scored(monkeypatch, {SALAMANDRA: {"uk": translator}, "qwen3.5-4b": {"uk": 51.0}})
    assert options("uk", Tier.MINIMUM, chat) == []


def test_a_model_nobody_measured_is_not_offered(monkeypatch, chat):
    scored(monkeypatch, {SALAMANDRA: {"de": 60.0}, "qwen3.5-4b": {"uk": 40.0}})
    assert options("uk", Tier.MINIMUM, chat) == []


def test_against_a_chat_model_nobody_measured_nothing_is_offered(monkeypatch):
    scored(monkeypatch, {SALAMANDRA: {"uk": 60.0}, "qwen3.5-4b": {"uk": 40.0}})
    big = catalog.load().get("qwen3.5-35b-a3b")
    assert options("uk", Tier.MINIMUM, big) == []
    assert chat_score("uk", big) is None
    assert options("uk", Tier.MINIMUM, None) == []  # no chat model at all


def test_the_shipped_scores_file_reads_as_means_and_its_models_are_in_the_catalog():
    languages.scores.cache_clear()
    table = languages.scores()
    known = {m.id for m in catalog.load().models}
    assert set(table) <= known
    for model_scores in table.values():
        assert all(0 <= score <= 100 for score in model_scores.values())


def test_the_scores_file_is_the_mean_into_and_out_of_english(monkeypatch, tmp_path):
    text = textwrap.dedent(
        """\
        measured: 2026-11-08
        scores:
          salamandrata-2b:
            uk: {into: 50.0, from: 60.0}
        """
    )

    class Files:
        def joinpath(self, name):  # noqa: ANN001, ANN202
            path = tmp_path / name
            path.write_text(text, encoding="utf-8")
            return path

    monkeypatch.setattr(languages.resources, "files", lambda package: Files())
    languages.scores.cache_clear()
    try:
        assert languages.scores() == {SALAMANDRA: {"uk": 55.0}}
    finally:
        languages.scores.cache_clear()


def test_no_scores_file_means_nothing_is_offered(monkeypatch, tmp_path):
    class Files:
        def joinpath(self, name):  # noqa: ANN001, ANN202
            return tmp_path / name  # not there

    monkeypatch.setattr(languages.resources, "files", lambda package: Files())
    languages.scores.cache_clear()
    try:
        assert languages.scores() == {}
    finally:
        languages.scores.cache_clear()
