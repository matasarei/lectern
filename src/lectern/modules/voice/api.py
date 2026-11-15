"""Reading aloud over HTTP: the languages a voice speaks and the voice each one uses, picking a
language's voice (an admin's: it downloads a model), the language of a reply, and one sentence
spoken as WAV.

The text sent to be spoken is the person's reply: it is never logged.
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from lectern.catalog import catalog
from lectern.catalog.catalog import CatalogEntry
from lectern.catalog.languages import LANGUAGE_NAMES
from lectern.core import settings
from lectern.core.auth import require_admin
from lectern.modules.voice.language import detect
from lectern.modules.voice.speak import MAX_SPEED, MIN_SPEED, VoiceError, Voices

router = APIRouter()

MAX_SENTENCE = 1_000  # characters spoken in one request: the UI sends one sentence at a time
MAX_REPLY = 20_000  # characters of a reply looked at to find its language

# A choice reads the chosen voices, changes one and writes them all back: two at once would
# each write back the other's old list.
_choosing = threading.Lock()


class VoiceChoice(BaseModel):
    model_id: str
    accept_licence: bool = False


class Reply(BaseModel):
    text: str = Field(max_length=MAX_REPLY)


class Sentence(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_SENTENCE)
    language: str
    speed: float = Field(default=1.0, ge=MIN_SPEED, le=MAX_SPEED)


@router.get("/voice")
def list_voices(request: Request) -> dict:
    """Every language some voice speaks, the voice it uses and the ones it could."""
    models = catalog.load()
    chosen = _chosen(request)
    return {
        "languages": [
            {
                "code": code,
                "voice": voice_of(code, chosen).id,
                "chosen": chosen.get(code),  # None: the language's default
                "voices": [_option(entry, request) for entry in models.voices_for(code)],
            }
            for code in models.voice_languages()
        ]
    }


@router.put("/voice/{code}", dependencies=[Depends(require_admin)])
async def choose_voice(code: str, body: VoiceChoice, request: Request) -> dict:
    """Give a language this voice, and download it if it isn't on disk yet."""
    entry = catalog.load().get(body.model_id)
    if code not in catalog.load().voice_languages():
        raise HTTPException(404, f"No voice speaks {code!r}.")
    if entry is None or entry.voice is None or entry.voice.language != code:
        raise HTTPException(409, f"{body.model_id!r} isn't a voice for {_named(code)}.")
    if entry.licence_notice and not body.accept_licence:
        raise HTTPException(
            409,
            f"{entry.title} is under {entry.licence}. Read the licence, then choose it again "
            "with accept_licence set to true.",
        )
    await run_in_threadpool(_choose, request, code, entry)
    downloads = request.app.state.downloads
    if not downloads.is_downloaded(entry):
        downloads.start(entry)
    return await run_in_threadpool(list_voices, request)


@router.post("/voice/language")
async def reply_language(body: Reply) -> dict:
    """The language of a reply, among the ones a voice speaks; None when it can't be told."""
    offered = catalog.load().voice_languages()
    return {"language": await run_in_threadpool(detect, body.text, offered)}


@router.post("/voice/speak")
async def speak(body: Sentence, request: Request) -> Response:
    """One sentence spoken by the language's voice, as a 16-bit mono WAV."""
    if body.language not in catalog.load().voice_languages():
        raise HTTPException(422, f"No voice speaks {body.language!r}.")
    entry = voice_of(body.language, await run_in_threadpool(_chosen, request))
    if not request.app.state.downloads.is_downloaded(entry):
        raise HTTPException(409, f"The {_named(body.language)} voice isn't on this computer yet.")
    try:
        wav = await _voices(request).wav(entry, body.text, body.speed)
    except VoiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


def voice_of(code: str, chosen: dict[str, str]) -> CatalogEntry:
    """The voice a language speaks with: the one an admin picked while the catalog still has
    it, else its default, else its first."""
    voices = catalog.load().voices_for(code)
    picked = next((v for v in voices if v.id == chosen.get(code)), None)
    return picked or catalog.load().default_voice(code) or voices[0]


def _option(entry: CatalogEntry, request: Request) -> dict:
    downloads = request.app.state.downloads
    return {
        "id": entry.id,
        "title": entry.title,
        "quant": entry.quant,
        "size": entry.size,
        "licence": entry.licence,
        "licence_notice": entry.licence_notice,
        "notes": entry.notes,  # what a restricted licence limits, in a sentence
        "default": bool(entry.voice and entry.voice.default),
        "installed": downloads.is_downloaded(entry),
        "download": downloads.state(entry).to_dict(),
    }


def _voices(request: Request) -> Voices:
    """The app's voices, made on the first sentence: there's no await between looking and
    setting, so two first requests can't make two."""
    voices = getattr(request.app.state, "voices", None)
    if voices is None:
        voices = Voices(request.app.state.downloads.file_path)
        request.app.state.voices = voices
    return voices


def _chosen(request: Request) -> dict[str, str]:
    stored = settings.get(request.app.state.sessions, settings.VOICE_KEY, settings.VOICE_DEFAULTS)
    return dict(stored["chosen"])


def _choose(request: Request, code: str, entry: CatalogEntry) -> None:
    sessions = request.app.state.sessions
    with _choosing:
        stored = settings.get(sessions, settings.VOICE_KEY, settings.VOICE_DEFAULTS)
        chosen = dict(stored["chosen"])
        # The default stores no choice, so a later Lectern's better default reaches it.
        if entry.voice is not None and entry.voice.default:
            chosen.pop(code, None)
        else:
            chosen[code] = entry.id
        settings.put(sessions, settings.VOICE_KEY, {**stored, "chosen": chosen})


def _named(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)
