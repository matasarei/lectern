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
    assert queued.arguments == {"format": "docx", "name": "lease-scan/"}  # the session's folder
    assert queued.label == "after OCR · lease-scan.pdf: export to DOCX"

    for tool in ("file.wipe", "task.after", "nothing.here"):
        bad = TASK_AFTER.validate({"session": "lease-scan", "tool": tool, "label": "x"})
        with pytest.raises(ToolError):
            TASK_AFTER.confirm_text(ctx, bad)


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
    TASK_AFTER.run(ctx, params)

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
