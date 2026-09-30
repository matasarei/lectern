from __future__ import annotations

import asyncio
import contextlib
import json
import random
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from PIL import Image

from lectern.dev.ocr_check import (
    Score,
    candidates,
    cer,
    distance,
    draft_references,
    load_tags,
    pick_pages,
    prepare,
    report,
    run_models,
    score,
    wer,
    word_counts,
)
from lectern.modules.ocr.text import normalise
from lectern.runners.base import RunnerError
from lectern.runtime.supervisor import NotAvailable

from .projects.conftest import write_scan_pdf


def _plain_distance(a, b) -> int:  # noqa: ANN001
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, start=1):
        previous, row[0] = row[0], i
        for j, y in enumerate(b, start=1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (x != y))
    return row[-1]


def test_the_fast_distance_matches_the_plain_one():
    rng = random.Random(7)  # noqa: S311 — seeded test data, not security
    for _ in range(300):
        a = "".join(rng.choice("абвг ab") for _ in range(rng.randint(0, 90)))
        b = "".join(rng.choice("абвг ab") for _ in range(rng.randint(0, 90)))
        assert distance(a, b) == _plain_distance(a, b), (a, b)
    assert distance("kitten", "sitting") == 3
    assert distance(["one", "two"], ["one", "three", "two"]) == 1


def test_identical_text_scores_zero_and_one_wrong_character_in_100_scores_001():
    reference = ("Орендар сплачує пеню " * 5)[:99] + "."  # 100 characters, as normalised
    assert len(normalise(reference)) == 100
    wrong = reference[:50] + "x" + reference[51:]

    assert cer(reference, reference) == 0 and wer(reference, reference) == 0
    assert cer(wrong, reference) == pytest.approx(0.01)


def test_markup_the_models_add_is_not_an_error():
    reference = "3. Орендна плата\n\nберезень 1998 250,00\nквітень 1998 250,00"
    markdown = "## 3. **Орендна плата**\n\n| березень 1998 | 250,00 |\n|---|---|\n| квітень 1998 | 250,00 |"  # noqa: E501
    html_table = (
        "# 3. Орендна плата\n<table><tr><td>березень 1998</td><td>250,00</td></tr>"
        "<tr><td>квітень&nbsp;1998</td><td>250,00</td></tr></table>"
    )

    assert cer(markdown, reference) == 0
    divs = '<div data-label="Text"><p>3. Орендна плата</p></div><div><p>березень 1998</p>'
    assert word_counts(divs + " 250,00 квітень 1998 250,00</div>", reference) == (0, 0, 9)
    assert cer(html_table, reference) == 0
    assert wer("  " + reference.replace(" ", "   "), reference) == 0


def test_composed_and_decomposed_letters_are_the_same():
    composed, decomposed = "Київ, й", "Київ, й"
    assert normalise(decomposed) == composed and cer(decomposed, composed) == 0


def test_an_empty_reference_counts_every_character_of_the_output():
    assert cer("", "") == 0
    assert cer("abc", "") == 3  # divided by 1, not by zero
    assert wer("", "two words") == 1


# --- the run, with a stand-in runtime ------------------------------------------------------


class FakeRuntime:
    """Leases answer from a list of texts per model; a model not listed can't be loaded."""

    def __init__(self, texts: dict[str, list[str | int]]):
        self.texts = {key: list(v) for key, v in texts.items()}
        self.leases: list[str] = []
        self.swaps = 0

    @contextlib.asynccontextmanager
    async def lease(self, key: str):  # noqa: ANN202
        if key not in self.texts:
            raise NotAvailable(f"{key.split('@')[1]} isn't downloaded yet. Download it first.")
        if self.texts[key] == ["crash"]:
            raise RunnerError("llama-server for it wasn't ready.\nlog: unknown model architecture")
        self.leases.append(key)

        async def handler(request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(0)
            answer = self.texts[key].pop(0)
            if isinstance(answer, int):
                return httpx2.Response(answer, json={"error": {"message": "boom"}})
            reply = {"message": {"content": answer}, "finish_reason": "stop"}
            return httpx2.Response(200, json={"choices": [reply], "usage": {}})

        client = httpx2.AsyncClient(base_url="http://m", transport=httpx2.MockTransport(handler))
        async with client:
            yield SimpleNamespace(client=client)

    def state(self) -> dict:
        return {"residents": [{"role": k, "bytes": 2 * 1024**3} for k in self.leases[-1:]]}

    async def stop_all(self) -> None:
        self.stopped = True


PADDLE = "ocr@paddleocr-vl-1.6"
QWEN = "ocr.hand@qwen3-vl-4b"
CHANDRA = "ocr.hand@chandra-ocr-2"


def _scans(folder: Path) -> Path:
    folder.mkdir()
    write_scan_pdf(folder / "a-lease.pdf", pages=2)
    Image.new("L", (300, 400), 255).save(folder / "b-receipt.png")
    (folder / "notes.txt").write_text("not a scan")
    return folder


def _run(folder: Path, runtime: FakeRuntime, keys: list[str]):  # noqa: ANN202
    pages = prepare(folder, echo=lambda _l: None)
    models = [(k, e) for k, e in candidates() if k in keys]
    return pages, asyncio.run(run_models(runtime, folder, pages, models, echo=lambda _l: None))


def test_scans_become_pages_numbered_across_files_and_are_kept_between_runs(tmp_path: Path):
    folder = _scans(tmp_path / "scans")

    pages = prepare(folder, echo=lambda _l: None)

    assert [(p.number, p.key, p.source, p.source_page) for p in pages] == [
        (1, "a-lease-p001", "a-lease.pdf", 1),
        (2, "a-lease-p002", "a-lease.pdf", 2),
        (3, "b-receipt", "b-receipt.png", 1),
    ]  # fmt: skip
    first = pages[0].path.stat().st_mtime_ns
    assert prepare(folder, echo=lambda _l: None) == pages
    assert pages[0].path.stat().st_mtime_ns == first  # not rendered again


def test_each_model_reads_every_page_under_one_lease(tmp_path: Path):
    folder = _scans(tmp_path / "scans")
    runtime = FakeRuntime({PADDLE: ["один", "два", 500], QWEN: ["one", "two", "three"]})

    pages, runs = _run(folder, runtime, [PADDLE, QWEN, CHANDRA])

    assert runtime.leases == [PADDLE, QWEN]  # one lease each: OCR runs in phases
    paddle, qwen, chandra = runs
    assert sorted(paddle.pages) == [1, 2] and list(paddle.failed) == [3]
    assert "answered 500" in paddle.failed[3]
    assert len(qwen.pages) == 3 and qwen.memory == 2 * 1024**3
    assert chandra.skipped == "chandra-ocr-2 isn't downloaded yet. Download it first."
    out = folder / ".ocr-check" / "paddleocr-vl-1.6"
    assert (out / "a-lease-p002.md").read_text(encoding="utf-8") == "два\n"
    assert json.loads((out / "a-lease-p001.json").read_text())["text"] is None
    written = {p.relative_to(folder).parts[0] for p in folder.rglob("*") if p.is_file()}
    assert written == {"a-lease.pdf", "b-receipt.png", "notes.txt", ".ocr-check"}


def test_a_draft_fills_missing_references_and_never_overwrites_one(tmp_path: Path):
    folder = _scans(tmp_path / "scans")
    pages, (run,) = _run(folder, FakeRuntime({PADDLE: ["один", "два", "три"]}), [PADDLE])
    (folder / "reference").mkdir()
    (folder / "reference" / "a-lease-p001.txt").write_text("Один, виправлено", encoding="utf-8")

    assert draft_references(folder, run, pages) == 2
    fixed = (folder / "reference" / "a-lease-p001.txt").read_text(encoding="utf-8")
    assert fixed == "Один, виправлено"
    assert (folder / "reference" / "b-receipt.txt").read_text(encoding="utf-8") == "три\n"


def test_a_draft_never_copies_what_an_earlier_run_left(tmp_path: Path):
    folder = _scans(tmp_path / "scans")
    _run(folder, FakeRuntime({PADDLE: ["old one", "old two", "old three"]}), [PADDLE])
    pages, (run,) = _run(folder, FakeRuntime({PADDLE: ["new one", 500, "new three"]}), [PADDLE])

    assert draft_references(folder, run, pages) == 2
    assert not (folder / "reference" / "a-lease-p002.txt").exists()  # failed now: not "old two"


def test_the_report_scores_each_model_overall_and_by_kind_of_page(tmp_path: Path):
    folder = _scans(tmp_path / "scans")
    runtime = FakeRuntime(
        {PADDLE: ["abcdefghij", "klmnopqrst", "x"], QWEN: ["abcdefghiX", 500, "x"]}
    )
    pages, runs = _run(folder, runtime, [PADDLE, QWEN, CHANDRA])
    (folder / "reference").mkdir()
    (folder / "reference" / "a-lease-p001.txt").write_text("abcdefghij")
    (folder / "reference" / "a-lease-p002.txt").write_text("klmnopqrst")
    (folder / "tags.yaml").write_text("a-lease-p001: [handwriting, table]\na-lease-p002: table\n")
    tags = load_tags(folder)

    assert tags == {"a-lease-p001": ["handwriting", "table"], "a-lease-p002": ["table"]}
    paddle, qwen, _ = (score(folder, r, pages, tags) for r in runs)
    assert paddle["all"] == Score(pages=2, cer=0.0, wer=0.0, missed=0.0, extra=0.0, looped=0)
    assert qwen["all"].pages == 1 and qwen["all"].cer == pytest.approx(0.1)
    assert (qwen["handwriting"].missed, qwen["handwriting"].extra) == (1.0, 1.0)
    text = report(folder, runs, pages, tags, swaps=1)
    assert "| PaddleOCR-VL 1.6 | 3 | 0.0% | 0.0% | 0.0% | 0 |" in text
    assert "| Qwen3-VL 4B Instruct | 2 | 100.0% | 100.0% | 10.0% | 0 |" in text
    assert "| Chandra OCR 2 | skipped: chandra-ocr-2 isn't downloaded yet." in text
    assert "| handwriting | 1 | 0.0% / 0.0% | 100.0% / 100.0% |" in text
    assert "| table | 2 | 0.0% / 0.0% | 100.0% / 100.0% |" in text
    assert "No reference yet for b-receipt: not scored." in text


def test_pages_and_models_can_be_narrowed():
    pages = [SimpleNamespace(number=n) for n in range(1, 11)]
    assert [p.number for p in pick_pages(pages, "1-3,8")] == [1, 2, 3, 8]
    with pytest.raises(ValueError, match="--pages takes numbers"):
        pick_pages(pages, "first")
    assert [e.id for _, e in candidates(["chandra-ocr-2"])] == ["chandra-ocr-2"]
    with pytest.raises(ValueError, match="Not an OCR model in the catalog: qwen3.5-4b"):
        candidates(["qwen3.5-4b"])


def test_adding_a_scan_later_doesnt_rename_the_pages_already_there(tmp_path: Path):
    folder = _scans(tmp_path / "scans")
    before = {p.key for p in prepare(folder, echo=lambda _l: None)}
    Image.new("L", (300, 400), 255).save(folder / "0-first.png")  # sorts before the others

    after = {p.key for p in prepare(folder, echo=lambda _l: None)}

    assert after == before | {"0-first"}
    Image.new("L", (300, 400), 255).save(folder / "b-receipt.jpg")  # same name, other type
    with pytest.raises(ValueError, match="b-receipt.jpg and b-receipt.png share a name"):
        prepare(folder, echo=lambda _l: None)


def test_word_counts_ignore_reading_order_but_not_missing_or_extra_words():
    reference = "QTY DESCRIPTION\n1 Brake cables 100.00\n2 Pedal arms 30.00"
    by_column = "QTY\n1\n2\nDESCRIPTION\nBrake cables\nPedal arms\n100.00\n30.00"

    assert word_counts(by_column, reference) == (0, 0, 10)  # same words, other order
    assert cer(by_column, reference) > 0.3  # which CER counts as errors
    assert word_counts("QTY DESCRIPTION 1 Brake cables", reference) == (5, 0, 10)
    assert word_counts(reference + "\n" + reference, reference) == (0, 10, 10)


def test_a_model_whose_server_fails_to_start_is_skipped_and_the_rest_still_run(tmp_path: Path):
    folder = _scans(tmp_path / "scans")
    runtime = FakeRuntime({PADDLE: ["crash"], QWEN: ["one", "two", "three"]})

    _, (paddle, qwen) = _run(folder, runtime, [PADDLE, QWEN])

    assert paddle.skipped == "llama-server for it wasn't ready."  # first line, not the log
    assert len(qwen.pages) == 3


def test_a_page_list_from_before_pages_had_names_is_rebuilt(tmp_path: Path):
    folder = _scans(tmp_path / "scans")
    pages = prepare(folder, echo=lambda _l: None)
    manifest = folder / ".ocr-check" / "pages" / "pages.json"
    saved = json.loads(manifest.read_text())
    for page in saved["pages"]:
        del page["key"]
    manifest.write_text(json.dumps(saved))

    assert prepare(folder, echo=lambda _l: None) == pages


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("- invoice\n- lease", "should have one line per page"),
        ("invoice:\n", "invoice needs a kind or a list of kinds"),
        ("invoice: [printed, {a: b}]", "invoice needs a kind or a list of kinds"),
        ("invoice: [printed", "isn't valid YAML"),
    ],
)
def test_a_malformed_tags_file_says_whats_wrong(tmp_path: Path, content: str, message: str):
    (tmp_path / "tags.yaml").write_text(content)
    with pytest.raises(ValueError, match=message):
        load_tags(tmp_path)
