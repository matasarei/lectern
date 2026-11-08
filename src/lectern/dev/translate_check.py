"""`lectern dev translate-check`: how well each model translates each language, measured, so the
Languages page offers a translation model only where it beats the chat model.

It reads a folder of aligned sentence files, one sentence per line: `en.txt` and `<code>.txt`
for each language to measure (FLORES-200's devtest is the intended source; nothing is
downloaded here). Each model translates the first N sentences into and out of English, with
the requests the `translate` tool itself sends, and is scored with chrF (sacrebleu) against
the other side. Translation models are asked through their own role (`translate@<id>`), chat
models through the chat's (`main@<id>`), with the chat model's prompt.

The scores go into catalog/translation_scores.yaml as each model and language is measured,
merged with what's there: a run measures some models and languages and leaves the others'
scores as they were, and one stopped halfway keeps what it measured. A pair already in the
file is skipped unless asked to measure it again (at temperature 0 the same scores come out).
A sentence a model translates without ending (it repeats itself until it runs out of room)
counts as not translated, and the run says how many there were.
"""

from __future__ import annotations

import datetime
import platform
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from lectern.catalog import catalog
from lectern.catalog.catalog import TRANSLATE_ROLE, CatalogEntry
from lectern.catalog.languages import LANGUAGE_NAMES
from lectern.hardware.tiers import Tier
from lectern.modules.translate import tools
from lectern.runtime.supervisor import Priority

ENGLISH = "en"
SCORES_PATH = Path(__file__).resolve().parents[1] / "catalog" / "translation_scores.yaml"


@dataclass(frozen=True)
class Candidate:
    entry: CatalogEntry
    key: str  # the lease: "translate@<id>" or "main@<id>"
    translator: CatalogEntry | None  # the tool's prompt: the model's own, or None (the chat's)

    def languages(self, available: list[str]) -> list[str]:
        """The languages to measure it on: all of them for a chat model, its own for a
        translation model."""
        if self.translator is None:
            return available
        return [code for code in available if self.translator.translate.covers(code, ENGLISH)]


def candidates(model_ids: list[str] | None) -> list[Candidate]:
    """The models to measure: those named, or every translation model and every chat model
    that is a default somewhere (each tier's)."""
    models = catalog.load()
    if model_ids:
        chosen = []
        for model_id in model_ids:
            entry = models.get(model_id)
            if entry is None or entry.replaces is not None:
                raise ValueError(f"No model {model_id!r} in the catalog to measure.")
            if TRANSLATE_ROLE not in entry.roles and "main" not in entry.roles:
                raise ValueError(f"{entry.title} neither translates nor chats.")
            chosen.append(entry)
    else:
        chats = {e.id: e for tier in Tier if (e := models.defaults_for(tier).get("main"))}
        chosen = [*models.choices_for(TRANSLATE_ROLE), *chats.values()]
    return [_candidate(entry) for entry in chosen]


def _candidate(entry: CatalogEntry) -> Candidate:
    if TRANSLATE_ROLE in entry.roles:
        return Candidate(entry, f"{TRANSLATE_ROLE}@{entry.id}", entry)
    return Candidate(entry, f"main@{entry.id}", None)


def load_sentences(folder: Path, limit: int) -> dict[str, list[str]]:
    """The first `limit` sentences of each language in the folder, by code: en.txt and the
    others. Every file must have as many lines as en.txt (they're aligned)."""
    english = folder / f"{ENGLISH}.txt"
    if not english.is_file():
        raise ValueError(f"{folder} has no {ENGLISH}.txt: the sentences every language pairs with.")
    found: dict[str, list[str]] = {}
    for path in sorted(folder.glob("*.txt")):
        code = path.stem
        if code not in LANGUAGE_NAMES:
            raise ValueError(f"{path.name}: {code!r} isn't a language code Lectern knows.")
        found[code] = path.read_text(encoding="utf-8").splitlines()
    lines = len(found[ENGLISH])
    uneven = [code for code, sentences in found.items() if len(sentences) != lines]
    if uneven:
        raise ValueError(f"Not as many lines as en.txt ({lines}): {', '.join(uneven)}.")
    return {code: sentences[:limit] for code, sentences in found.items()}


async def translate_all(
    runtime,  # noqa: ANN001
    candidate: Candidate,
    source: str,
    target: str,
    sentences: list[str],
) -> tuple[list[str], int]:
    """Each sentence translated on its own, as the tool would translate one part, and how many
    the model didn't finish (empty: they score as nothing translated)."""
    out = []
    looped = 0
    async with runtime.lease(candidate.key, Priority.CHAT) as lease:
        for sentence in sentences:
            messages = tools._messages(candidate.translator, source, target, sentence)
            try:
                out.append((await tools._ask(lease.client, messages)).replace("\n", " ").strip())
            except tools.Looped:
                out.append("")
                looped += 1
    return out, looped


def chrf(hypotheses: list[str], references: list[str]) -> float:
    import sacrebleu  # a dev dependency: run this from the source checkout (uv sync)

    return round(sacrebleu.corpus_chrf(hypotheses, [references]).score, 1)


async def measure(
    runtime,  # noqa: ANN001
    models: list[Candidate],
    sentences: dict[str, list[str]],
    languages: list[str] | None = None,
    echo: Callable[[str], None] = print,
    measured: Callable[[str, str], bool] = lambda _model, _code: False,
    record: Callable[[str, str, dict[str, float]], None] = lambda _model, _code, _pair: None,
) -> dict[str, dict[str, dict[str, float]]]:
    """Scores by model id, language, and direction ("into": from English into it; "from": out
    of it into English). A pair `measured` says is done is skipped; each new one is handed to
    `record` at once."""
    available = [c for c in sentences if c != ENGLISH and (not languages or c in languages)]
    english = sentences[ENGLISH]
    found: dict[str, dict[str, dict[str, float]]] = {}
    for candidate in models:
        for code in candidate.languages(available):
            if measured(candidate.entry.id, code):
                continue
            into, looped_in = await translate_all(runtime, candidate, ENGLISH, code, english)
            out_of, looped_out = await translate_all(
                runtime, candidate, code, ENGLISH, sentences[code]
            )
            pair = {"into": chrf(into, sentences[code]), "from": chrf(out_of, english)}
            found.setdefault(candidate.entry.id, {})[code] = pair
            record(candidate.entry.id, code, pair)
            if looped_in or looped_out:
                echo(f"{candidate.entry.id} {code}: {looped_in + looped_out} sentences looped")
            into_score, from_score = pair["into"], pair["from"]
            echo(f"{candidate.entry.id:24} {code:4} into {into_score:5.1f}  from {from_score:5.1f}")
    return found


def already(path: Path) -> set[tuple[str, str]]:
    """The (model, language) pairs the scores file has."""
    if not path.is_file():
        return set()
    scores = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("scores") or {}
    return {(model_id, code) for model_id, languages in scores.items() for code in languages}


def merge(path: Path, found: dict, count: int) -> None:
    """The new scores into the file, beside the ones this run didn't measure."""
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    scores = (current or {}).get("scores") or {}
    for model_id, languages in found.items():
        scores.setdefault(model_id, {}).update(languages)
    header = (
        "# How well each model translates each language: chrF (sacrebleu) on FLORES-200 "
        "devtest,\n# into and out of English. Written by `lectern dev translate-check`; "
        "don't edit by hand.\n"
    )
    body = {
        "measured": datetime.date.today().isoformat(),
        "machine": f"{platform.system()} {platform.machine()}",
        "sentences": count,
        "scores": {m: dict(sorted(langs.items())) for m, langs in sorted(scores.items())},
    }
    text = yaml.safe_dump(body, allow_unicode=True, sort_keys=False, default_flow_style=None)
    path.write_text(header + text, encoding="utf-8")
