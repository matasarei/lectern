"""The OCR check (blueprint, M2): score candidate OCR models on your own pages.

Each model's text is compared with a reference text you corrected by hand. The scores are the
character and word error rates (CER, WER): the edits needed to turn the model's text into the
reference, divided by the reference's length. Both texts are normalised first, so markup the
models add (Markdown markers, HTML table tags, spacing) doesn't count as an error; the words
and characters do.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import tempfile
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Hashable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from lectern.catalog import catalog
from lectern.catalog.catalog import CatalogEntry
from lectern.modules.ocr.pages import IMAGE_SUFFIXES, rasterise
from lectern.modules.ocr.recognise import RecogniseFailed, recognise
from lectern.runtime.supervisor import ModelRuntime, NotAvailable

BREAK_TAGS = re.compile(r"<\s*(br|/p|/tr|/li|/h[1-6])\s*/?\s*>", re.I)
CELL_TAGS = re.compile(r"<\s*/t[dh]\s*>", re.I)
ANY_TAG = re.compile(r"<[^<>]+>")
TABLE_RULE = re.compile(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-*:?\s*\|?\s*$", re.M)  # |---|:--|
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
MARKERS = re.compile(r"[*_`|]+")
SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """The text as it reads, without markup: one space between words."""
    text = BREAK_TAGS.sub("\n", text)
    text = CELL_TAGS.sub(" ", text)
    text = html.unescape(ANY_TAG.sub("", text))
    text = TABLE_RULE.sub("", text)
    text = HEADING.sub("", text)
    text = MARKERS.sub(" ", text)
    return SPACE.sub(" ", unicodedata.normalize("NFC", text)).strip()


def distance(a: Sequence[Hashable], b: Sequence[Hashable]) -> int:
    """Levenshtein distance. Myers' bit-parallel algorithm (Hyyrö's form): one pass over `a`
    with Python's big integers as bit vectors over `b`, so a page of a few thousand characters
    takes milliseconds rather than the seconds a plain table would."""
    if len(b) > len(a):
        a, b = b, a
    m = len(b)
    if m == 0:
        return len(a)
    peq: dict[Hashable, int] = {}
    for i, item in enumerate(b):
        peq[item] = peq.get(item, 0) | (1 << i)
    full = (1 << m) - 1
    last = 1 << (m - 1)
    pv, mv, score = full, 0, m
    for item in a:
        eq = peq.get(item, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | ~(xh | pv)
        mh = pv & xh
        if ph & last:
            score += 1
        elif mh & last:
            score -= 1
        ph = (ph << 1) | 1
        mh = mh << 1
        pv = (mh | ~(xv | ph)) & full
        mv = ph & xv & full
    return score


def cer(output: str, reference: str) -> float:
    ref, out = normalise(reference), normalise(output)
    return distance(out, ref) / max(len(ref), 1)


def wer(output: str, reference: str) -> float:
    ref, out = normalise(reference).split(), normalise(output).split()
    return distance(out, ref) / max(len(ref), 1)


# --- the run -------------------------------------------------------------------------------
#
# Your folder holds the scans (PDFs and images) and, once you've corrected them, one
# `reference/<page>.txt` per page plus an optional `tags.yaml` ("lease-p003: [handwriting]").
# A page is named after its file: `invoice` for an image, `lease-p003` for page 3 of
# lease.pdf, so adding scans later never renames the pages you already have references for.
# Everything this check writes goes under `<folder>/.ocr-check/`, except `reference/`
# drafts, which only `--draft` writes and which it never overwrites.

WORK_DIR = ".ocr-check"
REFERENCE_DIR = "reference"
TAGS_FILE = "tags.yaml"


@dataclass(frozen=True)
class Page:
    number: int  # its place in the run, for --pages
    key: str  # "invoice", "lease-p003": names its image, output and reference files
    path: Path
    source: str  # the scan's file name
    source_page: int  # its page in that file, from 1


@dataclass(frozen=True)
class PageRun:
    seconds: float
    cut_off: bool
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass
class ModelRun:
    key: str  # the lease key, "ocr.hand@chandra-ocr-2"
    model_id: str
    title: str
    skipped: str | None = None
    pages: dict[int, PageRun] = field(default_factory=dict)
    failed: dict[int, str] = field(default_factory=dict)
    load_seconds: float = 0.0
    memory: int = 0  # bytes the model took while it ran


def candidates(model_ids: list[str] | None = None) -> list[tuple[str, CatalogEntry]]:
    """(lease key, entry) for every catalog model that reads pages, or the ones asked for."""
    models = catalog.load()
    entries = [m for m in models.models if catalog.OCR_ROLES & set(m.roles)]
    if model_ids:
        unknown = [i for i in model_ids if models.get(i) not in entries]
        if unknown:
            known = ", ".join(e.id for e in entries)
            raise ValueError(
                f"Not an OCR model in the catalog: {', '.join(unknown)}. OCR models: {known}."
            )
        entries = [e for e in entries if e.id in model_ids]
    return [(f"{'ocr' if 'ocr' in e.roles else 'ocr.hand'}@{e.id}", e) for e in entries]


def sources(folder: Path) -> list[Path]:
    scans = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and (p.suffix.lower() == ".pdf" or p.suffix.lower() in IMAGE_SUFFIXES)
    ]
    return sorted(scans, key=lambda p: p.name.casefold())


def prepare(folder: Path, echo: Callable[[str], None] = print) -> list[Page]:
    """Page images for every scan in the folder, numbered across files in name order. Kept
    between runs while the scans are unchanged."""
    scans = sources(folder)
    if not scans:
        raise ValueError(f"No PDFs or images in {folder}.")
    stems: dict[str, str] = {}
    for scan in scans:
        other = stems.setdefault(scan.stem.casefold(), scan.name)
        if other != scan.name:
            # Pages are named after their file, and references after their page.
            raise ValueError(f"{other} and {scan.name} share a name. Rename one of them.")
    work = folder / WORK_DIR / "pages"
    manifest = work / "pages.json"
    signature = [[p.name, p.stat().st_size, p.stat().st_mtime_ns] for p in scans]
    if manifest.is_file():
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        if saved.get("sources") == signature:
            return [
                Page(p["number"], p["key"], work / f"{p['key']}.png", p["source"], p["source_page"])
                for p in saved["pages"]
            ]
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    pages: list[Page] = []
    for scan in scans:
        with tempfile.TemporaryDirectory(dir=folder / WORK_DIR) as tmp:
            images = rasterise(scan, Path(tmp))
            for image in images:
                key = scan.stem if len(images) == 1 else f"{scan.stem}-p{image.number:03d}"
                target = work / f"{key}.png"
                image.path.replace(target)
                pages.append(Page(len(pages) + 1, key, target, scan.name, image.number))
        echo(f"  {scan.name}: {len(pages)} pages so far")
    listed = [asdict(p) | {"path": p.path.name} for p in pages]
    manifest.write_text(
        json.dumps({"sources": signature, "pages": listed}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return pages


def pick_pages(pages: list[Page], spec: str | None) -> list[Page]:
    """`--pages 1-5,8`: those page numbers only."""
    if not spec:
        return pages
    wanted: set[int] = set()
    for part in spec.split(","):
        first, _, last = part.strip().partition("-")
        if not first.isdigit() or (last and not last.isdigit()):
            raise ValueError(f"--pages takes numbers and ranges like 1-5,8, not {spec!r}.")
        wanted.update(range(int(first), int(last or first) + 1))
    return [p for p in pages if p.number in wanted]


async def run_models(
    runtime: ModelRuntime,
    folder: Path,
    pages: list[Page],
    models: list[tuple[str, CatalogEntry]],
    echo: Callable[[str], None] = print,
) -> list[ModelRun]:
    """Every page through each model in turn: one lease per model, the way OCR runs in
    phases. A model that can't be loaded is skipped; a page that fails is counted."""
    runs = []
    for key, entry in models:
        run = ModelRun(key, entry.id, entry.title)
        runs.append(run)
        out = folder / WORK_DIR / entry.id
        started = time.monotonic()
        try:
            async with runtime.lease(key) as lease:
                run.load_seconds = round(time.monotonic() - started, 1)
                echo(f"{entry.title}: loaded in {run.load_seconds}s")
                out.mkdir(parents=True, exist_ok=True)
                for page in pages:
                    try:
                        text = await recognise(
                            lease.client,
                            page.path,
                            prompt=entry.ocr.prompt,
                            max_side=entry.ocr.max_side,
                        )
                    except RecogniseFailed as exc:
                        run.failed[page.number] = str(exc)
                        echo(f"  {page.key}: failed: {exc}")
                        continue
                    (out / f"{page.key}.md").write_text(text.text + "\n", encoding="utf-8")
                    detail = asdict(text) | {"text": None}
                    (out / f"{page.key}.json").write_text(
                        json.dumps(detail, ensure_ascii=False), encoding="utf-8"
                    )
                    run.pages[page.number] = PageRun(
                        text.seconds, text.cut_off, text.prompt_tokens, text.completion_tokens
                    )
                    run.memory = max(run.memory, _resident_bytes(runtime, key))
                    cut = " · cut off" if text.cut_off else ""
                    echo(f"  {page.key}: {text.seconds}s{cut}")
        except NotAvailable as exc:
            run.skipped = str(exc)
            echo(f"{entry.title}: skipped: {exc}")
    return runs


def _resident_bytes(runtime: ModelRuntime, key: str) -> int:
    return next((r["bytes"] for r in runtime.state()["residents"] if r["role"] == key), 0)


def draft_references(folder: Path, model_id: str, pages: list[Page]) -> int:
    """Copy a model's text to `reference/<page>.txt` for each page that has none yet, for you
    to correct. Never overwrites a reference."""
    references = folder / REFERENCE_DIR
    references.mkdir(exist_ok=True)
    written = 0
    for page in pages:
        output = folder / WORK_DIR / model_id / f"{page.key}.md"
        target = references / f"{page.key}.txt"
        if output.is_file() and not target.exists():
            target.write_text(output.read_text(encoding="utf-8"), encoding="utf-8")
            written += 1
    return written


def load_tags(folder: Path) -> dict[str, list[str]]:
    """Page name → its kinds, e.g. {"invoice": ["printed", "table"]}."""
    path = folder / TAGS_FILE
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): [v] if isinstance(v, str) else list(v) for k, v in raw.items()}


LOOP_LINES = 4  # a run of this many lines seen twice means the model went round again


def word_counts(output: str, reference: str) -> tuple[int, int, int]:
    """(reference words the output lacks, output words the reference lacks, reference words),
    in any order. Reading order is the model's choice on a page of blocks and tables; these
    counts say whether the words are there, and `extra` catches loops and made-up text."""
    out, ref = Counter(normalise(output).split()), Counter(normalise(reference).split())
    return sum((ref - out).values()), sum((out - ref).values()), sum(ref.values())


def repeats(text: str) -> bool:
    """The output went round again: some run of LOOP_LINES lines appears twice."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    seen: set[tuple[str, ...]] = set()
    for i in range(len(lines) - LOOP_LINES + 1):
        window = tuple(lines[i : i + LOOP_LINES])
        if window in seen:
            return True
        seen.add(window)
    return False


@dataclass(frozen=True)
class Score:
    pages: int
    cer: float  # in reading order
    wer: float  # in reading order
    missed: float  # share of reference words not in the output, any order
    extra: float  # output words not in the reference, as a share of the reference's words
    looped: int  # pages where the output repeats itself


def score(
    folder: Path, run: ModelRun, pages: list[Page], tags: dict[str, list[str]]
) -> dict[str, Score]:
    """Scores over all pages with a reference ("all"), and for each tag. Errors are summed
    before dividing, so a long page weighs more than a short one."""
    # group → [char edits, chars, word edits, words, missed, extra, looped, pages]
    totals: dict[str, list[int]] = {}
    for page in pages:
        reference = folder / REFERENCE_DIR / f"{page.key}.txt"
        output = folder / WORK_DIR / run.model_id / f"{page.key}.md"
        if page.number not in run.pages or not reference.is_file():
            continue
        raw_ref = reference.read_text(encoding="utf-8")
        raw_out = output.read_text(encoding="utf-8")
        ref, out = normalise(raw_ref), normalise(raw_out)
        missed, extra, words = word_counts(raw_out, raw_ref)
        counts = [
            distance(out, ref),
            len(ref),
            distance(out.split(), ref.split()),
            words,
            missed,
            extra,
            int(repeats(raw_out)),
            1,
        ]
        for group in ["all", *tags.get(page.key, [])]:
            total = totals.setdefault(group, [0] * len(counts))
            for i, n in enumerate(counts):
                total[i] += n
    return {
        group: Score(
            pages=t[7],
            cer=t[0] / max(t[1], 1),
            wer=t[2] / max(t[3], 1),
            missed=t[4] / max(t[3], 1),
            extra=t[5] / max(t[3], 1),
            looped=t[6],
        )
        for group, t in totals.items()
    }


def report(
    folder: Path, runs: list[ModelRun], pages: list[Page], tags: dict[str, list[str]], swaps: int
) -> str:
    """The results as a Markdown report: one row per model, then words missed and extra by
    kind of page."""
    references = folder / REFERENCE_DIR
    missing = [p.key for p in pages if not (references / f"{p.key}.txt").is_file()]
    scores = {run.model_id: score(folder, run, pages, tags) for run in runs if not run.skipped}
    lines = [
        f"# OCR check · {len(pages)} pages · {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        "Words missed and words extra ignore reading order; CER counts it.",
        "",
        "| Model | Pages read | Words missed | Words extra | CER | Looped | s/page | Load "
        "| Memory | Cut off | Failed |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        if run.skipped:
            lines.append(f"| {run.title} | skipped: {run.skipped} |" + " |" * 9)
            continue
        overall = scores[run.model_id].get("all")
        seconds = [p.seconds for p in run.pages.values()]
        lines.append(
            f"| {run.title} | {len(run.pages)} | {_rate(overall, 'missed')} "
            f"| {_rate(overall, 'extra')} | {_rate(overall, 'cer')} "
            f"| {overall.looped if overall else '–'} "
            f"| {sum(seconds) / max(len(seconds), 1):.1f} | {run.load_seconds:.0f}s "
            f"| {run.memory / 1024**3:.1f} GB | {sum(p.cut_off for p in run.pages.values())} "
            f"| {len(run.failed)} |"
        )
    groups = sorted({g for s in scores.values() for g in s} - {"all"})
    if groups:
        scored = [r for r in runs if not r.skipped]
        header = "| Kind | Pages | " + " | ".join(r.title for r in scored) + " |"
        lines += [
            "",
            "Words missed / extra by kind of page:",
            "",
            header,
            "|---|---|" + "---|" * len(scored),
        ]
        for group in groups:
            count = max((s[group].pages for s in scores.values() if group in s), default=0)
            cells = [_both(scores[r.model_id].get(group)) for r in scored]
            lines.append(f"| {group} | {count} | " + " | ".join(cells) + " |")
    lines += ["", f"Swaps: {swaps}."]
    if missing:
        lines.append(f"No reference yet for {', '.join(missing)}: not scored.")
    return "\n".join(lines) + "\n"


def _both(value: Score | None) -> str:
    return "–" if value is None else f"{_rate(value, 'missed')} / {_rate(value, 'extra')}"


def _rate(value: Score | None, which: str) -> str:
    return "–" if value is None else f"{getattr(value, which) * 100:.1f}%"
