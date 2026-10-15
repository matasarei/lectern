from __future__ import annotations

import pytest

from lectern.agent import followups, state
from lectern.core.auth import LOCAL_USER
from lectern.projects.tables import SessionRow


@pytest.fixture
def chats(store):
    project = store.create(LOCAL_USER, "Follow-ups")
    db = store.db(project)
    with db.begin() as session:
        second = SessionRow(owner_id=LOCAL_USER.id, kind="chat", title="Second")
        session.add(second)
        session.flush()
        return db, 1, second.id


def test_a_follow_up_waits_in_its_chat_and_the_chat_state_shows_it(chats):
    db, chat, _ = chats
    added = followups.add(db, chat, ocr_session_id=7, tool="export.run",
                          arguments={"name": "Lease 1998/", "format": "odt"},
                          label="export to ODT")  # fmt: skip

    assert followups.pending(db, chat) == [added]
    assert state.load(db, chat).followups == [added]
    assert (added.ocr_session_id, added.tool, added.arguments["format"]) == (7, "export.run", "odt")


def test_the_chats_waiting_on_a_session_are_found_and_others_left(chats):
    db, first, second = chats
    a = followups.add(db, first, ocr_session_id=7, tool="export.run", arguments={}, label="a")
    b = followups.add(db, second, ocr_session_id=7, tool="export.run", arguments={}, label="b")
    followups.add(db, second, ocr_session_id=8, tool="export.run", arguments={}, label="c")

    assert followups.waiting_on(db, 7) == [(first, a), (second, b)]
    assert followups.waiting_on(db, 9) == []


def test_notices_dont_take_a_follow_ups_place(chats):
    db, chat, _ = chats
    for n in range(4):
        followups.add(db, chat, ocr_session_id=n, tool="", arguments={}, label="say it's done")
    for n in range(followups.MAX_FOLLOWUPS):  # five exports still fit beside four notices
        followups.add(db, chat, ocr_session_id=n, tool="export.run", arguments={}, label="x")
    followups.add(db, chat, ocr_session_id=9, tool="", arguments={}, label="say it's done")
    with pytest.raises(followups.FollowUpError):
        followups.add(db, chat, ocr_session_id=9, tool="export.run", arguments={}, label="y")


def test_a_follow_up_is_taken_once(chats):
    db, chat, _ = chats
    added = followups.add(db, chat, ocr_session_id=7, tool="export.run", arguments={}, label="x")

    assert followups.take(db, chat, added.id) == added
    assert followups.take(db, chat, added.id) is None  # never twice
    assert followups.pending(db, chat) == []


def test_a_chat_holds_at_most_a_few_follow_ups(chats):
    db, chat, _ = chats
    for i in range(followups.MAX_FOLLOWUPS):
        followups.add(db, chat, ocr_session_id=i, tool="export.run", arguments={}, label="x")
    with pytest.raises(followups.FollowUpError, match="already waiting"):
        followups.add(db, chat, ocr_session_id=99, tool="export.run", arguments={}, label="x")


# --- running follow-ups when a session finishes, and task.after ----------------------------

import asyncio  # noqa: E402
import json  # noqa: E402

from pydantic import BaseModel  # noqa: E402
from sqlalchemy import select  # noqa: E402

from lectern.agent.core_tools import CORE_TOOLS  # noqa: E402
from lectern.agent.followups import TASK_AFTER  # noqa: E402
from lectern.agent.tools import (  # noqa: E402
    AgentTool,
    Registry,
    ToolContext,
    ToolError,
    ToolResult,
)
from lectern.agent.turns import TurnManager  # noqa: E402
from lectern.projects.tables import ArtifactRow, MessageRow, OcrSessionRow  # noqa: E402

from .fake_model import FakeHandle, make_runtime, text_reply  # noqa: E402

RAN: list[dict] = []


class _EchoParams(BaseModel):
    format: str = "odt"


def _echo(ctx: ToolContext, params: _EchoParams) -> ToolResult:
    RAN.append({"chat": ctx.session_id, "format": params.format})
    return ToolResult(f"Saved as Lease 1998.{params.format}.")


ECHO = AgentTool(name="export.run", description="Export.", params=_EchoParams, run=_echo)
CARDED = AgentTool(name="file.wipe", description="Asks.", params=_EchoParams, run=_echo,
                   confirm=True)  # fmt: skip


@pytest.fixture
def follow(store):
    RAN.clear()

    def make(replies, *, hold=None):  # noqa: ANN001, ANN202
        handle = FakeHandle(replies, hold=hold)
        manager = TurnManager(make_runtime(handle), store,
                              Registry([*CORE_TOOLS, ECHO, CARDED]))  # fmt: skip
        project = store.create(LOCAL_USER, "Scans")
        db = store.db(project)
        with db.begin() as session:
            source = ArtifactRow(owner_id=LOCAL_USER.id, path="lease-scan.pdf", kind="imported",
                                 mime="application/pdf", size=1, sha256="0" * 64,
                                 from_kind="import")  # fmt: skip
            scan = SessionRow(owner_id=LOCAL_USER.id, kind="ocr", title="lease-scan.pdf")
            session.add_all([source, scan])
            session.flush()
            session.add(OcrSessionRow(session_id=scan.id, source_artifact_id=source.id,
                                      folder="lease-scan", status="running"))  # fmt: skip
            ocr_id = scan.id
        return manager, project, db, ocr_id, handle

    return make


def _transcript(db, chat: int = 1) -> list[tuple]:  # noqa: ANN001
    with db() as session:
        rows = session.scalars(select(MessageRow).where(MessageRow.session_id == chat)
                               .order_by(MessageRow.id)).all()  # fmt: skip
    return [(r.role, r.content, json.loads(r.tool_calls)[0]["function"]["name"] if r.tool_calls
             else r.tool_name) for r in rows]  # fmt: skip


DONE = "OCR · lease-scan.pdf finished · 3 pages · 1 open question."


def test_when_its_session_is_done_the_follow_up_runs_once_and_the_model_reports(follow):
    manager, project, db, ocr_id, _ = follow([text_reply("Done: Lease 1998.odt is saved.")])
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={"format": "odt"},
                  label="after OCR · lease-scan.pdf: export to ODT")  # fmt: skip

    async def go():  # noqa: ANN202
        await manager.after_session(project, ocr_id, "done", DONE)
        await manager.after_session(project, ocr_id, "done", DONE)  # a second signal: no rerun
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert RAN == [{"chat": 1, "format": "odt"}]
    assert _transcript(db) == [
        ("assistant", DONE, "export_run"),
        ("tool", "Saved as Lease 1998.odt.", "export.run"),
        ("assistant", "Done: Lease 1998.odt is saved.", None),
    ]
    assert followups.pending(db, 1) == []


def test_when_its_session_fails_the_follow_up_is_dropped_and_reported(follow):
    manager, project, db, ocr_id, _ = follow([text_reply("The OCR session stopped, sorry.")])
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={},
                  label="after OCR · lease-scan.pdf: export to ODT")  # fmt: skip

    async def go():  # noqa: ANN202
        await manager.after_session(
            project, ocr_id, "failed", "OCR · lease-scan.pdf stopped: no model."
        )
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert RAN == []
    (notice, reply) = _transcript(db)
    assert notice[0] == "assistant" and notice[1] == (
        "OCR · lease-scan.pdf stopped: no model. The follow-up (after OCR · lease-scan.pdf: "
        "export to ODT) was dropped."
    )
    assert reply == ("assistant", "The OCR session stopped, sorry.", None)
    assert followups.pending(db, 1) == []


def _notice(db, ocr_id: int, chat: int = 1):  # noqa: ANN001, ANN202
    return followups.add(db, chat, ocr_session_id=ocr_id, tool="", arguments={},
                         label="after OCR · lease-scan.pdf: say it's done")  # fmt: skip


def _refs(db, chat: int = 1) -> list:  # noqa: ANN001
    with db() as session:
        rows = session.scalars(select(MessageRow).where(MessageRow.session_id == chat)
                               .order_by(MessageRow.id)).all()  # fmt: skip
    return [json.loads(r.refs) if r.refs else [] for r in rows]


def test_a_notice_posts_a_plain_message_with_the_document_and_runs_no_model(follow):
    manager, project, db, ocr_id, handle = follow([])
    with db.begin() as session:
        doc = ArtifactRow(owner_id=LOCAL_USER.id, path="lease-scan/document.md",
                          kind="generated", mime="text/markdown", size=1, sha256="1" * 64,
                          from_kind="session")  # fmt: skip
        session.add(doc)
        session.flush()
        session.get(OcrSessionRow, ocr_id).document_artifact_id = doc.id
        doc_id = doc.id
    _notice(db, ocr_id)

    async def go():  # noqa: ANN202
        await manager.after_session(project, ocr_id, "done", DONE)
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert _transcript(db) == [("assistant", DONE, None)]
    turn = next(e for e in manager.channel((project.id, 1)).events if e["type"] == "turn")
    assert turn["followup"] == ""  # no "Follow-up: … say it's done" on the page
    assert _refs(db) == [[
        {"kind": "session", "id": ocr_id, "name": "lease-scan.pdf", "ref": None},
        {"kind": "artifact", "id": doc_id, "name": "lease-scan/document.md", "ref": None},
    ]]  # fmt: skip
    assert handle.requests == []  # no model turn, so no swap back to the chat's model
    assert followups.pending(db, 1) == []


def test_a_notice_for_a_failed_session_says_why_it_stopped(follow):
    manager, project, db, ocr_id, handle = follow([])
    _notice(db, ocr_id)
    stopped = "OCR · lease-scan.pdf stopped: no model."

    async def go():  # noqa: ANN202
        await manager.after_session(project, ocr_id, "failed", stopped)
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert _transcript(db) == [("assistant", stopped, None)]
    assert handle.requests == []


def test_a_notice_is_dropped_when_an_export_for_the_same_session_reports_instead(follow):
    manager, project, db, ocr_id, _ = follow([text_reply("Done: Lease 1998.odt is saved.")])
    _notice(db, ocr_id)
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={"format": "odt"},
                  label="after OCR · lease-scan.pdf: export to ODT")  # fmt: skip

    async def go():  # noqa: ANN202
        await manager.after_session(project, ocr_id, "done", DONE)
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert [m[2] for m in _transcript(db)] == ["export_run", "export.run", None]  # one report
    assert followups.pending(db, 1) == []


def test_a_notice_whose_session_finished_unseen_posts_when_the_chat_is_next_opened(follow):
    manager, project, db, ocr_id, handle = follow([])
    _notice(db, ocr_id)
    with db.begin() as session:
        session.get(OcrSessionRow, ocr_id).status = "done"

    async def go():  # noqa: ANN202
        await manager.resume_follow_ups(project, 1)
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert _transcript(db) == [("assistant", "OCR · lease-scan.pdf finished.", None)]
    assert handle.requests == []


def test_a_notice_posted_late_says_the_session_isnt_finished_while_questions_are_open(follow):
    """After a restart the job's own signal is gone: the line comes from the job's notice,
    not a bare "finished", so open questions aren't hidden."""
    manager, project, db, ocr_id, handle = follow([])
    _notice(db, ocr_id)
    with db.begin() as session:
        session.get(OcrSessionRow, ocr_id).status = "done"
    line = "OCR · lease-scan.pdf isn't finished yet: 3 pages read · 2 open questions."

    class Jobs(_Summary):
        def notice(self, project, session_id):  # noqa: ANN001, ANN201
            return "done", line

    manager.ocr_jobs = Jobs()  # a notice is only a message: it's posted, open questions or not

    async def go():  # noqa: ANN202
        await manager.resume_follow_ups(project, 1)
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert _transcript(db) == [("assistant", line, None)]
    assert handle.requests == []


def test_the_model_sees_waiting_exports_but_not_notices(follow):
    manager, project, _, ocr_id, handle = follow([text_reply("ok")])
    db = manager.projects.db(project)
    _notice(db, ocr_id)
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={"format": "odt"},
                  label="after OCR · lease-scan.pdf: export to ODT")  # fmt: skip

    async def go():  # noqa: ANN202
        await manager.send(project, LOCAL_USER, 1, "Anything new?", [])
        await manager.wait((project.id, 1))

    asyncio.run(go())
    prompt = json.dumps(handle.requests[0]["messages"], ensure_ascii=False)
    assert "export to ODT" in prompt
    assert "say it's done" not in prompt


def test_a_notice_beside_an_export_is_dropped_when_the_chat_is_next_opened_too(follow):
    manager, project, db, ocr_id, handle = follow([text_reply("Done: Lease 1998.odt is saved.")])
    _notice(db, ocr_id)
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={"format": "odt"},
                  label="after OCR · lease-scan.pdf: export to ODT")  # fmt: skip
    with db.begin() as session:
        session.get(OcrSessionRow, ocr_id).status = "done"  # finished while nobody heard

    async def go():  # noqa: ANN202
        await manager.resume_follow_ups(project, 1)
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert [m[2] for m in _transcript(db)] == ["export_run", "export.run", None]  # one report
    assert RAN == [{"chat": 1, "format": "odt"}]
    assert followups.pending(db, 1) == []


def test_only_notices_beside_a_working_follow_up_for_the_same_session_are_redundant():
    notice = followups.FollowUp("n", 7, "", {}, "say it's done")
    export = followups.FollowUp("e", 7, "export.run", {}, "export")
    other = followups.FollowUp("o", 8, "", {}, "say it's done")
    waiting = [(1, notice), (1, export), (1, other), (2, notice)]
    assert followups.redundant_notices(waiting) == [(1, notice)]


class _Summary:
    """Stands in for OcrJobs.summary: whether the session's result is final."""

    def __init__(self, open_doubts: int = 0, failed_pages: tuple = ()):
        self.open_doubts, self.failed_pages = open_doubts, list(failed_pages)

    def summary(self, project, session_id):  # noqa: ANN001, ANN201
        return {"status": "done", "open_doubts": self.open_doubts,
                "failed_pages": self.failed_pages,
                "clear": not self.open_doubts and not self.failed_pages}  # fmt: skip


HELD = (
    "OCR · lease-scan.pdf isn't finished yet: 2 open questions. Export to ODT waits for it: "
    "answer the questions in the Document, or export now as it is."
)


def _export(db, ocr_id: int, chat: int = 1):  # noqa: ANN001, ANN202
    return followups.add(db, chat, ocr_session_id=ocr_id, tool="export.run",
                         arguments={"format": "odt"}, what="export to ODT",
                         label="after OCR · lease-scan.pdf: export to ODT")  # fmt: skip


def test_an_export_waits_while_questions_are_open_and_the_chat_is_told_once(follow):
    manager, project, db, ocr_id, handle = follow([text_reply("Done: Lease 1998.odt is saved.")])
    manager.ocr_jobs = _Summary(open_doubts=2)
    _export(db, ocr_id)
    _notice(db, ocr_id)  # the hold says it instead

    async def go():  # noqa: ANN202
        await manager.after_session(project, ocr_id, "done", DONE)
        await manager.follow_ups_settled()
        await manager.after_session(project, ocr_id, "done", DONE)  # another signal
        await manager.resume_follow_ups(project, 1)  # the chat opened again
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert RAN == [] and handle.requests == []  # no export, no model
    assert _transcript(db) == [("assistant", HELD, None)]
    assert [(r["kind"], r.get("hold")) for r in _refs(db)[0]] == [("session", True)]
    (waiting,) = followups.pending(db, 1)
    assert waiting.tool == "export.run" and waiting.held


def test_once_the_document_is_clear_the_held_export_runs_and_the_model_reports(follow):
    manager, project, db, ocr_id, _ = follow([text_reply("Done: Lease 1998.odt is saved.")])
    manager.ocr_jobs = _Summary(open_doubts=2)
    _export(db, ocr_id)

    async def go():  # noqa: ANN202
        await manager.after_session(project, ocr_id, "done", DONE)
        await manager.follow_ups_settled()
        manager.ocr_jobs = _Summary()  # the last question answered
        await manager.after_session(project, ocr_id, "done", DONE)
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert RAN == [{"chat": 1, "format": "odt"}]
    assert [m[2] for m in _transcript(db)] == [None, "export_run", "export.run", None]
    assert followups.pending(db, 1) == []


def test_a_page_that_wasnt_cleaned_holds_the_export_and_is_named(follow):
    manager, project, db, ocr_id, _ = follow([])
    manager.ocr_jobs = _Summary(open_doubts=1, failed_pages=(2,))
    _export(db, ocr_id)

    async def go():  # noqa: ANN202
        await manager.after_session(project, ocr_id, "done", DONE)
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert _transcript(db) == [("assistant",
        "OCR · lease-scan.pdf isn't finished yet: 1 open question; page 2 couldn't be cleaned. "
        "Export to ODT waits for it: answer the questions in the Document and clean page 2 "
        "again in the OCR tab, or export now as it is.", None)]  # fmt: skip


def test_no_stale_hold_message_when_the_questions_are_answered_while_the_chat_is_busy(follow):
    import threading

    gate = threading.Event()
    manager, project, db, ocr_id, _ = follow(
        [text_reply("first answer"), text_reply("Done: Lease 1998.odt is saved.")], hold=gate
    )
    manager.ocr_jobs = _Summary(open_doubts=2)
    _export(db, ocr_id)

    async def go():  # noqa: ANN202
        await manager.send(project, LOCAL_USER, 1, "a question", [])
        await manager.after_session(project, ocr_id, "done", DONE)  # the hold waits for idle
        await asyncio.sleep(0.05)
        manager.ocr_jobs = _Summary()  # the last question answered meanwhile
        await manager.after_session(project, ocr_id, "done", DONE)
        gate.set()
        await manager.follow_ups_settled()

    asyncio.run(go())
    said = [r[1] for r in _transcript(db)]
    assert not any("isn't finished" in m for m in said)  # it was, by the time it could say it
    assert RAN == [{"chat": 1, "format": "odt"}]


def test_a_chat_with_only_a_notice_gets_it_with_the_questions_still_open(follow):
    manager, project, db, ocr_id, handle = follow([])
    manager.ocr_jobs = _Summary(open_doubts=2)
    _notice(db, ocr_id)

    async def go():  # noqa: ANN202
        await manager.after_session(project, ocr_id, "done", DONE)
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert _transcript(db) == [("assistant", DONE, None)] and handle.requests == []


def test_a_paused_session_keeps_its_follow_up_waiting(follow):
    manager, project, db, ocr_id, _ = follow([])
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={}, label="x")
    asyncio.run(manager.after_session(project, ocr_id, "paused", "paused"))
    assert len(followups.pending(db, 1)) == 1 and _transcript(db) == []


def test_a_busy_chat_gets_its_follow_up_once_its_turn_is_over(follow):
    import threading

    gate = threading.Event()
    manager, project, db, ocr_id, _ = follow(
        [text_reply("first answer"), text_reply("Exported.")], hold=gate
    )
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={}, label="x")

    async def go():  # noqa: ANN202
        await manager.send(project, LOCAL_USER, 1, "a question", [])
        await manager.after_session(project, ocr_id, "done", DONE)
        await asyncio.sleep(0.05)
        assert RAN == []  # the chat is still answering
        gate.set()
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert RAN == [{"chat": 1, "format": "odt"}]
    assert [r[1] for r in _transcript(db)][:2] == ["a question", "first answer"]


def test_task_after_queues_a_follow_up_and_refuses_one_that_would_ask(follow):
    manager, project, db, ocr_id, _ = follow([])
    ctx = ToolContext(project=project, sessions=db, user=LOCAL_USER, session_id=1,
                      registry=Registry([*CORE_TOOLS, ECHO, CARDED]))  # fmt: skip
    params = TASK_AFTER.validate(
        {"session": "lease-scan", "tool": "export_run", "arguments": {"format": "docx"},
         "label": "export to DOCX"}
    )  # fmt: skip

    assert TASK_AFTER.confirm
    assert TASK_AFTER.confirm_text(ctx, params) == (
        "After OCR · lease-scan.pdf finishes: export to DOCX (export.run)?"
    )
    asyncio.run(TASK_AFTER.run(ctx, params))
    (queued,) = followups.pending(db, 1)
    assert (queued.ocr_session_id, queued.tool) == (ocr_id, "export.run")
    assert queued.arguments == {"format": "docx", "name": "lease-scan/"}  # the session's folder
    assert queued.label == "after OCR · lease-scan.pdf: export to DOCX"

    for tool in ("file.wipe", "task.after", "nothing.here"):
        bad = TASK_AFTER.validate({"session": "lease-scan", "tool": tool, "label": "x"})
        with pytest.raises(ToolError):
            TASK_AFTER.confirm_text(ctx, bad)


def test_a_card_answered_once_its_session_finished_runs_the_tool_then_and_there(follow):
    """Asked while the session ran, the card was answered after it finished: the tool used
    to refuse ("already finished: run export_run now"), and the 4B had to find it itself."""
    manager, project, db, ocr_id, _ = follow([])
    file_tab = {"type": "open_tab", "tab": {"kind": "artifact", "id": 30}}

    def export(ctx: ToolContext, params: _EchoParams) -> ToolResult:  # noqa: ARG001
        RAN.append({"chat": ctx.session_id, "format": params.format})
        return ToolResult("Saved as Lease 1998.docx.", refs=[{"kind": "artifact", "id": 30}],
                          action=file_tab)  # fmt: skip

    exporting = AgentTool(name="export.run", description="Export.", params=_EchoParams, run=export)
    ctx = ToolContext(project=project, sessions=db, user=LOCAL_USER, session_id=1,
                      registry=Registry([*CORE_TOOLS, exporting]))  # fmt: skip
    params = TASK_AFTER.validate({"session": "lease-scan", "tool": "export_run",
                                  "arguments": {"format": "docx"},
                                  "label": "export to DOCX"})  # fmt: skip
    TASK_AFTER.confirm_text(ctx, params)  # the card, while the session runs
    _set_status(db, ocr_id, "done")  # it finishes before the person answers

    result = asyncio.run(TASK_AFTER.run(ctx, params))

    assert RAN == [{"chat": 1, "format": "docx"}]
    assert result.text == (
        "lease-scan.pdf had already finished, so it ran now: Saved as Lease 1998.docx."
    )
    assert (result.refs, result.action) == ([{"kind": "artifact", "id": 30}], file_tab)
    assert followups.pending(db, 1) == []  # nothing left to run when the session ends


def test_queueing_what_is_already_queued_says_so_whatever_the_arguments(follow):
    """Seen in the real run: after ocr.start(then_export=odt) the 4B called task.after with
    empty arguments again and again. "Already queued" stops it at the first try."""
    manager, project, db, ocr_id, _ = follow([])
    ctx = ToolContext(project=project, sessions=db, user=LOCAL_USER, session_id=1,
                      registry=Registry([*CORE_TOOLS, ECHO, CARDED]))  # fmt: skip
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={"format": "odt"},
                  label="after OCR · lease-scan.pdf: export to ODT")  # fmt: skip
    again = TASK_AFTER.validate({"session": "lease-scan.pdf", "tool": "export_run",
                                 "label": "export to ODT"})  # fmt: skip
    with pytest.raises(ToolError, match="Already queued: after OCR · lease-scan.pdf: export to "
                                        "ODT. Nothing more to do now"):  # fmt: skip
        TASK_AFTER.confirm_text(ctx, again)
    assert len(followups.pending(db, 1)) == 1


def _set_status(db, ocr_id: int, status: str) -> None:  # noqa: ANN001
    with db.begin() as session:
        session.get(OcrSessionRow, ocr_id).status = status


def test_a_follow_up_whose_session_finished_unseen_runs_when_the_chat_is_next_used(follow):
    """The app stopped between the session finishing and the follow-up running (or the
    signal was lost): nothing would ever trigger it again."""
    manager, project, db, ocr_id, _ = follow([text_reply("Exported.")])
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={"format": "odt"},
                  label="after OCR · lease-scan.pdf: export to ODT")  # fmt: skip
    _set_status(db, ocr_id, "done")

    async def go():  # noqa: ANN202
        await manager.resume_follow_ups(project, 1)
        await manager.resume_follow_ups(project, 1)  # opened twice: still once
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert RAN == [{"chat": 1, "format": "odt"}] and followups.pending(db, 1) == []
    assert _transcript(db)[0][1] == "OCR · lease-scan.pdf finished."


def test_a_follow_up_whose_session_is_gone_is_dropped_and_one_still_running_waits(follow):
    manager, project, db, ocr_id, _ = follow([text_reply("It can't run now.")])
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={}, label="waits")
    followups.add(db, 1, ocr_session_id=999, tool="export.run", arguments={}, label="gone one")

    async def go():  # noqa: ANN202
        await manager.resume_follow_ups(project, 1)
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert RAN == [] and [f.label for f in followups.pending(db, 1)] == ["waits"]
    assert _transcript(db)[0][1] == "The OCR session is gone. The follow-up (gone one) was dropped."


def test_a_stale_follow_up_also_runs_after_the_chats_next_turn(follow):
    manager, project, db, ocr_id, _ = follow([text_reply("Hello."), text_reply("Exported.")])
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={}, label="x")
    _set_status(db, ocr_id, "done")

    async def go():  # noqa: ANN202
        await manager.send(project, LOCAL_USER, 1, "hi", [])
        await manager.follow_ups_settled()

    asyncio.run(go())
    assert RAN == [{"chat": 1, "format": "odt"}]
    assert [r[1] for r in _transcript(db)][:2] == ["hi", "Hello."]


def test_a_follow_up_whose_tool_breaks_ends_its_turn_with_the_error(follow, store):
    manager, project, db, ocr_id, _ = follow([])

    def broken(ctx, params):  # noqa: ANN001, ANN202
        raise OSError("disk full")

    manager._registry = lambda: Registry([*CORE_TOOLS, AgentTool(
        name="export.run", description="Export.", params=_EchoParams, run=broken)])  # fmt: skip
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={}, label="x")

    async def go():  # noqa: ANN202
        await manager.after_session(project, ocr_id, "done", DONE)
        await manager.follow_ups_settled()

    asyncio.run(go())
    events = manager.channel((project.id, 1)).events
    assert {"type": "error", "detail": "The follow-up couldn't run: disk full"}.items() <= events[
        -2
    ].items()
    assert events[-1]["type"] == "turn" and events[-1]["status"] == "error"
    assert manager.state((project.id, 1)) == "idle"


@pytest.mark.parametrize("named", [None, "lease-scan.pdf"])
def test_an_export_after_ocr_without_the_documents_name_gets_the_sessions_folder(follow, named):
    """Seen on the test server: asked to "read the scan and prepare DOCX", the 4B queued
    task.after(export_run, arguments without a name) — it can't know the session's folder —
    got "name: Field required" twice, and gave up: nothing followed the OCR. A name that is
    the scan itself is the same mistake."""
    from lectern.agent.export_tools import export_tools
    from lectern.modules import load_modules, module_exporters

    manager, project, db, ocr_id, _ = follow([])
    real = Registry([*CORE_TOOLS, *export_tools(module_exporters(load_modules()))])
    ctx = ToolContext(project=project, sessions=db, user=LOCAL_USER, session_id=1, registry=real)
    arguments = {"format": "docx"} | ({"name": named} if named else {})
    params = TASK_AFTER.validate({"session": "lease-scan.pdf", "tool": "export_run",
                                  "arguments": arguments, "label": "export to DOCX"})  # fmt: skip

    TASK_AFTER.confirm_text(ctx, params)  # no "Field required"
    asyncio.run(TASK_AFTER.run(ctx, params))

    (queued,) = followups.pending(db, 1)
    assert queued.arguments == {"name": "lease-scan/", "format": "docx"}


def test_an_export_after_ocr_without_a_format_says_how_to_give_one(follow):
    """Seen in the real check: task_after(export_run, arguments={}) twice, each answered by
    pydantic's bare "format: Field required", before the 4B tried {"format": "docx"}."""
    from lectern.agent.export_tools import export_tools
    from lectern.modules import load_modules, module_exporters

    manager, project, db, ocr_id, _ = follow([])
    real = Registry([*CORE_TOOLS, *export_tools(module_exporters(load_modules()))])
    ctx = ToolContext(project=project, sessions=db, user=LOCAL_USER, session_id=1, registry=real)
    params = TASK_AFTER.validate({"session": "lease-scan.pdf", "tool": "export_run",
                                  "arguments": {}, "label": "export to DOCX"})  # fmt: skip

    with pytest.raises(ToolError, match=r'arguments=\{"format": "docx"\}'):
        TASK_AFTER.confirm_text(ctx, params)


def test_the_runtime_hears_of_turns_and_only_a_persons_message_counts_as_one(follow):
    """The runtime holds jobs while a turn runs, and brings back the idle window only for
    the person's messages; a follow-up the app runs is a turn, not a message."""
    manager, project, db, ocr_id, _ = follow(
        [text_reply("Done: Lease 1998.odt is saved."), text_reply("Hello.")]
    )
    followups.add(db, 1, ocr_session_id=ocr_id, tool="export.run", arguments={"format": "odt"},
                  label="after OCR · lease-scan.pdf: export to ODT")  # fmt: skip
    runtime = manager.runtime
    seen: list[tuple[int, float | None]] = []

    async def go():  # noqa: ANN202
        await manager.after_session(project, ocr_id, "done", DONE)
        await manager.follow_ups_settled()
        seen.append((runtime._turns, runtime._last_message_at))  # the follow-up's turn
        await manager.send(project, LOCAL_USER, 1, "hi", [])
        await manager.follow_ups_settled()
        seen.append((runtime._turns, runtime._last_message_at))

    asyncio.run(go())
    (after_follow_up, after_message) = seen
    assert after_follow_up == (0, None)  # ran and ended; not a message
    assert after_message[0] == 0 and after_message[1] is not None
