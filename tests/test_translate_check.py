"""`lectern dev translate-check`: aligned sentences in, chrF per model and language out, merged
into the scores file."""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml

from lectern.dev import translate_check as check

from .agent.fake_model import FakeHandle, make_runtime


def write(folder, **files: str) -> None:  # noqa: ANN001
    for code, text in files.items():
        (folder / f"{code}.txt").write_text(text, encoding="utf-8")


def test_sentences_are_read_aligned_and_cut_to_the_limit(tmp_path):
    write(tmp_path, en="One.\nTwo.\nThree.", uk="Один.\nДва.\nТри.")
    assert check.load_sentences(tmp_path, 2) == {"en": ["One.", "Two."], "uk": ["Один.", "Два."]}


@pytest.mark.parametrize(
    ("files", "why"),
    [
        ({"uk": "Один."}, "has no en.txt"),
        ({"en": "One.", "xx": "Ein."}, "isn't a language code"),
        ({"en": "One.\nTwo.", "uk": "Один."}, "Not as many lines as en.txt"),
    ],
)
def test_a_folder_that_cant_be_measured_says_why(tmp_path, files, why):
    write(tmp_path, **files)
    with pytest.raises(ValueError, match=why):
        check.load_sentences(tmp_path, 10)


def test_by_default_every_translation_model_and_every_tiers_chat_model_is_measured():
    keys = [c.key for c in check.candidates(None)]
    assert "translate@salamandrata-2b" in keys
    assert {"main@qwen3.5-4b", "main@qwen3.5-9b", "main@qwen3.5-35b-a3b"} <= set(keys)
    assert not any("mlx" in key for key in keys)


@pytest.mark.parametrize(
    ("model_id", "why"),
    [("gone", "No model"), ("qwen3.5-4b-mlx", "No model"), ("paddleocr-vl-1.6", "neither")],
)
def test_only_a_catalog_model_that_translates_or_chats_can_be_named(model_id, why):
    with pytest.raises(ValueError, match=why):
        check.candidates([model_id])


def answer(text: str) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    ).encode()


def test_each_model_translates_into_and_out_of_english_and_is_scored():
    sentences = {"en": ["The cat sat."], "uk": ["Кіт сидів."], "ja": ["猫が座った。"]}
    # Salamandra doesn't translate Japanese: two requests for Ukrainian, then the chat model's
    # four, Ukrainian then Japanese.
    replies = [
        "Кіт сидів.",
        "The cat sat.",
        "Кіт сидів.",
        "A cat was sitting.",
        "猫",
        "The cat sat.",
    ]
    handle = FakeHandle([answer(r) for r in replies])
    runtime = make_runtime(handle, per_role=True)
    models = check.candidates(["salamandrata-2b", "qwen3.5-4b"])
    lines: list[str] = []

    found = asyncio.run(check.measure(runtime, models, sentences, echo=lines.append))

    assert found["salamandrata-2b"] == {"uk": {"into": 100.0, "from": 100.0}}
    assert set(found["qwen3.5-4b"]) == {"uk", "ja"}
    assert found["qwen3.5-4b"]["uk"]["into"] == 100.0
    assert found["qwen3.5-4b"]["uk"]["from"] < 100.0
    assert len(handle.requests) == 6
    assert handle.requests[0]["messages"][0]["content"].startswith(
        "Translate the following text from English into Ukrainian."
    )
    assert handle.requests[2]["messages"][0]["role"] == "system"  # the chat model's prompt
    assert len(lines) == 3


def test_measuring_some_languages_only():
    sentences = {"en": ["One."], "uk": ["Один."], "de": ["Eins."]}
    handle = FakeHandle([answer("Eins."), answer("One.")])
    runtime = make_runtime(handle, per_role=True)
    found = asyncio.run(
        check.measure(runtime, check.candidates(["salamandrata-2b"]), sentences, ["de"], echo=print)
    )
    assert list(found["salamandrata-2b"]) == ["de"]


def test_new_scores_are_merged_with_the_ones_not_measured_again(tmp_path):
    path = tmp_path / "scores.yaml"
    check.merge(path, {"salamandrata-2b": {"uk": {"into": 50.0, "from": 60.0}}}, 100)
    check.merge(path, {"salamandrata-2b": {"de": {"into": 55.0, "from": 58.0}}}, 100)
    check.merge(path, {"qwen3.5-4b": {"uk": {"into": 45.0, "from": 57.0}}}, 100)
    again = {"salamandrata-2b": {"uk": {"into": 51.0, "from": 60.0}}}
    check.merge(path, again, 100)
    check.merge(path, again, 100)  # safe to run twice: the same file

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# How well each model translates each language")
    body = yaml.safe_load(text)
    assert body["sentences"] == 100
    assert body["scores"] == {
        "qwen3.5-4b": {"uk": {"into": 45.0, "from": 57.0}},
        "salamandrata-2b": {
            "de": {"into": 55.0, "from": 58.0},
            "uk": {"into": 51.0, "from": 60.0},
        },
    }
