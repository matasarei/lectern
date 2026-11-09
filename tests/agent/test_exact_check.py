"""The exact-results check (docs/acceptance/m1b.md): with the sandbox running, a reply to the
person's message given without any tool is held back, and if it has a number in it the model
is asked once whether it states an exact result. If it does, it computes it with code_write
and code_run, and the draft is never shown; if not (NO_CALC), the draft is the answer."""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel
from sqlalchemy import select

from lectern.agent import loop
from lectern.agent.core_tools import CORE_TOOLS
from lectern.agent.prompts import CALL_CHECK, EXACT_CHECK, HELD_BLANK, HELD_GUARD, HELD_NOTE
from lectern.agent.tools import AgentTool, Registry, ToolContext, ToolError, ToolResult
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


def test_a_reply_with_no_number_isnt_checked_and_shows_at_once(store):
    manager, project, handle = _manager(store, [text_reply("Hello", ", I can help.")])

    status, seen = _run(manager, project, "Hi! What can you help me with?")

    assert status == "done" and len(handle.requests) == 1  # nothing in it to compute
    assert [e["text"] for e in seen if e["type"] == "delta"] == ["Hello, I can help."]


def test_a_count_asked_in_words_is_computed_instead(store):
    """digit-count-words on the 4B (2026-10-23): no digit in the question, answered from
    memory 3 times in 3. The draft has the number, so the draft is what's checked."""
    manager, project, handle = _manager(store, [
        text_reply("This is general knowledge. ", "You write the digit one **120** times."),
        tool_reply("code_write", {"brief": "Count the digit 1 in the numbers 1 to 200."}),
        tool_reply("code_run", {}, call_id="call_2"),
        text_reply("You write the digit one **140** times."),
    ])  # fmt: skip

    status, seen = _run(manager, project, "You write down the numbers from one to two "
                        "hundred. How many times do you write the digit one?")  # fmt: skip

    assert status == "done" and RAN == ["code.write", "code.run"]
    assert _shown(seen) == "You write the digit one **140** times."
    assert "120" not in str(seen) and "120" not in str(transcript(store, project))


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


def test_stop_during_a_held_reply_with_no_number_stores_nothing(store, monkeypatch):
    """Every reply with no tool is held now, a letter as much as a sum: stopped while it's
    written, none of it was shown, so none of it is kept (unheld, the partial text is)."""
    manager, project, _ = _manager(store, [])
    started: list[bool] = []

    async def writing(run, client, body):  # noqa: ANN001, ANN202, ARG001
        run.emit({"type": "delta", "text": "Dear tenant, "})
        started.append(True)
        await asyncio.Event().wait()  # still writing when the turn is stopped

    monkeypatch.setattr(loop, "_stream", writing)

    async def scenario():  # noqa: ANN202
        async with manager.channel((project.id, 1)).listen() as (_, queue):
            await manager.send(project, LOCAL_USER, 1, "Write a letter to the tenant.", [])
            await until(lambda: bool(started))
            assert await manager.stop((project.id, 1))
            status = await manager.wait((project.id, 1))
            return status, [queue.get_nowait() for _ in range(queue.qsize())]

    status, seen = asyncio.run(scenario())

    assert status == "stopped" and _shown(seen) == ""
    assert [r[0] for r in transcript(store, project)] == ["user"]


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
    # A message with no digit may still be answered with a sum: the draft decides.
    in_words = {"id": 1, "role": "user", "content": "How many days are there in a leap year?"}
    assert loop._may_check(run, loop.History([in_words], [], None))


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
    """An essay about the 1920s isn't a sum, but it has numbers in it: it waits for the check
    like any other reply that might be one, then appears at once."""
    essay = "In 1925 " + "a" * 1000 + "b" * 1000 + "c" * 10
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


# --- the call check: a tool writing a number nobody gave ------------------------------------


class _Doc(BaseModel):
    text: str
    over: str = ""


# A stand-in for pdf.edit / artifact.write: it writes `text` over `over`.
DOC = AgentTool(name="doc.write", description="Write text into the person's file.", params=_Doc,
                run=_ran("doc.write", "Written."), writes=lambda p: (p.text, p.over))  # fmt: skip


def _writer(store, replies, *, sandbox=True):  # noqa: ANN001, ANN202
    RAN.clear()
    handle = FakeHandle(replies)
    tools = [*CORE_TOOLS, *CODE, DOC] if sandbox else [*CORE_TOOLS, DOC]
    manager = TurnManager(make_runtime(handle), store, Registry(tools))
    return manager, store.create(LOCAL_USER, "Totals"), handle


ADD_AND_SAVE = "Add 1284.50, 377.25, 99.99 and 2410.30 and put the total in the file."


def _tool_rows(store, project) -> list[str]:  # noqa: ANN001
    return [name for role, _, name in transcript(store, project) if role == "tool"]


def test_a_call_writing_a_total_nobody_gave_is_checked_and_computed_first(store):
    manager, project, handle = _writer(store, [
        tool_reply("doc_write", {"text": "Total: 4172.03"}),  # summed in its head, and wrong
        tool_reply("code_write", {"brief": "Add 1284.50, 377.25, 99.99 and 2410.30."}),
        tool_reply("code_run", {}, call_id="call_2"),
        tool_reply("doc_write", {"text": "Total: 4172.04"}, call_id="call_3"),
        text_reply("Saved."),
    ])  # fmt: skip

    status, seen = _run(manager, project, ADD_AND_SAVE)

    assert status == "done"
    assert RAN == ["code.write", "code.run", "doc.write"]  # the draft call never ran
    check = handle.requests[1]["messages"]
    assert check[-1] == {
        "role": "user",
        "content": CALL_CHECK.format(
            tool="doc_write", numbers="4172.03", arguments='{"text": "Total: 4172.03"}'
        ),
    }
    assert all(m.get("content") != "Total: 4172.03" for m in check)  # the draft isn't shown
    assert _tool_rows(store, project) == ["code.write", "code.run", "doc.write"]
    assert "4172.03" not in str(transcript(store, project))  # neither stored
    assert CALL_CHECK.split(":")[0] not in str(transcript(store, project))
    assert "4172.03" not in str(seen)


def test_no_calculation_runs_the_call_as_it_was_and_asks_no_more_that_turn(store):
    manager, project, handle = _writer(store, [
        tool_reply("doc_write", {"text": "Invoice 4410"}),
        text_reply("NO_CALC"),
        tool_reply("doc_write", {"text": "Ref 7731"}, call_id="call_2"),
        text_reply("Saved."),
    ])  # fmt: skip

    status, seen = _run(manager, project, "Put my invoice number in the file.")

    assert status == "done" and RAN == ["doc.write", "doc.write"]
    assert len(handle.requests) == 4  # one check, for the first call only
    assert "NO_CALC" not in str(seen) and "NO_CALC" not in str(transcript(store, project))


def test_numbers_the_person_a_tool_or_the_text_written_over_gave_arent_checked(store):
    manager, project, handle = _writer(store, [
        tool_reply("doc_write", {"text": "Paid 1,250.00 on 5 April 2026"}),
        tool_reply("doc_write", {"text": "Fee EUR 1,330.00", "over": "Fee EUR 1,330.00 net"},
                   call_id="call_2"),
        tool_reply("doc_write", {"text": "1. Rent\n2. Water\n10) Heating"}, call_id="call_3"),
        text_reply("Done."),
    ])  # fmt: skip

    status, _ = _run(manager, project, "Write that I paid 1250.00 on 5 April 2026.")

    assert status == "done" and RAN == ["doc.write"] * 3
    assert len(handle.requests) == 4  # no check anywhere


def test_without_the_sandbox_no_call_is_checked(store):
    manager, project, handle = _writer(
        store, [tool_reply("doc_write", {"text": "Total: 4172.03"}), text_reply("Saved.")],
        sandbox=False,
    )  # fmt: skip

    status, _ = _run(manager, project, ADD_AND_SAVE)

    assert status == "done" and RAN == ["doc.write"] and len(handle.requests) == 2


def test_a_call_whose_arguments_dont_validate_isnt_checked(store):
    manager, project, handle = _writer(store, [
        tool_reply("doc_write", {"words": "Total: 4172.03"}),  # no `text`: it fails to run
        text_reply("Sorry."),
    ])  # fmt: skip

    status, _ = _run(manager, project, ADD_AND_SAVE)

    assert status == "done" and RAN == [] and len(handle.requests) == 2


def test_a_call_whose_writes_cant_say_what_it_writes_isnt_checked(store):
    def cannot_say(params: _Doc) -> tuple[str, str]:
        raise ToolError("no text to write")

    unsure = AgentTool(
        name="doc.write", description="Write text into the person's file.", params=_Doc,
        run=_ran("doc.write", "Written."), writes=cannot_say,
    )  # fmt: skip
    RAN.clear()
    handle = FakeHandle([tool_reply("doc_write", {"text": "Total: 4172.03"}), text_reply("Saved.")])
    manager = TurnManager(make_runtime(handle), store, Registry([*CORE_TOOLS, *CODE, unsure]))
    project = store.create(LOCAL_USER, "Totals")

    status, _ = _run(manager, project, ADD_AND_SAVE)

    assert status == "done" and RAN == ["doc.write"] and len(handle.requests) == 2


def test_the_call_check_counts_towards_the_turns_model_calls(store, monkeypatch):
    monkeypatch.setattr(loop, "MAX_MODEL_CALLS", 2)
    manager, project, handle = _writer(store, [
        tool_reply("doc_write", {"text": "Total: 4172.03"}),
        text_reply("NO_CALC"),
    ])  # fmt: skip

    status, seen = _run(manager, project, ADD_AND_SAVE)

    assert status == "error" and RAN == ["doc.write"] and len(handle.requests) == 2
    assert any("Stopped after" in str(e.get("detail")) for e in seen if e["type"] == "error")


def test_the_call_check_quotes_a_long_written_text_cut_short(store):
    long = "Total: 4172.03. " + "Notes about the payment schedule. " * 20
    manager, project, handle = _writer(store, [
        tool_reply("doc_write", {"text": long}), text_reply("NO_CALC"), text_reply("Saved."),
    ])  # fmt: skip

    _run(manager, project, ADD_AND_SAVE)

    asked = handle.requests[1]["messages"][-1]["content"]
    arguments = json.dumps({"text": long}, ensure_ascii=False)
    assert f"{arguments[: loop.CALL_QUOTE_CHARS]}…" in asked
    assert arguments[: loop.CALL_QUOTE_CHARS + 1] not in asked


def test_a_number_in_a_file_attached_to_the_chat_is_given(store):
    from .test_loop import add_file

    manager, project, handle = _writer(store, [
        tool_reply("doc_write", {"text": "Invoice total: 1,480.00"}), text_reply("Saved."),
    ])  # fmt: skip
    invoice = add_file(store, project, "invoice.txt", "Total due 1,480.00 by 10 December.")

    async def scenario():  # noqa: ANN202
        await manager.send(project, LOCAL_USER, 1, "Put the invoice total in the file.", [invoice])
        return await manager.wait((project.id, 1))

    assert asyncio.run(scenario()) == "done"
    assert RAN == ["doc.write"] and len(handle.requests) == 2  # no check


# --- the held call: kept while its number is computed ---------------------------------------


class _Pair(BaseModel):
    find: str
    replace: str
    page: int | None = None


class _Edits(BaseModel):
    edits: list[_Pair]


# A stand-in for pdf.edit: several find → replace pairs in one call.
EDIT = AgentTool(name="doc.edit", description="Change text in the person's file.", params=_Edits,
                 run=_ran("doc.edit", "Saved."),
                 writes=lambda p: (" ".join(e.replace for e in p.edits),
                                   " ".join(e.find for e in p.edits)))  # fmt: skip


def _editor(store, replies, *, sandbox=True):  # noqa: ANN001, ANN202
    RAN.clear()
    handle = FakeHandle(replies)
    tools = [*CORE_TOOLS, *CODE, EDIT] if sandbox else [*CORE_TOOLS, EDIT]
    manager = TurnManager(make_runtime(handle), store, Registry(tools))
    return manager, store.create(LOCAL_USER, "Invoice"), handle


PRICE_AND_TOTAL = (
    "Change the price to 225.00 and put the total of 1284.50, 377.25, 99.99 and 2410.30 in."
)
BOTH = {"edits": [{"find": "Price 200.00", "replace": "Price 225.00"},
                  {"find": "Total 4000.00", "replace": "Total 4172.03"}]}  # fmt: skip
BOTH_COMPUTED = {"edits": [{"find": "Price 200.00", "replace": "Price 225.00"},
                           {"find": "Total 4000.00", "replace": "Total 4172.04"}]}  # fmt: skip
HELD_ARGUMENTS = (
    '{"edits": [{"find": "Price 200.00", "replace": "Price 225.00"}, '
    '{"find": "Total 4000.00", "replace": "Total 4172.03"}]}'
)
# The note and the guard quote it without the number worked out in the head: copied from the
# note, a wrong total went into the PDF (agent-check runs 11-12).
BLANKED = HELD_ARGUMENTS.replace("4172.03", HELD_BLANK)
NOTE = {
    "role": "user",
    "content": HELD_NOTE.format(tool="doc_edit", blank=HELD_BLANK, arguments=BLANKED),
}
COMPUTE = [
    tool_reply("code_write", {"brief": "Add 1284.50, 377.25, 99.99 and 2410.30."}),
    tool_reply("code_run", {}, call_id="call_2"),
]


def _noted(handle) -> list[bool]:  # noqa: ANN001
    """For each model call: whether it carried the held note."""
    return [
        any("Lectern note" in str(m.get("content")) for m in r["messages"]) for r in handle.requests
    ]


def test_a_held_call_is_quoted_with_every_edit_on_each_model_call_until_made(store):
    manager, project, handle = _editor(store, [
        tool_reply("doc_edit", BOTH),  # the total summed in its head
        *COMPUTE,
        tool_reply("doc_edit", BOTH_COMPUTED, call_id="call_3"),
        text_reply("Saved."),
    ])  # fmt: skip

    status, seen = _run(manager, project, PRICE_AND_TOTAL)

    assert status == "done" and RAN == ["code.write", "code.run", "doc.edit"]
    check = handle.requests[1]["messages"][-1]["content"]
    assert check == CALL_CHECK.format(tool="doc_edit", numbers="4172.03", arguments=HELD_ARGUMENTS)
    assert _noted(handle) == [False, False, True, True, False]  # code_write, code_run: noted
    assert handle.requests[2]["messages"][-1] == NOTE
    assert handle.requests[3]["messages"][-1] == NOTE
    assert "Price 200.00" in NOTE["content"] and "Total 4000.00" in NOTE["content"]
    assert "4172.03" not in NOTE["content"]
    assert "Lectern note" not in str(transcript(store, project))  # never stored
    assert _shown(seen) == "Saved."


def test_the_note_stops_once_the_tool_is_called_again_with_any_arguments(store):
    total_only = {"edits": [{"find": "Total 4000.00", "replace": "Total 4172.04"}]}
    manager, project, handle = _editor(store, [
        tool_reply("doc_edit", BOTH),
        *COMPUTE,
        tool_reply("doc_edit", total_only, call_id="call_3"),  # the price edit dropped
        text_reply("Saved."),
    ])  # fmt: skip

    status, _ = _run(manager, project, PRICE_AND_TOTAL)

    assert status == "done" and RAN == ["code.write", "code.run", "doc.edit"]
    assert _noted(handle) == [False, False, True, True, False]
    assert len(handle.requests) == 5  # and no guard: the call was made


def test_no_held_call_no_note(store):
    manager, project, handle = _editor(store, [
        tool_reply("doc_edit", BOTH),
        text_reply("NO_CALC"),  # copied, it says: the call runs as it was
        text_reply("Saved."),
    ])  # fmt: skip

    status, seen = _run(manager, project, PRICE_AND_TOTAL)

    assert status == "done" and RAN == ["doc.edit"]
    assert _noted(handle) == [False, False, False]
    assert _shown(seen) == "Saved."


GUARD = HELD_GUARD.format(tool="doc_edit", blank=HELD_BLANK, arguments=BLANKED)


def test_a_turn_ending_with_the_call_still_held_is_asked_once_and_makes_it(store):
    manager, project, handle = _editor(store, [
        tool_reply("doc_edit", BOTH),
        *COMPUTE,
        text_reply("I have updated the file."),  # it says so, and made no call
        tool_reply("doc_edit", BOTH_COMPUTED, call_id="call_3"),
        text_reply("Saved: the price and the total."),
    ])  # fmt: skip

    status, seen = _run(manager, project, PRICE_AND_TOTAL)

    assert status == "done" and RAN == ["code.write", "code.run", "doc.edit"]
    guard = handle.requests[4]["messages"]
    assert guard[-2:] == [
        {"role": "assistant", "content": "I have updated the file."},
        {"role": "user", "content": GUARD},
    ]
    assert _shown(seen) == "Saved: the price and the total."
    assert "I have updated" not in str(seen) and "I have updated" not in str(
        transcript(store, project)
    )
    assert "Lectern check" not in str(transcript(store, project))
    assert len(handle.requests) == 6


def test_a_text_reply_to_the_guard_leaves_the_reply_it_asked_about(store):
    manager, project, handle = _editor(store, [
        tool_reply("doc_edit", BOTH),
        *COMPUTE,
        text_reply("The total is 4172.04."),
        text_reply("The person only asked for the total."),
    ])  # fmt: skip

    status, seen = _run(manager, project, PRICE_AND_TOTAL)

    assert status == "done" and RAN == ["code.write", "code.run"]
    assert len(handle.requests) == 5  # asked once
    assert _shown(seen) == "The total is 4172.04."
    assert "only asked" not in str(seen) and "only asked" not in str(transcript(store, project))


def test_the_guard_asks_once_a_turn(store):
    manager, project, handle = _editor(store, [
        tool_reply("doc_edit", BOTH),
        *COMPUTE,
        text_reply("Let me look at the file first."),
        tool_reply("document_read", {"name": "invoice.pdf"}, call_id="call_3"),
        text_reply("Done."),
    ])  # fmt: skip

    status, seen = _run(manager, project, PRICE_AND_TOTAL)

    assert status == "done" and len(handle.requests) == 6
    assert sum(m.get("content") == GUARD for r in handle.requests for m in r["messages"]) == 1
    assert _shown(seen).endswith("Done.")


def test_no_held_call_no_guard(store):
    manager, project, handle = _editor(store, [
        tool_reply("doc_edit", BOTH), text_reply("NO_CALC"), text_reply("Saved."),
    ])  # fmt: skip

    status, _ = _run(manager, project, PRICE_AND_TOTAL)

    assert status == "done" and len(handle.requests) == 3
    assert not any(m.get("content") == GUARD for r in handle.requests for m in r["messages"])


def test_the_guard_counts_towards_the_turns_model_calls(store, monkeypatch):
    monkeypatch.setattr(loop, "MAX_MODEL_CALLS", 4)
    manager, project, handle = _editor(store, [
        tool_reply("doc_edit", BOTH),
        tool_reply("code_run", {}),  # the check's answer
        text_reply("I have updated the file."),
        tool_reply("doc_edit", BOTH_COMPUTED, call_id="call_3"),  # the guard's answer
    ])  # fmt: skip

    status, seen = _run(manager, project, PRICE_AND_TOTAL)

    assert status == "error" and RAN == ["code.run", "doc.edit"] and len(handle.requests) == 4
    assert any("Stopped after" in str(e.get("detail")) for e in seen if e["type"] == "error")


def test_no_guard_when_no_model_call_is_left(store, monkeypatch):
    monkeypatch.setattr(loop, "MAX_MODEL_CALLS", 3)
    manager, project, handle = _editor(store, [
        tool_reply("doc_edit", BOTH),
        tool_reply("code_run", {}),
        text_reply("The total is 4172.04."),
    ])  # fmt: skip

    status, seen = _run(manager, project, PRICE_AND_TOTAL)

    assert status == "done" and len(handle.requests) == 3
    assert _shown(seen) == "The total is 4172.04."


def test_only_the_unchecked_number_itself_is_blanked():
    params = _Edits(edits=[
        {"find": "Fee 150.00", "replace": "Fee 5", "page": 5},
        {"find": "Item 5.50", "replace": "Item 5.50 x 5"},
    ])  # fmt: skip
    held = loop._Held("doc_edit", params, ["5"])

    assert held.quoted(blank=True) == (
        f'{{"edits": [{{"find": "Fee 150.00", "replace": "Fee {HELD_BLANK}", "page": 5}}, '
        f'{{"find": "Item 5.50", "replace": "Item 5.50 x {HELD_BLANK}"}}]}}'
    )
    assert held.quoted(blank=False) == json.dumps(
        params.model_dump(exclude_defaults=True), ensure_ascii=False
    )
