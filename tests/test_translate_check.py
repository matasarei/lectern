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


def answer_looping() -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": "la la"}, "finish_reason": "length"}]}
    ).encode()


def test_a_sentence_that_loops_counts_as_not_translated_and_the_run_goes_on():
    sentences = {"en": ["One.", "Two."], "uk": ["Один.", "Два."]}
    replies = [answer("Один."), answer_looping(), answer("One."), answer("Two.")]
    runtime = make_runtime(FakeHandle(replies), per_role=True)
    lines: list[str] = []
    found = asyncio.run(
        check.measure(runtime, check.candidates(["salamandrata-2b"]), sentences, echo=lines.append)
    )
    assert found["salamandrata-2b"]["uk"]["from"] == 100.0
    assert found["salamandrata-2b"]["uk"]["into"] < 100.0
    assert "salamandrata-2b uk: 1 sentences looped" in lines


def test_a_measured_pair_is_skipped_and_each_new_one_recorded_at_once(tmp_path):
    sentences = {"en": ["One."], "uk": ["Один."], "de": ["Eins."]}
    handle = FakeHandle([answer("Eins."), answer("One.")])
    runtime = make_runtime(handle, per_role=True)
    recorded: list[tuple[str, str]] = []
    asyncio.run(
        check.measure(
            runtime, check.candidates(["salamandrata-2b"]), sentences, echo=print,
            measured=lambda model_id, code: code == "uk",
            record=lambda model_id, code, pair: recorded.append((model_id, code)),
        )
    )  # fmt: skip
    assert recorded == [("salamandrata-2b", "de")]
    assert len(handle.requests) == 2  # Ukrainian wasn't asked again


def test_the_pairs_a_scores_file_has(tmp_path):
    path = tmp_path / "scores.yaml"
    assert check.already(path) == set()
    check.merge(path, {"salamandrata-2b": {"uk": {"into": 1.0, "from": 2.0}}}, 10)
    assert check.already(path) == {("salamandrata-2b", "uk")}
