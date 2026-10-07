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
    TASK_AFTER.run(ctx, params)
    (queued,) = followups.pending(db, 1)
    assert (queued.ocr_session_id, queued.tool) == (ocr_id, "export.run")
    assert queued.arguments == {"format": "docx"}
    assert queued.label == "after OCR · lease-scan.pdf: export to DOCX"

    for tool in ("file.wipe", "task.after", "nothing.here"):
        bad = TASK_AFTER.validate({"session": "lease-scan", "tool": tool, "label": "x"})
        with pytest.raises(ToolError):
            TASK_AFTER.confirm_text(ctx, bad)
