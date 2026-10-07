from __future__ import annotations

import asyncio
import inspect

import pytest
from PIL import Image

from lectern.agent import followups
from lectern.agent.tools import ToolContext, ToolError
from lectern.core.auth import LOCAL_USER
from lectern.modules.ocr import store as ocr_store
from lectern.modules.ocr.tools import NOTE, START
from lectern.projects import files

from ...projects.conftest import write_pdf, write_scan_pdf


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
    def __init__(self, swap: bool) -> None:
        self.swap = swap

    def needs_room(self, role: str) -> bool:
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
    with pytest.raises(ToolError, match="letter.pdf has text: read it with document_read"):
        _question(ctx, name="letter.pdf")
    with pytest.raises(ToolError, match="No file called exactly"):
        _question(ctx, name="nothing.pdf")


def test_starting_opens_the_session_and_queues_the_export(ctx):
    result = _call(START, ctx, name="lease-scan.pdf", then_export="odt")

    (sid,) = ctx.ocr_jobs.started
    assert result.action == {"type": "open_tab", "tab": {"kind": "session", "id": sid}}
    (follow,) = followups.pending(ctx.sessions, 1)
    assert (follow.ocr_session_id, follow.tool) == (sid, "export.run")
    assert follow.arguments == {"name": "lease-scan/", "format": "odt"}
    assert follow.label == "after OCR · lease-scan.pdf: export to ODT"
    assert "After it finishes I'll export to ODT." in result.text


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
