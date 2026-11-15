"""Speaking with a voice: loading it once, keeping a few, letting idle ones go, one sentence at a
time, and failing without the text in the log."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import wave
from pathlib import Path

import pytest

from lectern.catalog import catalog
from lectern.catalog.catalog import CatalogEntry, ModelFile
from lectern.modules.voice import speak
from lectern.modules.voice.speak import VoiceError, Voices


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeSpeaker:
    def __init__(self, model: Path, fail: bool = False) -> None:
        self.model = model
        self.fail = fail
        self.spoken: list[tuple[str, float]] = []
        self.busy = 0
        self.overlapped = False

    def wav(self, text: str, speed: float) -> bytes:
        self.busy += 1
        self.overlapped |= self.busy > 1
        try:
            if self.fail:
                raise RuntimeError("onnxruntime refused")
            self.spoken.append((text, speed))
            out = io.BytesIO()
            with wave.open(out, "wb") as file:
                file.setnchannels(1)
                file.setsampwidth(2)
                file.setframerate(22050)
                file.writeframes(b"\0\0" * 220)
            return out.getvalue()
        finally:
            self.busy -= 1


class Loader:
    def __init__(self, fail_load: bool = False, fail_speak: bool = False) -> None:
        self.fail_load = fail_load
        self.fail_speak = fail_speak
        self.loads: list[tuple[Path, Path, int | None]] = []
        self.speakers: list[FakeSpeaker] = []

    def __call__(self, model: Path, config: Path, speaker: int | None) -> FakeSpeaker:
        if self.fail_load:
            raise FileNotFoundError(model)
        self.loads.append((model, config, speaker))
        self.speakers.append(FakeSpeaker(model, fail=self.fail_speak))
        return self.speakers[-1]


def _file_path(entry: CatalogEntry, file: ModelFile) -> Path:
    return Path("/models") / entry.id / file.path


def _voice(model_id: str) -> CatalogEntry:
    entry = catalog.load().get(model_id)
    assert entry is not None
    return entry


DENIS, JOE, LJSPEECH = "piper-ru-ru-denis", "piper-en-us-joe", "piper-en-us-ljspeech"


def test_a_voice_speaks_wav_from_its_files_and_loads_once():
    loader = Loader()
    voices = Voices(_file_path, load=loader, clock=Clock())

    first = asyncio.run(voices.wav(_voice(DENIS), "Привет.", 1.0))
    asyncio.run(voices.wav(_voice(DENIS), "Ещё раз.", 1.25))

    with wave.open(io.BytesIO(first)) as read:
        assert (read.getnchannels(), read.getsampwidth(), read.getframerate()) == (1, 2, 22050)
    model, config, speaker = loader.loads[0]
    assert len(loader.loads) == 1
    assert model == Path("/models/piper-ru-ru-denis/ru/ru_RU/denis/medium/ru_RU-denis-medium.onnx")
    assert config.name == "ru_RU-denis-medium.onnx.json" and speaker is None
    assert loader.speakers[0].spoken == [("Привет.", 1.0), ("Ещё раз.", 1.25)]


def test_the_speed_is_held_between_half_and_double():
    loader = Loader()
    voices = Voices(_file_path, load=loader, clock=Clock())
    asyncio.run(voices.wav(_voice(DENIS), "a", 0.1))
    asyncio.run(voices.wav(_voice(DENIS), "b", 9.0))
    assert [speed for _, speed in loader.speakers[0].spoken] == [0.5, 2.0]


def test_a_third_voice_lets_the_least_recently_used_go():
    loader = Loader()
    voices = Voices(_file_path, load=loader, clock=Clock(), keep=2)

    for model_id in (DENIS, JOE, DENIS, LJSPEECH):  # Denis used again, so Joe is the oldest
        asyncio.run(voices.wav(_voice(model_id), "x"))

    assert [key.split("@")[0] for key in voices.loaded()] == [DENIS, LJSPEECH]
    assert len(loader.loads) == 3  # Denis wasn't loaded twice


def test_a_voice_nobody_used_for_the_idle_time_is_let_go():
    clock = Clock()
    voices = Voices(_file_path, load=Loader(), clock=clock, idle_seconds=600)
    asyncio.run(voices.wav(_voice(DENIS), "x"))

    clock.now += 599
    voices.drop_idle()
    assert len(voices.loaded()) == 1

    clock.now += 1
    voices.drop_idle()
    assert voices.loaded() == []


def test_two_sentences_at_once_are_spoken_one_after_the_other():
    loader = Loader()
    voices = Voices(_file_path, load=loader, clock=Clock())

    async def both() -> None:
        await asyncio.gather(*(voices.wav(_voice(DENIS), f"{n}") for n in range(4)))

    asyncio.run(both())
    assert not loader.speakers[0].overlapped
    assert sorted(text for text, _ in loader.speakers[0].spoken) == ["0", "1", "2", "3"]


def test_a_voice_that_does_not_load_fails_with_its_title():
    voices = Voices(_file_path, load=Loader(fail_load=True), clock=Clock())
    with pytest.raises(VoiceError, match="Piper Denis .* didn't load"):
        asyncio.run(voices.wav(_voice(DENIS), "секретный текст"))
    assert voices.loaded() == []


def test_a_failed_sentence_is_logged_without_its_text(caplog):
    voices = Voices(_file_path, load=Loader(fail_speak=True), clock=Clock())
    with (
        caplog.at_level(logging.WARNING, logger=speak.__name__),
        pytest.raises(VoiceError, match="couldn't speak"),
    ):
        asyncio.run(voices.wav(_voice(DENIS), "секретный текст"))
    assert "15 characters" in caplog.text
    assert "секретный" not in caplog.text


@pytest.mark.voice
@pytest.mark.skipif(
    not os.environ.get("LECTERN_TEST_VOICE"),
    reason="set LECTERN_TEST_VOICE to a Piper voice's .onnx, with its .onnx.json beside it",
)
def test_a_real_piper_voice_speaks():
    model = Path(os.environ["LECTERN_TEST_VOICE"])
    speaker = speak.load_piper(model, Path(f"{model}.json"), None)

    normal = speaker.wav("One two three four five.", 1.0)
    faster = speaker.wav("One two three four five.", 2.0)

    def seconds(data: bytes) -> float:
        with wave.open(io.BytesIO(data)) as read:
            assert read.getsampwidth() == 2 and read.getnchannels() == 1
            return read.getnframes() / read.getframerate()

    assert seconds(normal) > 0.5
    assert seconds(faster) < seconds(normal) * 0.75
