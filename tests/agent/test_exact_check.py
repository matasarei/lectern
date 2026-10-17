"""The exact-results check (docs/acceptance/m1b.md): with the sandbox running, a reply to a
message with a number in it, given without any tool, is held back and the model is asked
once whether it states an exact result. If it does, it computes it with code_write and
code_run, and the draft is never shown; if not (NO_CALC), the draft is the answer."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from lectern.agent import loop
from lectern.agent.core_tools import CORE_TOOLS
from lectern.agent.prompts import EXACT_CHECK
from lectern.agent.tools import AgentTool, Registry, ToolContext, ToolResult
from lectern.agent.turns import TurnManager
from lectern.core.auth import LOCAL_USER

from .fake_model import FakeHandle, make_runtime, sse, text_reply, tool_reply
from .test_loop import transcript, turn, until

RAN: list[str] = []


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


def test_a_turn_that_called_a_tool_is_answered_as_ever(store):
    manager, project, handle = _manager(store, [
        tool_reply("project_find", {"query": "invoice-2020"}),
        text_reply("There's no ", "invoice-2020.pdf here."),
    ])  # fmt: skip

    status, seen = _run(manager, project, "Is there a file called invoice-2020.pdf?")

    assert status == "done" and len(handle.requests) == 2
    assert [e["text"] for e in seen if e["type"] == "delta"] == [
        "There's no ",
        "invoice-2020.pdf here.",
    ]


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
