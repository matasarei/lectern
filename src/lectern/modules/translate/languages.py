"""The languages an admin added, and which model translates a request.

A language added on the Languages page has a model of its own, picked there. Translating to or
from it uses that model, as long as it also translates the other language; anything else is
translated by the chat model (`choose`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from importlib import resources

import yaml
from sqlalchemy.orm import Session, sessionmaker

from lectern.catalog import catalog
from lectern.catalog.catalog import TRANSLATE_ROLE, CatalogEntry
from lectern.catalog.languages import name
from lectern.core import settings
from lectern.hardware.tiers import Tier
from lectern.runtime.supervisor import NotAvailable
from lectern.runtime.wiring import pick

Added = tuple[str, str, str | None]  # (code, model, previous), as ToolEnv carries them


def added_languages(sessions: sessionmaker[Session]) -> tuple[Added, ...]:
    """The languages added, from the setting: each with its model and the one before it."""
    stored = settings.get(sessions, settings.LANGUAGES_KEY, settings.LANGUAGE_DEFAULTS)["added"]
    return tuple(
        (code, value["model"], value.get("previous"))
        for code, value in sorted(stored.items())
        if isinstance(value, dict) and isinstance(value.get("model"), str)
    )


def language_model(added: Added, on_disk: Callable[[CatalogEntry], bool]) -> CatalogEntry | None:
    """The model a language translates with now: its own once it is on disk, until then the
    one it had before; None when neither is (still downloading, or the download failed) or the
    catalog no longer lists them."""
    _code, model_id, previous = added
    models = catalog.load()
    for candidate in (model_id, previous):
        entry = models.get(candidate) if candidate else None
        if entry is not None and entry.translate is not None and on_disk(entry):
            return entry
    return None


@dataclass(frozen=True)
class Choice:
    """The model a translation runs on: a language's own, or None for the chat model, with why
    the chat model translates when an added language's model can't."""

    entry: CatalogEntry | None
    why: str | None = None
    # An added language's model isn't on disk yet: it will translate once it is.
    waiting: bool = False


def choose(
    source: str,
    target: str,
    added: tuple[Added, ...],
    on_disk: Callable[[CatalogEntry], bool],
) -> Choice:
    """The target's model when it translates the source too, else the source's model when it
    translates the target, else the chat model. The target's goes first: what's read is the
    text it writes."""
    by_code = {item[0]: item for item in added}
    reasons: list[str] = []
    waiting = False
    for code, other in ((target, source), (source, target)):
        item = by_code.get(code)
        if item is None:
            continue
        entry = language_model(item, on_disk)
        if entry is None:
            waiting = True
            reasons.append(f"the {name(code)} model isn't downloaded yet")
        elif not entry.translate.covers(other):
            reasons.append(f"{entry.title} doesn't translate {name(other)}")
        else:
            return Choice(entry)
    return Choice(None, "; ".join(reasons) or None, waiting)


# --- what a language can be given ---------------------------------------------------------

# In lectern.catalog, written by `lectern dev translate-check`.
SCORES_FILE = "translation_scores.yaml"
# How much better than the chat model a translation model must score to be offered: chrF on 50
# sentences moves by about a point between models this close, and on a 4 GB card most measured
# differences were under one (docs/acceptance/translate.md).
MARGIN = 1.0


@cache
def scores() -> dict[str, dict[str, float]]:
    """How well each measured model translates each language, by model id then language: the
    mean chrF of translating it into and out of English (catalog/translation_scores.yaml).
    Translation models and chat models alike: a chat model's is what a translation model has to
    beat. Empty when nothing was measured."""
    try:
        text = resources.files("lectern.catalog").joinpath(SCORES_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    measured = (yaml.safe_load(text) or {}).get("scores") or {}
    return {
        model_id: {code: (pair["into"] + pair["from"]) / 2 for code, pair in languages.items()}
        for model_id, languages in measured.items()
    }


def chat_model(runtime) -> CatalogEntry | None:  # noqa: ANN001
    """The chat model this computer translates with when a language has no model of its own:
    the one chosen for the chat, else the tier's."""
    chosen = getattr(runtime, "chosen_models", None)
    try:
        return pick("main", runtime.tier, chosen() if chosen else None)[1]
    except NotAvailable:
        return None


@dataclass(frozen=True)
class Option:
    """A model a language can be given on this computer, and why one might pick it."""

    entry: CatalogEntry
    score: float  # chrF, into and out of English, from the scores file
    best: bool  # the best results of the options
    open_licence: bool  # the best of the permissively licensed ones


def covered() -> list[str]:
    """Every language some translation model in the catalog translates, in code order."""
    codes = {
        code
        for entry in catalog.load().choices_for(TRANSLATE_ROLE)
        for code in entry.translate.languages
    }
    return sorted(codes)


def chat_score(code: str, chat: CatalogEntry | None) -> float | None:
    """How well the chat model translates `code`; None when that wasn't measured."""
    return scores().get(chat.id, {}).get(code) if chat is not None else None


def options(code: str, tier: Tier, chat: CatalogEntry | None) -> list[Option]:
    """The translation models that translate `code`, suit this computer's tier, and were
    measured translating it better than the chat model, by MARGIN at least: the best first,
    each tagged the best of them, and the best with an open licence. A model nobody measured
    isn't offered, and against a chat model nobody measured (the comfortable tier's, or one
    chosen outside the defaults) none is: there's nothing to show it would help."""
    measured = scores()
    baseline = chat_score(code, chat)
    found = [
        (entry, measured[entry.id][code])
        for entry in catalog.load().choices_for(TRANSLATE_ROLE)
        if entry.translate.covers(code) and entry.suits(tier) and code in measured.get(entry.id, {})
    ]
    better = sorted(
        ((e, score) for e, score in found if baseline is not None and score >= baseline + MARGIN),
        key=lambda pair: -pair[1],
    )
    best = better[0][0].id if better else None
    open_licence = next((e.id for e, _ in better if e.permissive), None)
    return [
        Option(e, score, best=e.id == best, open_licence=e.id == open_licence)
        for e, score in better
    ]
