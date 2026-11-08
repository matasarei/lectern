"""The languages an admin added, and which model translates a request.

A language added on the Languages page has a model of its own, picked there. Translating to or
from it uses that model, as long as it also translates the other language; anything else is
translated by the chat model (`choose`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from lectern.catalog import catalog
from lectern.catalog.catalog import CatalogEntry
from lectern.catalog.languages import name
from lectern.core import settings

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
    for code, other in ((target, source), (source, target)):
        item = by_code.get(code)
        if item is None:
            continue
        entry = language_model(item, on_disk)
        if entry is None:
            reasons.append(f"the {name(code)} model isn't downloaded yet")
        elif not entry.translate.covers(other):
            reasons.append(f"{entry.title} doesn't translate {name(other)}")
        else:
            return Choice(entry)
    return Choice(None, "; ".join(reasons) or None)
