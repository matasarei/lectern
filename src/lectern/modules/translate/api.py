"""The Languages page's API: the languages a translation model can be added for, the ones added,
and adding, changing and removing one. Seen by everyone; changed only by an admin, since adding a
language downloads a model, as Models & memory's downloads are an admin's."""

from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from lectern.catalog import catalog
from lectern.catalog.catalog import CatalogEntry
from lectern.catalog.languages import LANGUAGE_NAMES
from lectern.core import settings
from lectern.core.auth import require_admin
from lectern.hardware.probe import probe
from lectern.hardware.tiers import effective_tier
from lectern.modules.translate.languages import (
    Option,
    added_languages,
    chat_model,
    chat_score,
    covered,
    language_model,
    options,
)

router = APIRouter()

# A change reads the added languages, changes one, and writes them all back: two at once
# would each write back the other's old list.
_changing = threading.Lock()


class LanguageChoice(BaseModel):
    model_id: str
    accept_licence: bool = False


def _option(option: Option, request: Request) -> dict:
    entry = option.entry
    downloads = request.app.state.downloads
    return {
        "id": entry.id,
        "title": entry.title,
        "size": entry.size,
        "licence": entry.licence,
        "licence_notice": entry.licence_notice,
        "notes": entry.notes,
        "score": round(option.score, 1),  # chrF, into and out of English
        "best": option.best,
        "open_licence": option.open_licence,
        "download": downloads.state(entry).to_dict(),
    }


@router.get("/languages")
def list_languages(request: Request) -> dict:
    tier = effective_tier(probe())
    chat = chat_model(request.app.state.runtime)
    added = {
        code: (model, previous) for code, model, previous in added_languages(_sessions(request))
    }
    languages = []
    for code in covered():
        offered = options(code, tier, chat)
        measured = chat_score(code, chat)
        model, previous = added.get(code, (None, None))
        languages.append(
            {
                "code": code,
                # None: not added. `previous` translates until `model` is on disk.
                "added": {"model": model, "previous": previous} if model else None,
                "options": [_option(o, request) for o in offered],
                # How well the chat model translates it, which an option beats; None: not
                # measured.
                "chat_score": round(measured, 1) if measured is not None else None,
            }
        )
    return {"tier": tier.value, "chat_model": chat.title if chat else None, "languages": languages}


@router.put("/languages/{code}", dependencies=[Depends(require_admin)])
async def add_language(code: str, body: LanguageChoice, request: Request) -> dict:
    """Add a language with the model picked for it, or give an added one another model, and
    start that model's download. The one it had translates until the new one is on disk."""
    entry = _offered(code, body.model_id, chat_model(request.app.state.runtime))
    if entry.licence_notice and not body.accept_licence:
        raise HTTPException(
            409,
            f"{entry.title} is under {entry.licence}. Read the licence, then add it again with "
            "accept_licence set to true.",
        )
    await run_in_threadpool(_choose, request, code, entry.id)
    request.app.state.downloads.start(entry)
    return await run_in_threadpool(list_languages, request)


@router.delete("/languages/{code}", dependencies=[Depends(require_admin)])
async def remove_language(code: str, request: Request) -> dict:
    """Take a language off the list. Its model stays on disk: Models & memory removes it."""
    if not await run_in_threadpool(_remove, request, code):
        raise HTTPException(404, f"{_named(code)} isn't added.")
    return await run_in_threadpool(list_languages, request)


def _sessions(request: Request):  # noqa: ANN202
    return request.app.state.sessions


def _named(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


def _offered(code: str, model_id: str, chat: CatalogEntry | None) -> CatalogEntry:
    if code not in covered():
        raise HTTPException(404, f"No translation model translates {code!r}.")
    entry = catalog.load().get(model_id)
    if entry is None:
        raise HTTPException(404, f"No model {model_id!r} in the catalog.")
    if entry.id not in {o.entry.id for o in options(code, effective_tier(probe()), chat)}:
        raise HTTPException(409, f"{entry.title} isn't one of the models for {_named(code)} here.")
    return entry


def _choose(request: Request, code: str, model_id: str) -> None:
    sessions = _sessions(request)
    with _changing:
        stored = settings.get(sessions, settings.LANGUAGES_KEY, settings.LANGUAGE_DEFAULTS)
        added = dict(stored["added"])
        before = added.get(code) if isinstance(added.get(code), dict) else None
        if before is not None and before.get("model") == model_id:
            return  # the same model again: its download is (re)started, nothing else changes
        # The model translating now goes on until this one is downloaded: the one it had, or,
        # when that hadn't finished downloading either, the one before.
        in_use = (
            language_model(
                (code, before.get("model"), before.get("previous")),
                request.app.state.downloads.is_downloaded,
            )
            if before is not None
            else None
        )
        previous = in_use.id if in_use is not None and in_use.id != model_id else None
        added[code] = {"model": model_id, "previous": previous}
        settings.put(sessions, settings.LANGUAGES_KEY, {**stored, "added": added})


def _remove(request: Request, code: str) -> bool:
    sessions = _sessions(request)
    with _changing:
        stored = settings.get(sessions, settings.LANGUAGES_KEY, settings.LANGUAGE_DEFAULTS)
        added = dict(stored["added"])
        if added.pop(code, None) is None:
            return False
        settings.put(sessions, settings.LANGUAGES_KEY, {**stored, "added": added})
        return True
