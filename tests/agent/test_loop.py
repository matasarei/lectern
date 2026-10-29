from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from lectern.agent import loop
from lectern.agent.core_tools import CORE_TOOLS, core_registry
from lectern.agent.tools import AgentTool, Registry, ToolContext, ToolError, ToolResult
from lectern.agent.turns import Busy, TurnManager
from lectern.core.auth import LOCAL_USER
from lectern.projects import files
from lectern.projects.tables import ConfirmationRow, MessageRow, SessionRow

from .fake_model import FakeHandle, make_runtime, sse, text_reply, tool_reply


@pytest.fixture
def setup(store):
    def make(replies, **kwargs):
        handle = FakeHandle(replies, **kwargs)
        runtime = make_runtime(handle)
        manager = TurnManager(runtime, store, core_registry())
        project = store.create(LOCAL_USER, "Lease archive")
        return manager, project, handle, runtime

    return make


def add_file(store, project, name, text):
    with store.db(project).begin() as session:
        return files.write_text(
            project.root, session, owner_id=LOCAL_USER.id, name=name, text=text,
            origin=files.Origin("import"),
        ).id  # fmt: skip


def transcript(store, project):
    with store.db(project)() as session:
        rows = session.scalars(select(MessageRow).order_by(MessageRow.id)).all()
    return [(r.role, r.content, r.tool_name) for r in rows]


async def turn(manager, project, text, attachments=(), session_id=1):
    await manager.send(project, LOCAL_USER, session_id, text, list(attachments))
    return await manager.wait((project.id, session_id))


async def until(condition, timeout=5.0):
    """Wait for `condition()` to hold; fail instead of hanging when it never does."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0.01)


async def recorded(manager, project, text, session_id=1):
    """Run a turn while listening, as a page does: every event, as it was sent."""
    seen = []
    async with manager.channel((project.id, session_id)).listen() as (_, queue):
        status = await turn(manager, project, text, session_id=session_id)
        while not queue.empty():
            seen.append(queue.get_nowait())
    return status, seen


def events(manager, project, session_id=1):
    return manager.channel((project.id, session_id)).events


def test_a_tool_call_then_an_answer(setup, store):
    manager, project, handle, _ = setup(
        [tool_reply("project_find", {"query": "lease"}), text_reply("It's ", "lease.md.")]
    )
    add_file(store, project, "lease.md", "# Terms\nPay by the 10th.")

    status, seen = asyncio.run(recorded(manager, project, "Is there a lease file?"))

    assert status == "done"
    kinds = [e["type"] for e in seen]
    assert kinds == [
        "turn", "model", "usage", "assistant", "tool", "tool_result",
        "usage", "delta", "delta", "assistant", "done", "turn",
    ]  # fmt: skip
    tool = next(e for e in seen if e["type"] == "tool")
    assert (tool["name"], json.loads(tool["arguments"])) == ("project.find", {"query": "lease"})
    result = next(e for e in seen if e["type"] == "tool_result")
    # The chat is already named after the message, so it matches "lease" too.
    assert "lease.md · file" in result["text"]
    assert [r["name"] for r in result["refs"]] == ["Is there a lease file?", "lease.md"]
    assert [(r, c[:12], n) for r, c, n in transcript(store, project)] == [
        ("user", "Is there a l", None),
        ("assistant", "", None),
        ("tool", "Is there a l", "project.find"),
        ("assistant", "It's lease.m", None),
    ]
    # The second request carried the tool result, and the tools went with every request.
    second = handle.requests[1]
    assert second["messages"][-1]["role"] == "tool"
    assert second["messages"][-2]["tool_calls"][0]["function"]["name"] == "project_find"
    assert {t["function"]["name"] for t in second["tools"]} >= {"project_find", "document_read"}
    assert [e["seq"] for e in seen] == list(range(1, 13))


def test_the_chat_and_an_untitled_project_are_named_from_the_first_message(setup, store):
    manager, _, _, _ = setup([text_reply("Hi")])
    project = store.create(LOCAL_USER)  # "Untitled project"
    asyncio.run(turn(manager, project, "Who are the parties in the 1998 lease contract?"))

    with store.db(project)() as session:
        assert session.get(SessionRow, 1).title == "Who are the parties in the…"
    assert store.get(LOCAL_USER, project.id).name == "Who are the parties in the…"


def test_trash_waits_for_allow_without_holding_the_model(setup, store):
    manager, project, _, runtime = setup(
        [tool_reply("artifact_trash", {"name": "old.md"}), text_reply("Done.")]
    )
    add_file(store, project, "old.md", "old")

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "Delete old.md", [])
        key = (project.id, 1)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if manager.state(key) == "waiting":
                break
        assert manager.state(key) == "waiting"
        assert (project.root / "old.md").exists()
        assert all(r["leases"] == 0 for r in runtime.state()["residents"])
        card = next(e for e in events(manager, project) if e["type"] == "confirm")
        assert card["text"] == "Move old.md to the trash?"
        assert manager.decide(key, card["confirmation_id"], True)
        assert not manager.decide(key, card["confirmation_id"], True)  # already answered
        return await manager.wait(key)

    assert asyncio.run(scenario()) == "done"
    assert not (project.root / "old.md").exists()
    with store.db(project)() as session:
        assert session.scalars(select(ConfirmationRow.status)).all() == ["approved"]


def test_a_turn_waiting_for_a_card_doesnt_count_as_a_chat_writing(setup, store):
    """Jobs held for the chat go while the person reads a card (the model is idle), and the
    turn counts again once answered, or stopped."""
    manager, project, _, runtime = setup(
        [
            tool_reply("artifact_trash", {"name": "old.md"}),
            text_reply("Done."),
            tool_reply("artifact_trash", {"name": "older.md"}),
        ]  # fmt: skip
    )
    add_file(store, project, "old.md", "old")
    add_file(store, project, "older.md", "older")

    async def scenario():  # noqa: ANN202
        key = (project.id, 1)
        await manager.send(project, LOCAL_USER, 1, "Delete old.md", [])
        await until(lambda: manager.state(key) == "waiting")
        during = runtime.chat_idle_seconds()
        card = next(e for e in events(manager, project) if e["type"] == "confirm")
        manager.decide(key, card["confirmation_id"], True)
        await manager.wait(key)
        await manager.send(project, LOCAL_USER, 1, "And again", [])
        await until(lambda: manager.state(key) == "waiting")
        await manager.stop(key)  # cancels the card
        await manager.wait(key)
        # Nothing is left counted as waiting: the next turn is writing again
        await runtime.turn_started(message=False)
        next_turn = runtime.chat_idle_seconds()
        await runtime.turn_ended()
        return during, next_turn

    during, next_turn = asyncio.run(scenario())
    assert during > 0  # not "writing" while the card waits
    assert next_turn == 0  # after an answer and a stop, a new turn counts as writing


def test_dont_allow_tells_the_model_and_moves_nothing(setup, store):
    manager, project, handle, _ = setup(
        [tool_reply("artifact_trash", {"name": "old.md"}), text_reply("Kept it.")]
    )
    add_file(store, project, "old.md", "old")

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "Delete old.md", [])
        key = (project.id, 1)
        await until(lambda: manager.state(key) == "waiting")
        card = next(e for e in events(manager, project) if e["type"] == "confirm")
        assert card["kind"] == "allow"  # a tool's own card: Allow / Don't allow
        manager.decide(key, card["confirmation_id"], False)
        return await manager.wait(key)

    assert asyncio.run(scenario()) == "done"
    assert (project.root / "old.md").exists()
    assert handle.requests[1]["messages"][-1]["content"].startswith("The person declined")


def test_bad_arguments_are_sent_back_once_then_the_turn_ends(setup, store):
    manager, project, handle, _ = setup(
        [tool_reply("project_find", {"q": "x"}), tool_reply("project_find", {"q": "y"}, "call_2")]
    )

    assert asyncio.run(turn(manager, project, "Find x")) == "error"

    retry = handle.requests[1]["messages"][-1]["content"]
    assert retry.startswith("Error: Invalid arguments for project.find: query: Field required")
    error = next(e for e in events(manager, project) if e["type"] == "error")
    assert "Invalid arguments for project.find" in error["detail"]


def test_an_unknown_tool_gets_the_list_of_tools(setup):
    manager, project, handle, _ = setup([tool_reply("shell_exec", {"cmd": "ls"}), text_reply("ok")])
    assert asyncio.run(turn(manager, project, "ls")) == "done"
    assert "there is no tool 'shell_exec'" in handle.requests[1]["messages"][-1]["content"]


class _OpenParams(BaseModel):
    url: str


def test_a_call_that_failed_isnt_run_or_asked_about_again_in_the_turn(store):
    opened = []

    def open_page(ctx, params):  # noqa: ANN001, ANN202
        opened.append(params.url)
        if params.url == "https://refusing.example/":
            raise ToolError("refusing.example answered 403.")
        return ToolResult("Saved.")

    tool = AgentTool(
        name="demo.open", description="Open a page.", params=_OpenParams, run=open_page,
        confirm=True, confirm_text=lambda ctx, params: f"Open {params.url}?",
    )  # fmt: skip
    refusing, other = {"url": "https://refusing.example/"}, {"url": "https://other.example/"}
    handle = FakeHandle([
        tool_reply("demo_open", refusing, "call_1"),
        tool_reply("demo_open", refusing, "call_2"),
        tool_reply("demo_open", other, "call_3"),
        text_reply("Done."),
    ])  # fmt: skip
    manager = TurnManager(make_runtime(handle), store, Registry([*CORE_TOOLS, tool]))
    project = store.create(LOCAL_USER, "Pages")

    async def scenario():  # noqa: ANN202
        key = (project.id, 1)
        await manager.send(project, LOCAL_USER, 1, "open them", [])
        answered = set()
        deadline = asyncio.get_running_loop().time() + 5
        while manager.state(key) != "idle":
            assert asyncio.get_running_loop().time() < deadline, "the turn didn't end"
            for card in [e for e in events(manager, project) if e["type"] == "confirm"]:
                if card["confirmation_id"] not in answered:
                    answered.add(card["confirmation_id"])
                    manager.decide(key, card["confirmation_id"], True)
            await asyncio.sleep(0.01)
        return await manager.wait(key)

    assert asyncio.run(scenario()) == "done"
    assert opened == ["https://refusing.example/", "https://other.example/"]
    cards = [e["text"] for e in events(manager, project) if e["type"] == "confirm"]
    assert cards == ["Open https://refusing.example/?", "Open https://other.example/?"]
    again = handle.requests[2]["messages"][-1]["content"]
    assert again == (
        "Error: demo.open already failed with these arguments in this answer: "
        "refusing.example answered 403. Don't call it with them again: use what you have, "
        "or say it didn't work."
    )


def test_a_turn_stops_after_eight_model_calls(setup):
    replies = [tool_reply("project_list", {}, f"call_{i}") for i in range(8)]
    manager, project, handle, _ = setup(replies)

    assert asyncio.run(turn(manager, project, "loop")) == "error"
    assert len(handle.requests) == 8
    assert "Stopped after 8 model calls" in events(manager, project)[-2]["detail"]


def test_stop_keeps_what_was_written(setup, store):
    hold = asyncio.Event()
    manager, project, _, _ = setup([text_reply("never")], hold=hold)

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "Write a long letter", [])
        key = (project.id, 1)
        await asyncio.sleep(0.05)
        with pytest.raises(Busy):
            await manager.send(project, LOCAL_USER, 1, "again", [])
        assert await manager.stop(key)
        return manager.state(key)

    assert asyncio.run(scenario()) == "idle"
    assert [e["type"] for e in events(manager, project)][-2:] == ["stopped", "turn"]
    assert events(manager, project)[-1]["status"] == "stopped"
    assert transcript(store, project) == [("user", "Write a long letter", None)]


def test_stop_during_a_reply_stores_the_partial_text(setup, store, monkeypatch):
    manager, project, _, _ = setup([])

    async def slow_stream(run, client, body):  # noqa: ANN001
        run.emit({"type": "delta", "text": "Dear tenant, "})
        await asyncio.sleep(10)

    monkeypatch.setattr(loop, "_stream", slow_stream)

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "Write a letter", [])
        await until(lambda: any(e["type"] == "delta" for e in events(manager, project)))
        await manager.stop((project.id, 1))

    asyncio.run(scenario())
    assert transcript(store, project)[-1] == ("assistant", "Dear tenant, ", None)


def test_attached_files_go_to_the_model(setup, store):
    manager, project, handle, _ = setup([text_reply("Summary.")])
    notes = add_file(store, project, "notes.md", "# Payment\nBy the 10th of each month.")

    asyncio.run(turn(manager, project, "Summarise this", attachments=[notes, 999]))

    system = handle.requests[0]["messages"][0]["content"]
    assert "## notes.md\n### Payment\nBy the 10th of each month." in system
    with store.db(project)() as session:
        user_row = session.scalars(select(MessageRow).where(MessageRow.role == "user")).one()
    assert json.loads(user_row.attachments) == [notes]  # 999 isn't a file here


def test_model_errors_end_the_turn_with_a_message(setup):
    manager, project, _, _ = setup([b"data: {not json\n\n"])
    assert asyncio.run(turn(manager, project, "hi")) == "error"
    assert events(manager, project)[-2] == {
        "type": "error", "detail": "The model server sent a malformed reply.",
        "seq": events(manager, project)[-2]["seq"],
    }  # fmt: skip


def test_no_card_for_a_partial_name_and_nothing_moves(setup, store):
    manager, project, handle, _ = setup(
        [tool_reply("artifact_trash", {"name": "notes"}), text_reply("Which file?")]
    )
    add_file(store, project, "meeting-notes-1998.md", "notes")

    assert asyncio.run(turn(manager, project, "Delete the notes")) == "done"

    assert not any(e["type"] == "confirm" for e in events(manager, project))
    assert handle.requests[1]["messages"][-1]["content"].startswith(
        "Error: No file called exactly 'notes'"
    )
    assert (project.root / "meeting-notes-1998.md").exists()


def test_the_card_names_the_file_as_it_is_stored(setup, store):
    manager, project, _, _ = setup(
        [tool_reply("artifact_trash", {"name": "OLD.MD"}), text_reply("Done.")]
    )
    add_file(store, project, "old.md", "old")

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "Delete OLD.MD", [])
        key = (project.id, 1)
        await until(lambda: manager.state(key) == "waiting")
        card = next(e for e in events(manager, project) if e["type"] == "confirm")
        manager.decide(key, card["confirmation_id"], False)
        await manager.wait(key)
        return card["text"]

    assert asyncio.run(scenario()) == "Move old.md to the trash?"


def test_two_sends_at_once_start_only_one_turn(setup, store):
    manager, project, handle, _ = setup([text_reply("One answer.")])

    async def scenario():
        results = await asyncio.gather(
            manager.send(project, LOCAL_USER, 1, "first", []),
            manager.send(project, LOCAL_USER, 1, "second", []),
            return_exceptions=True,
        )
        await manager.wait((project.id, 1))
        return results

    results = asyncio.run(scenario())
    assert sum(isinstance(r, Busy) for r in results) == 1
    assert len(handle.requests) == 1
    assert [row[0] for row in transcript(store, project)] == ["user", "assistant"]


def test_a_failed_send_leaves_the_chat_free(setup, store, monkeypatch):
    manager, project, _, _ = setup([text_reply("ok")])
    real = loop.store_message
    calls = {"n": 0}

    def failing_once(run, **fields):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full")
        return real(run, **fields)

    monkeypatch.setattr("lectern.agent.turns.store_message", failing_once)

    async def scenario():
        with pytest.raises(RuntimeError):
            await manager.send(project, LOCAL_USER, 1, "first", [])
        assert manager.state((project.id, 1)) == "idle"
        await manager.send(project, LOCAL_USER, 1, "again", [])
        return await manager.wait((project.id, 1))

    assert asyncio.run(scenario()) == "done"


def test_a_finished_turn_keeps_no_streamed_text_in_memory(setup, store):
    manager, project, _, _ = setup([text_reply(*(["word "] * 200))])

    assert asyncio.run(turn(manager, project, "Write a lot")) == "done"

    kept = events(manager, project)
    assert not any(e["type"] == "delta" for e in kept)
    assert [e["type"] for e in kept] == ["turn", "model", "usage", "assistant", "done", "turn"]
    assert transcript(store, project)[-1] == ("assistant", "word " * 200, None)


class _AskParams(BaseModel):
    what: str


async def _ask_then_work(ctx, params):  # noqa: ANN001
    ctx.emit({"type": "progress", "label": "preparing", "done": 1, "total": 2})
    if not await ctx.confirm(f"Prepare {params.what}?"):
        return ToolResult("The person said no.")
    await asyncio.sleep(0)
    ctx.emit({"type": "progress", "label": "preparing", "done": 2, "total": 2})
    return ToolResult(f"Prepared {params.what}; runtime given: {ctx.runtime is not None}.")


ASK_TOOL = AgentTool(
    name="demo.prepare", description="Prepare something.", params=_AskParams, run=_ask_then_work
)


@pytest.mark.parametrize("approve", [True, False])
def test_an_async_tool_can_report_progress_and_ask_mid_run(store, approve):
    handle = FakeHandle([tool_reply("demo_prepare", {"what": "the image"}), text_reply("ok")])
    manager = TurnManager(make_runtime(handle), store, Registry([*CORE_TOOLS, ASK_TOOL]))
    project = store.create(LOCAL_USER, "Async")

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "prepare", [])
        key = (project.id, 1)
        await until(lambda: manager.state(key) == "waiting")
        card = next(e for e in events(manager, project) if e["type"] == "confirm")
        assert card["text"] == "Prepare the image?"
        manager.decide(key, card["confirmation_id"], approve)
        return await manager.wait(key)

    assert asyncio.run(scenario()) == "done"
    progress = [e for e in events(manager, project) if e["type"] == "progress"]
    assert progress[0]["done"] == 1 and progress[0]["call_id"] == "call_1"
    result = handle.requests[1]["messages"][-1]["content"]
    if approve:
        assert result == "Prepared the image; runtime given: True."
        assert [p["done"] for p in progress] == [1, 2]
    else:
        assert result == "The person said no."


class _NoParams(BaseModel):
    pass


async def _write_in_the_wrong_code_page(ctx, params):  # noqa: ANN001
    # What code_write did on a cp1251 Windows with "₴" in the script (docs/acceptance/m4.md).
    raise UnicodeEncodeError("charmap", "Сума: 409,62 ₴", 13, 14, "character maps to <undefined>")


BROKEN_TOOL = AgentTool(
    name="demo.broken", description="Fails as nothing expects.", params=_NoParams,
    run=_write_in_the_wrong_code_page,
)  # fmt: skip


def test_a_tool_failing_as_nothing_expects_ends_the_turn_and_says_so(store, caplog):
    """It ended the turn's task with nothing said: "Working…" for good, Stop and a reload
    couldn't end it, and no message could be sent. And the call has its result, the error,
    so the next turn's model knows it failed."""
    handle = FakeHandle([tool_reply("demo_broken", {}), text_reply("Next one.")])
    manager = TurnManager(make_runtime(handle), store, Registry([*CORE_TOOLS, BROKEN_TOOL]))
    project = store.create(LOCAL_USER, "Broken")

    status, seen = asyncio.run(recorded(manager, project, "Compute it"))

    assert status == "error"
    error = next(e for e in seen if e["type"] == "error")
    assert error["detail"].startswith("Something went wrong (UnicodeEncodeError: 'charmap' codec")
    assert error["detail"].endswith("It's in the log.")
    assert seen[-1]["type"] == "turn" and seen[-1]["status"] == "error"  # the page hears it end
    logged = [r for r in caplog.records if r.name == "lectern.agent.loop"]
    assert (
        logged and logged[0].exc_info[0] is UnicodeEncodeError
    )  # the traceback, for whoever looks
    result = next(e for e in seen if e["type"] == "tool_result")
    assert result["error"] and result["text"].startswith("Error: UnicodeEncodeError: 'charmap'")
    assert [(role, name) for role, _, name in transcript(store, project)] == [
        ("user", None), ("assistant", None), ("tool", "demo.broken"),
    ]  # fmt: skip
    # The chat isn't stuck: the next message gets an answer, and its model sees the error.
    assert asyncio.run(turn(manager, project, "Again")) == "done"
    tool_messages = [m for m in handle.requests[-1]["messages"] if m["role"] == "tool"]
    assert tool_messages[-1]["content"].startswith("Error: UnicodeEncodeError")


def test_a_step_that_isnt_a_tool_failing_ends_the_turn_and_says_so(setup, monkeypatch):
    """Any step of run_turn, which follow-ups run too: the same end, not a turn left running."""
    manager, project, _, _ = setup([text_reply("ok")])

    def lost(_run):  # noqa: ANN001, ANN202
        raise KeyError("x")

    monkeypatch.setattr(loop, "load_history", lost)
    assert asyncio.run(turn(manager, project, "hi")) == "error"
    error = next(e for e in events(manager, project) if e["type"] == "error")
    assert error["detail"] == "Something went wrong (KeyError: 'x'). It's in the log."
    assert events(manager, project)[-1]["type"] == "turn"


def test_a_tool_outside_a_turn_is_never_approved(store):
    project = store.create(LOCAL_USER, "Direct")
    ctx = ToolContext(project=project, sessions=store.db(project), user=LOCAL_USER, session_id=1)
    result = asyncio.run(_ask_then_work(ctx, _AskParams(what="x")))
    assert result.text == "The person said no."


def test_a_slow_tool_list_doesnt_delay_sending(store):
    import threading

    release = threading.Event()

    def slow_registry():
        release.wait(timeout=5)  # a cold `podman info`
        return core_registry()

    handle = FakeHandle([text_reply("ok")])
    manager = TurnManager(make_runtime(handle), store, slow_registry)
    project = store.create(LOCAL_USER, "Slow")

    async def scenario():
        started = asyncio.get_running_loop().time()
        await manager.send(project, LOCAL_USER, 1, "hi", [])
        sent_in = asyncio.get_running_loop().time() - started
        assert manager.state((project.id, 1)) == "running"
        release.set()
        return sent_in, await manager.wait((project.id, 1))

    sent_in, status = asyncio.run(scenario())
    assert sent_in < 1 and status == "done"


async def opened_tabs(manager, project, run):
    """What the project page hears while `run()` happens."""
    async with manager.project_channel(project.id).listen() as (_, queue):
        await run()
        return [queue.get_nowait() for _ in range(queue.qsize())]


def test_a_tab_a_chat_opens_reaches_the_project_page_too(setup, store):
    manager, project, _, _ = setup(
        [tool_reply("tab_open", {"name": "notes.md"}), text_reply("Opened.")]
    )
    file_id = add_file(store, project, "notes.md", "n")

    heard = asyncio.run(opened_tabs(manager, project, lambda: turn(manager, project, "Open it")))

    tab = {"kind": "artifact", "id": file_id}
    assert heard == [{"type": "open_tab", "session_id": 1, "tab": tab, "seq": 1}]
    assert manager.project_channel(project.id).events == []  # live only: nothing to replay


def test_only_open_tab_actions_reach_the_project_page(setup):
    manager, project, _, _ = setup([])
    chat = manager.channel((project.id, 2))
    tab = {"kind": "artifact", "id": 5}

    async def publish():
        chat.publish({"type": "delta", "text": "x"})
        chat.publish({"type": "tool_result", "call_id": "a", "action": None})
        chat.publish({"type": "tool_result", "call_id": "b", "action": {"type": "other"}})
        chat.publish({"type": "tool_result", "call_id": "c",
                      "action": {"type": "open_tab", "tab": tab, "focus": False}})  # fmt: skip

    heard = asyncio.run(opened_tabs(manager, project, publish))

    assert heard == [{"type": "open_tab", "session_id": 2, "tab": tab, "seq": 1}]
    assert len(chat.events) == 4  # the chat's own stream keeps everything, as before


def test_a_failure_passing_an_event_on_does_not_break_the_chats_own_stream(caplog):
    from lectern.agent.turns import Channel

    def broken(_event):  # noqa: ANN001, ANN202
        raise KeyError("tab")

    chat = Channel(forward=broken)

    async def publish():
        async with chat.listen() as (_, queue):
            chat.publish({"type": "tool_result", "call_id": "a"})
            return queue.get_nowait()

    heard = asyncio.run(publish())

    assert heard == {"type": "tool_result", "call_id": "a", "seq": 1}
    assert chat.events == [heard]
    assert "Couldn't pass a chat event on to its project" in caplog.text


# --- the reply's speed ------------------------------------------------------------------------


def _ticking_clock(monkeypatch):
    """Every look at the clock is one second after the last: time passes, the same way each run."""
    now = iter(range(1, 10_000))
    monkeypatch.setattr(loop.time, "perf_counter", lambda: float(next(now)))


def _done(manager, project):
    return next(e for e in events(manager, project) if e["type"] == "done")


def test_a_reply_has_a_speed_when_the_model_server_reports_none(setup, monkeypatch):
    # MLX's server gives the token count and no timings; llama.cpp gives both.
    _ticking_clock(monkeypatch)
    manager, project, _, _ = setup([text_reply("The", " lease", " runs", " from", " March.")])
    assert asyncio.run(turn(manager, project, "When does the lease start?")) == "done"
    done = _done(manager, project)
    assert done["tokens"] == 5
    # Counted here: the tokens after the first, over the time since it arrived.
    assert done["tps"] is not None and 0 < done["tps"] <= 4


def test_the_model_servers_own_speed_is_kept_when_it_gives_one(setup, monkeypatch):
    _ticking_clock(monkeypatch)
    reply = sse(
        {"choices": [{"delta": {"content": "March."}}]},
        {
            "choices": [],
            "usage": {"completion_tokens": 2},
            "timings": {"predicted_per_second": 42.5},
        },
    )
    manager, project, _, _ = setup([reply])
    assert asyncio.run(turn(manager, project, "When?")) == "done"
    assert _done(manager, project)["tps"] == 42.5


def test_a_reply_of_one_token_claims_no_speed(setup, monkeypatch):
    _ticking_clock(monkeypatch)
    manager, project, _, _ = setup([text_reply("Yes.")])
    assert asyncio.run(turn(manager, project, "Is it signed?")) == "done"
    assert _done(manager, project)["tps"] is None  # nothing to time between tokens
