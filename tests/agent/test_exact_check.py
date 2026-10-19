"""The exact-results check (docs/acceptance/m1b.md): with the sandbox running, a reply to a
message with a number in it, given without any tool, is held back and the model is asked
once whether it states an exact result. If it does, it computes it with code_write and
code_run, and the draft is never shown; if not (NO_CALC), the draft is the answer."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel
from sqlalchemy import select

from lectern.agent import loop
from lectern.agent.core_tools import CORE_TOOLS
from lectern.agent.prompts import EXACT_CHECK
from lectern.agent.tools import AgentTool, Registry, ToolContext, ToolResult
from lectern.agent.turns import TurnManager
from lectern.core.auth import LOCAL_USER
from lectern.projects.tables import MessageRow

from .fake_model import FakeHandle, make_runtime, sse, text_reply, tool_reply

RAN: list[str] = []


def transcript(store, project):  # noqa: ANN001, ANN201
    with store.db(project)() as session:
        rows = session.scalars(select(MessageRow).order_by(MessageRow.id)).all()
    return [(r.role, r.content, r.tool_name) for r in rows]


async def turn(manager, project, text):  # noqa: ANN001, ANN201
    await manager.send(project, LOCAL_USER, 1, text, [])
    return await manager.wait((project.id, 1))


async def until(condition, timeout: float = 5.0) -> None:  # noqa: ANN001
    """Wait for `condition()` to hold; fail instead of hanging when it never does."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0.01)


def _ticking_clock(monkeypatch) -> None:  # noqa: ANN001
    """Every look at the clock is one second after the last, the same way each run."""
    now = iter(range(1, 10_000))
    monkeypatch.setattr(loop.time, "perf_counter", lambda: float(next(now)))


class _Brief(BaseModel):
    brief: str = ""


def _ran(name: str, text: str):  # noqa: ANN202
    def run(ctx: ToolContext, params: _Brief) -> ToolResult:  # noqa: ARG001
        RAN.append(name)
        return ToolResult(text)

    return run


# Stand-ins for the sandbox module's tools: offered only while a container engine runs.
CODE = [
    AgentTool(name="code.write", description="Write a script.", params=_Brief,
              run=_ran("code.write", "Saved calc.py; its test passed.")),
    AgentTool(name="code.run", description="Run the script.", params=_Brief,
              run=_ran("code.run", "4172.04")),
]  # fmt: skip


def _manager(store, replies, *, sandbox=True, **kwargs):  # noqa: ANN001, ANN202
    RAN.clear()
    handle = FakeHandle(replies, **kwargs)
    tools = [*CORE_TOOLS, *CODE] if sandbox else list(CORE_TOOLS)
    manager = TurnManager(make_runtime(handle), store, Registry(tools))
    return manager, store.create(LOCAL_USER, "Sums"), handle


def _run(manager, project, text):  # noqa: ANN001, ANN202
    """The turn's status and every event it sent, as a page hears them."""

    async def scenario():  # noqa: ANN202
        async with manager.channel((project.id, 1)).listen() as (_, queue):
            status = await turn(manager, project, text)
            return status, [queue.get_nowait() for _ in range(queue.qsize())]

    return asyncio.run(scenario())


def _shown(seen: list[dict]) -> str:
    return "".join(e["text"] for e in seen if e["type"] == "delta")


SUM = "What is the sum of 1284.50, 377.25, 99.99 and 2410.30?"


def test_a_sum_answered_from_memory_is_computed_instead_and_the_draft_never_shown(store):
    manager, project, handle = _manager(store, [
        text_reply("That's ", "4172.03."),  # from memory, and wrong
        tool_reply("code_write", {"brief": "Add 1284.50, 377.25, 99.99 and 2410.30."}),
        tool_reply("code_run", {}, call_id="call_2"),
        text_reply("The sum is ", "**4172.04**."),
    ])  # fmt: skip

    status, seen = _run(manager, project, SUM)

    assert status == "done"
    assert RAN == ["code.write", "code.run"]
    assert _shown(seen) == "The sum is **4172.04**."
    assert "4172.03" not in str(seen) and "4172.03" not in str(transcript(store, project))
    check = handle.requests[1]["messages"]
    assert check[-2] == {"role": "assistant", "content": "That's 4172.03."}
    assert check[-1] == {"role": "user", "content": EXACT_CHECK}
    assert EXACT_CHECK not in str(transcript(store, project))  # asked, never stored


def test_no_calculation_here_keeps_the_draft_as_the_answer(store):
    question = "In 2 sentences: what was the Peace of Westphalia?"
    draft = "It ended the Thirty Years' War, signed in 1648."
    manager, project, handle = _manager(store, [text_reply("It ended ", draft[9:]),
                                                text_reply("NO_CALC")])  # fmt: skip

    status, seen = _run(manager, project, question)

    assert status == "done" and len(handle.requests) == 2 and RAN == []
    assert [e["text"] for e in seen if e["type"] == "delta"] == [draft]  # held, then at once
    assert "NO_CALC" not in str(seen)
    assert transcript(store, project) == [("user", question, None), ("assistant", draft, None)]


def test_asked_again_and_answering_from_memory_again_shows_the_first_answer(store):
    manager, project, handle = _manager(store, [
        text_reply("2 to the power of 40 is 1,099,511,627,776."),
        text_reply("It is 1099511627776."),
    ])  # fmt: skip

    status, seen = _run(manager, project, "What is 2 to the power of 40?")

    assert status == "done" and len(handle.requests) == 2
    assert _shown(seen) == "2 to the power of 40 is 1,099,511,627,776."
    assert [r[0] for r in transcript(store, project)] == ["user", "assistant"]


def test_a_message_with_no_number_is_answered_as_ever(store):
    manager, project, handle = _manager(store, [text_reply("Hello", ", I can help.")])

    status, seen = _run(manager, project, "Hi! What can you help me with?")

    assert status == "done" and len(handle.requests) == 1
    assert [e["text"] for e in seen if e["type"] == "delta"] == ["Hello", ", I can help."]


def test_without_the_sandbox_there_is_no_check(store):
    manager, project, handle = _manager(store, [text_reply("4172.04")], sandbox=False)

    status, _ = _run(manager, project, SUM)

    assert status == "done" and len(handle.requests) == 1


def test_a_reply_after_only_reading_the_project_is_held_and_checked(store):
    """Finding a file is reading, not computing: the answer after it could still be a sum
    from memory, so it waits for the check like a direct one, then appears at once."""
    manager, project, handle = _manager(store, [
        tool_reply("project_find", {"query": "invoice-2020"}),
        text_reply("There's no ", "invoice-2020.pdf here."),
        text_reply("NO_CALC"),
    ])  # fmt: skip

    status, seen = _run(manager, project, "Is there a file called invoice-2020.pdf?")

    assert status == "done" and len(handle.requests) == 3  # checked
    deltas = [e["text"] for e in seen if e["type"] == "delta"]
    assert deltas == ["There's no invoice-2020.pdf here."]  # held, then at once


def test_a_count_worked_out_after_reading_a_file_is_computed_instead(store):
    """csv-count on the 4B (2026-10-19): it found the file, read it, and counted in text.
    Read is all it did, so the count is checked, and computed."""
    manager, project, handle = _manager(store, [
        tool_reply("project_find", {"query": "payments.csv"}),
        tool_reply("document_read", {"name": "payments.csv"}, call_id="call_2"),
        text_reply("Looking at the dates, ", "4 payments were made after the 10th."),
        tool_reply("code_write", {"brief": "Count payments after day 10."}, call_id="call_3"),
        tool_reply("code_run", {}, call_id="call_4"),
        text_reply("**3** payments were made after the 10th."),
    ])  # fmt: skip

    status, seen = _run(manager, project, "How many payments were made after the 10th?")

    assert status == "done" and RAN == ["code.write", "code.run"]
    assert _shown(seen) == "**3** payments were made after the 10th."
    assert "4 payments" not in str(seen) and "4 payments" not in str(transcript(store, project))


def test_text_before_a_tool_call_in_a_held_reply_still_shows(store):
    words_then_call = sse(
        {"choices": [{"delta": {"content": "Let me compute that. "}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "code_write", "arguments": '{"brief": "sum"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )  # fmt: skip
    manager, project, handle = _manager(store, [
        words_then_call, tool_reply("code_run", {}, call_id="call_2"), text_reply("4172.04"),
    ])  # fmt: skip

    status, seen = _run(manager, project, SUM)

    assert status == "done" and RAN == ["code.write", "code.run"] and len(handle.requests) == 3
    assert _shown(seen) == "Let me compute that. 4172.04"


def test_each_numbered_turn_gets_its_own_single_check(store):
    manager, project, handle = _manager(store, [
        text_reply("It was signed in 1648."), text_reply("NO_CALC"),
        text_reply("Kyiv was founded in 482."), text_reply("NO_CALC"),
    ])  # fmt: skip

    _run(manager, project, "When was the Peace of Westphalia, in 1 line?")
    _run(manager, project, "And Kyiv, in 1 line?")

    assert len(handle.requests) == 4


def test_the_check_counts_towards_the_turns_model_calls(store):
    replies = [text_reply("It's 4172.03.")] + [
        tool_reply("code_write", {"brief": "sum"}, call_id=f"call_{i}") for i in range(8)
    ]
    manager, project, handle = _manager(store, replies)

    status, seen = _run(manager, project, SUM)

    assert status == "error" and len(handle.requests) == loop.MAX_MODEL_CALLS
    assert any("model calls" in e.get("detail", "") for e in seen if e["type"] == "error")


def test_stop_while_held_stores_nothing_the_person_didnt_see(store):
    never = asyncio.Event()  # the check never answers: the turn is stopped meanwhile

    def is_check(body: dict) -> bool:
        return body["messages"][-1]["content"] == EXACT_CHECK

    manager, project, handle = _manager(
        store, [text_reply("It's 4172.03."), text_reply("NO_CALC")], hold=never, hold_if=is_check
    )

    async def scenario():  # noqa: ANN202
        await manager.send(project, LOCAL_USER, 1, SUM, [])
        await until(lambda: any(is_check(r) for r in handle.requests))
        assert await manager.stop((project.id, 1))
        return await manager.wait((project.id, 1))

    assert asyncio.run(scenario()) == "stopped"
    assert [r[0] for r in transcript(store, project)] == ["user"]  # not the unchecked draft


def test_a_follow_up_after_a_tool_isnt_checked_though_the_older_message_has_a_number():
    """A follow-up turn (an export run once an OCR session finished) answers a tool's result:
    the person's message before it ("Scan lease-1998.pdf…") isn't what the reply is about."""
    from types import SimpleNamespace

    run = SimpleNamespace(registry=Registry([*CORE_TOOLS, *CODE]))
    asked = {"id": 1, "role": "user", "content": "Scan lease-1998.pdf and export it to DOCX."}
    after_a_tool = [
        asked,
        {"id": 2, "role": "assistant", "content": "", "tool_calls": []},
        {"id": 3, "role": "tool", "content": "Saved as Lease 1998.docx.", "tool_call_id": "c"},
    ]
    assert loop._may_check(run, loop.History([asked], [], None))
    assert not loop._may_check(run, loop.History(after_a_tool, [], None))
    # After tools that only read the project, the reply still answers the person's message.
    read = {"id": 3, "role": "tool", "content": "…", "tool_call_id": "c", "name": "document.read"}
    after_reading = [asked, {"id": 2, "role": "assistant", "content": "", "tool_calls": []}, read]
    assert loop._may_check(run, loop.History(after_reading, [], None))
    ran = {**read, "name": "code.run"}
    assert not loop._may_check(run, loop.History([*after_reading, ran], [], None))
    assert not loop._may_check(run, loop.History([], [], None))


# March 2027's working days, worked out day by day in text: answered from memory in 1,917
# characters on the 4B (2026-10-18), past the 1,500 a held reply used to stream after.
WORKED_OUT = "Here is every day of March 2027: " + "Monday 1 is a working day, " * 70 + "so 23."


def test_a_long_sum_answered_from_memory_is_held_whole_and_computed_instead(store):
    manager, project, handle = _manager(store, [
        text_reply(WORKED_OUT[:1000], WORKED_OUT[1000:]),
        tool_reply("code_write", {"brief": "Count Monday-to-Friday days in March 2027."}),
        tool_reply("code_run", {}, call_id="call_2"),
        text_reply("There are **23** working days in March 2027."),
    ])  # fmt: skip

    status, seen = _run(manager, project, "How many working days are in March 2027?")

    assert len(WORKED_OUT) > 1500
    assert status == "done" and RAN == ["code.write", "code.run"]
    assert _shown(seen) == "There are **23** working days in March 2027."
    assert "Monday 1" not in str(seen) and "Monday 1" not in str(transcript(store, project))


def test_a_long_reply_with_nothing_to_compute_is_shown_whole_once_checked(store):
    """An essay about the 1920s isn't a sum, but its question has a number: it waits for the
    check like any other reply that might be one, then appears at once."""
    essay = "a" * 1000 + "b" * 1000 + "c" * 10
    manager, project, handle = _manager(
        store, [text_reply(essay[:1000], essay[1000:2000], essay[2000:]), text_reply("NO_CALC")]
    )

    status, seen = _run(manager, project, "Write 3 paragraphs about the 1920s.")

    assert status == "done" and len(handle.requests) == 2  # checked
    assert [e["text"] for e in seen if e["type"] == "delta"] == [essay]
    assert transcript(store, project)[-1] == ("assistant", essay, None)


def test_a_long_answer_to_the_check_is_still_never_shown(store):
    manager, project, handle = _manager(
        store, [text_reply("It's 4172.03."), text_reply("x" * 3000)]
    )

    status, seen = _run(manager, project, SUM)

    assert status == "done" and len(handle.requests) == 2
    assert _shown(seen) == "It's 4172.03."  # the draft, as with any other text


def test_a_held_replys_time_to_first_token_is_when_it_was_shown(store, monkeypatch):
    """Held and checked, its first token reached the person after the check, not when the
    model wrote it: the same reply, unheld, has the shorter time."""

    def ttft(sandbox: bool) -> float:  # noqa: FBT001
        _ticking_clock(monkeypatch)
        manager, project, _ = _manager(store, [text_reply("1648."), text_reply("NO_CALC")],
                                       sandbox=sandbox)  # fmt: skip
        _, seen = _run(manager, project, "When was the Peace of Westphalia? In 1 word.")
        return next(e for e in seen if e["type"] == "done")["ttft"]

    assert ttft(sandbox=True) > ttft(sandbox=False)


def test_an_empty_reply_isnt_checked(store):
    manager, project, handle = _manager(store, [text_reply("")])

    status, _ = _run(manager, project, SUM)

    assert status == "done" and len(handle.requests) == 1  # nothing in it to compute
