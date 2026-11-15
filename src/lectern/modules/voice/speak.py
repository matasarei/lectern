"""Speech from text: a Piper voice read on the CPU by piper-tts, in this process.

Every other model runs in a runner process of its own (lectern.runners). A voice doesn't: it is
about 60 MB, loads in a fraction of a second and runs on the CPU, so a process, a port and a key
would be more code than they protect. It takes no GPU room, so it never makes the chat model
swap out on a small card, and it isn't counted in the runtime's budget.

One sentence is spoken at a time, on a worker thread, with two CPU threads, so a llama-server on
the CPU keeps the rest. At most `keep` voices stay loaded, least recently used first, and one
nobody has used for `idle_seconds` is let go.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import wave
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lectern.catalog.catalog import CatalogEntry, ModelFile

log = logging.getLogger(__name__)

THREADS = 2
KEEP = 2
IDLE_SECONDS = 600.0
MIN_SPEED = 0.5
MAX_SPEED = 2.0


class Speaker(Protocol):
    """A loaded voice."""

    def wav(self, text: str, speed: float) -> bytes:
        """The text spoken, as a 16-bit mono WAV file."""
        ...


Loader = Callable[[Path, Path, int | None], Speaker]


class VoiceError(Exception):
    """The voice couldn't be loaded, or couldn't speak the text."""


@dataclass
class PiperSpeaker:
    voice: object  # piper.PiperVoice
    speaker: int | None

    def wav(self, text: str, speed: float) -> bytes:
        from piper.config import SynthesisConfig

        # piper speaks slower with a larger length scale: the voice's own, divided by the speed.
        scale = self.voice.config.length_scale / speed
        out = io.BytesIO()
        with wave.open(out, "wb") as file:
            self.voice.synthesize_wav(
                text, file, SynthesisConfig(speaker_id=self.speaker, length_scale=scale)
            )
        return out.getvalue()


def load_piper(model: Path, config: Path, speaker: int | None) -> PiperSpeaker:
    """Load a Piper voice. piper-tts is imported here, not with the app: it brings
    onnxruntime, which only someone listening needs."""
    import onnxruntime
    from piper import PiperVoice
    from piper.config import PiperConfig

    with config.open(encoding="utf-8") as file:
        settings = PiperConfig.from_dict(json.load(file))
    # PiperVoice.load would give the session every core; this keeps it to THREADS.
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = THREADS
    options.inter_op_num_threads = 1
    session = onnxruntime.InferenceSession(
        str(model), sess_options=options, providers=["CPUExecutionProvider"]
    )
    # download_dir is where piper fetches extra data for Chinese, Japanese and Thai; no voice
    # here needs it, and it must never be the server's working directory.
    voice = PiperVoice(session=session, config=settings, download_dir=model.parent)
    return PiperSpeaker(voice, speaker)


class Voices:
    """The voices loaded now, and speaking with them one sentence at a time."""

    def __init__(
        self,
        file_path: Callable[[CatalogEntry, ModelFile], Path],
        load: Loader = load_piper,
        clock: Callable[[], float] = time.monotonic,
        keep: int = KEEP,
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        self._file_path = file_path  # DownloadManager.file_path: where a model's file is
        self._load = load
        self._clock = clock
        self._keep = keep
        self._idle_seconds = idle_seconds
        # "<id>@<pin>" → (the voice, when it was last used), least recently used first
        self._loaded: OrderedDict[str, tuple[Speaker, float]] = OrderedDict()
        self._one_at_a_time = asyncio.Lock()

    async def wav(self, entry: CatalogEntry, text: str, speed: float = 1.0) -> bytes:
        """The text spoken by the voice. Raises VoiceError when it can't be."""
        async with self._one_at_a_time:
            return await asyncio.to_thread(self._speak, entry, text, speed)

    def loaded(self) -> list[str]:
        """The keys of the voices loaded now, least recently used first."""
        return list(self._loaded)

    def drop_idle(self) -> None:
        """Let go of the voices nobody has used for `idle_seconds`."""
        now = self._clock()
        for key, (_, used) in list(self._loaded.items()):
            if now - used >= self._idle_seconds:
                del self._loaded[key]

    def _speak(self, entry: CatalogEntry, text: str, speed: float) -> bytes:
        self.drop_idle()
        speaker = self._voice(entry)
        try:
            return speaker.wav(text, min(max(speed, MIN_SPEED), MAX_SPEED))
        except Exception as exc:  # whatever piper or onnxruntime raised: the voice failed
            # The text is the person's: never in the log.
            log.warning("%s couldn't speak %d characters: %s", entry.id, len(text), exc)
            raise VoiceError(f"{entry.title} couldn't speak this text.") from exc

    def _voice(self, entry: CatalogEntry) -> Speaker:
        # Keyed by the pin as well, so an updated voice is loaded afresh.
        key = f"{entry.id}@{entry.pin}"
        if key in self._loaded:
            speaker, _ = self._loaded.pop(key)
        else:
            if entry.voice is None or entry.files.config is None:
                raise VoiceError(f"{entry.title} isn't a voice.")
            model = self._file_path(entry, entry.files.model[0])
            config = self._file_path(entry, entry.files.config)
            try:
                speaker = self._load(model, config, entry.voice.speaker)
            except Exception as exc:  # a missing or broken file, or onnxruntime refusing it
                log.warning("%s didn't load: %s", entry.id, exc)
                raise VoiceError(f"{entry.title} didn't load.") from exc
            while len(self._loaded) >= self._keep:
                self._loaded.popitem(last=False)
        self._loaded[key] = (speaker, self._clock())
        return speaker
