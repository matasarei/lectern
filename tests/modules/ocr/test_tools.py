from __future__ import annotations

import asyncio
import inspect

import pytest
from PIL import Image

from lectern.agent import followups
from lectern.agent.tools import ToolContext, ToolError
from lectern.catalog import catalog
from lectern.core.auth import LOCAL_USER
from lectern.hardware.tiers import Tier
from lectern.modules.ocr import store as ocr_store
from lectern.modules.ocr.tools import NOTE, START
from lectern.projects import files
from lectern.runtime.supervisor import NotAvailable

from ...projects.conftest import write_pdf, write_scan_pdf
from ..conftest import FakeDownloads


class FakeJobs:
    def __init__(self) -> None:
        self.started: list[int] = []
        self.events: list[dict] = []

    async def start(self, project, session_id: int) -> None:  # noqa: ANN001
        self.started.append(session_id)

    def channel(self, key):  # noqa: ANN001, ANN201
        jobs = self

        class _Channel:
            def publish(self, event: dict) -> None:
                jobs.events.append(event)

        return _Channel()


class FakeRuntime:
    tier = Tier.MINIMUM

    def __init__(self, swap: bool, missing: bool = False) -> None:
        self.swap = swap
        self.missing = missing
        self.chosen: dict[str, str | None] = {}

    def chosen_models(self) -> dict[str, str | None]:
        return self.chosen

    def resident_bytes(self) -> int:
        return 3 * 10**9 if self.swap else 0  # the chat model, loaded

    def needs_room(self, role: str) -> bool:
        if self.missing:  # as the real one: resolving a model that isn't on disk raises
            raise NotAvailable("PaddleOCR-VL 1.6 isn't downloaded yet. Download it first.")
        return self.swap and role == "ocr"


@pytest.fixture
def ctx(store, tmp_path):
    project = store.create(LOCAL_USER, "Scans")
    db = store.db(project)
    letter = ["Dear tenant, the rent is due on the tenth of each month."]
    sources = {"lease-scan.pdf": write_scan_pdf(tmp_path / "s.pdf", pages=3),
               "letter.pdf": write_pdf(tmp_path / "t.pdf", letter)}  # fmt: skip
    with db.begin() as session:
        for name, path in sources.items():
            files.write_bytes(project.root, session, owner_id=LOCAL_USER.id, name=name,
                              data=path.read_bytes(), origin=files.Origin("import"))  # fmt: skip
    Image.new("L", (40, 60), 255).save(tmp_path / "x.png")
    return ToolContext(project=project, sessions=db, user=LOCAL_USER, session_id=1,
                       ocr_jobs=FakeJobs(), runtime=FakeRuntime(swap=False))  # fmt: skip


def _call(tool, ctx, **arguments):  # noqa: ANN001, ANN202
    result = tool.run(ctx, tool.validate(arguments))
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _question(ctx, **arguments) -> str:  # noqa: ANN001
    return START.confirm_text(ctx, START.validate(arguments))


def test_the_card_names_the_file_its_pages_the_time_and_what_follows(ctx):
    assert START.confirm
    assert _question(ctx, name="lease-scan.pdf") == (
        "Start an OCR session for lease-scan.pdf? 3 pages · about 1 minute on this PC · "
        "opens in a new tab."
    )
    ctx.runtime = FakeRuntime(swap=True)
    question = _question(ctx, name="lease-scan.pdf", then_export="odt")
    assert (
        "The chat model is unloaded while pages are read, so replies wait until then." in question
    )
    assert question.endswith("When it finishes: export to ODT.")


def test_a_file_with_text_or_not_a_page_source_is_refused_before_any_card(ctx):
    with pytest.raises(ToolError, match="letter.pdf has text: read it with document_read") as e:
        _question(ctx, name="letter.pdf")
    # …and says how to read it with OCR anyway, so the model doesn't go round in circles.
    assert "keep_layout=true" in str(e.value) and "export it directly" in str(e.value)
    with pytest.raises(ToolError, match="No file called exactly"):
        _question(ctx, name="nothing.pdf")


def test_keep_layout_reads_a_pdf_with_text_and_the_card_says_why(ctx):
    question = _question(ctx, name="letter.pdf", keep_layout=True, then_export="docx")
    assert question.startswith(
        "Start an OCR session for letter.pdf? This PDF has text; OCR reads it again to keep its "
        "tables and layout. 1 page · about 1 minute on this PC"
    )
    assert question.endswith("When it finishes: export to DOCX.")

    result = _call(START, ctx, name="letter.pdf", keep_layout=True, then_export="docx")

    (sid,) = ctx.ocr_jobs.started
    assert result.action == {
        "type": "open_tab",
        "tab": {"kind": "session", "id": sid},
        "focus": False,
    }
    (follow,) = followups.pending(ctx.sessions, 1)
    assert follow.arguments == {"name": "letter/", "format": "docx"}


def test_starting_without_an_export_asks_for_a_notice_when_it_finishes(ctx):
    _call(START, ctx, name="lease-scan.pdf")

    (sid,) = ctx.ocr_jobs.started
    (notice,) = followups.pending(ctx.sessions, 1)
    assert (notice.ocr_session_id, notice.tool, notice.what) == (sid, "", "")
    assert followups.is_notice(notice)


def test_starting_opens_the_session_and_queues_the_export(ctx):
    result = _call(START, ctx, name="lease-scan.pdf", then_export="odt")

    (sid,) = ctx.ocr_jobs.started
    assert result.action == {
        "type": "open_tab",
        "tab": {"kind": "session", "id": sid},
        "focus": False,
    }
    (follow,) = followups.pending(ctx.sessions, 1)
    assert (follow.ocr_session_id, follow.tool) == (sid, "export.run")
    assert follow.arguments == {"name": "lease-scan/", "format": "odt"}
    assert follow.label == "after OCR · lease-scan.pdf: export to ODT"
    assert follow.what == "export to ODT"  # and no notice beside it
    assert 'Its document will be "lease-scan/" when it finishes.' in result.text
    assert (
        "The export to ODT is queued: it runs by itself when the session finishes, so there's "
        "nothing more to do now." in result.text
    )


def test_a_note_reaches_the_page_and_the_tab(ctx):
    _call(START, ctx, name="lease-scan.pdf")
    (sid,) = ctx.ocr_jobs.started
    ocr = ocr_store._ocr(ctx.sessions, sid)
    from lectern.modules.ocr.pages import rasterise

    images = rasterise(ocr_store.source_path(ctx.project.root, ctx.sessions, ocr),
                       ocr_store.pages_dir(ctx.project.root, ocr))  # fmt: skip
    ocr_store.add_pages(ctx.sessions, sid, images)

    result = _call(NOTE, ctx, session="lease-scan.pdf", page=2, note="Ignore the handwriting.")

    assert [p.note for p in ocr_store.pages(ctx.sessions, sid)] == [
        "",
        "Ignore the handwriting.",
        "",
    ]
    assert ctx.ocr_jobs.events[-1]["type"] == "page" and ctx.ocr_jobs.events[-1]["number"] == 2
    assert "Saved the note for page 2 of lease-scan.pdf" in result.text
    with pytest.raises(ToolError, match="has pages 1–3"):
        _call(NOTE, ctx, session="lease-scan", page=9, note="x")
    with pytest.raises(ToolError, match="No OCR session"):
        _call(NOTE, ctx, session="nothing", page=1, note="x")


def _missing_ocr_model(ctx, **downloads):  # noqa: ANN001, ANN202
    """The chat model loaded and the OCR model not on disk, as on a fresh install."""
    ctx.runtime = FakeRuntime(swap=True, missing=True)
    ctx.downloads = FakeDownloads(downloaded=False, **downloads)
    events: list[dict] = []
    ctx.emit = events.append
    return catalog.load().defaults_for(Tier.MINIMUM)["ocr"], events


def test_without_the_ocr_model_the_card_says_it_downloads_it_first(ctx):
    entry, _ = _missing_ocr_model(ctx)

    question = _question(ctx, name="lease-scan.pdf")  # didn't raise and end the turn

    assert (
        f"It downloads {entry.title} first ({entry.size / 1e9:.1f} GB, {entry.licence})."
        in question
    )
    # The runtime can't size a model before it's on disk, but the chat model is loaded.
    assert question.endswith(
        "The chat model may be unloaded while pages are read, so replies may wait until then."
    )


def test_the_card_and_the_download_are_for_the_ocr_model_chosen_for_the_role(ctx, monkeypatch):
    real = catalog.load()
    other = real.get("paddleocr-vl-1.6").model_copy(
        update={"id": "other-ocr", "title": "Other OCR", "defaults": {}}
    )
    fake = real.model_copy(update={"models": [*real.models, other]})
    monkeypatch.setattr(catalog, "load", lambda: fake)
    _missing_ocr_model(ctx)
    ctx.runtime.chosen = {"ocr": "other-ocr"}

    assert "It downloads Other OCR first" in _question(ctx, name="lease-scan.pdf")
    _call(START, ctx, name="lease-scan.pdf")
    assert ctx.downloads.started == ["other-ocr"]


def test_starting_downloads_the_ocr_model_then_opens_the_session(ctx):
    entry, events = _missing_ocr_model(ctx)

    result = _call(START, ctx, name="lease-scan.pdf")

    assert ctx.downloads.started == [entry.id]
    bars = [e for e in events if e.get("label") == f"Downloading {entry.title}"]
    assert [b["done"] for b in bars] == [entry.size // 2, entry.size]
    assert len(ctx.ocr_jobs.started) == 1 and result.action["type"] == "open_tab"


def test_a_failed_ocr_model_download_starts_no_session(ctx):
    _missing_ocr_model(ctx, fail=True)

    with pytest.raises(ToolError, match="download failed: disk full"):
        _call(START, ctx, name="lease-scan.pdf")
    assert ctx.ocr_jobs.started == []


def test_an_ocr_model_download_stopped_midway_starts_no_session(ctx):
    entry, _ = _missing_ocr_model(ctx, end="paused")

    with pytest.raises(ToolError, match=f"{entry.title}'s download was stopped.*resumes it"):
        _call(START, ctx, name="lease-scan.pdf")
    assert ctx.ocr_jobs.started == []
