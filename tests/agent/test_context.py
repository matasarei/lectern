from __future__ import annotations

import asyncio

import pytest

from lectern.agent.context import (
    Attachment,
    ContextTooSmall,
    Counter,
    answer_reserve,
    build,
    split_turns,
)

WORDS = Counter()  # the estimate: characters // 3 + 1


def user(text):
    return {"role": "user", "content": text}


def assistant(text, calls=None):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = calls
    return message


def call(call_id, name="project_search"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def result(call_id, text, name="project.search"):
    return {"role": "tool", "tool_call_id": call_id, "content": text, "name": name}


def run(**kwargs):
    base = {"system": "You are Lectern.", "tools": [], "attachments": [], "counter": WORDS}
    return asyncio.run(build(**{**base, **kwargs}))


def test_everything_fits_in_order_and_names_are_not_sent():
    history = [
        user("find the lease"),
        assistant("", [call("a")]),
        result("a", "lease.md #c3"),
        assistant("It's in lease.md."),
    ]
    built = run(history=history, ctx=8192)

    assert [m["role"] for m in built.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert "name" not in built.messages[3]
    assert built.messages[3]["tool_call_id"] == "a"
    assert (built.left_out, built.released) == (0, 0)
    assert built.budget == 8192 - 1536


def test_answer_reserve_is_smaller_on_small_contexts():
    assert answer_reserve(4096) == 1024
    assert answer_reserve(32768) == 1536


def test_turns_start_at_user_messages():
    history = [user("1"), assistant("", [call("a")]), result("a", "r"), assistant("x"), user("2")]
    assert [len(t) for t in split_turns(history)] == [4, 1]


def test_old_tool_results_are_released_before_turns_are_dropped():
    big = "x" * 6000  # ~2000 tokens
    history = [
        user("first"),
        assistant("", [call("a")]),
        result("a", big),
        assistant("done"),
        user("second"),
    ]
    built = run(history=history, ctx=2400)  # budget 1800 (a quarter kept for the answer)

    assert built.left_out == 0 and built.released == 1
    assert built.messages[3]["content"].startswith("[This project.search result was released")
    assert built.messages[-1] == user("second")


def test_the_oldest_turns_are_left_out_when_still_too_long():
    history = []
    for i in range(10):
        history += [user(f"question {i} " + "y" * 600), assistant(f"answer {i} " + "z" * 600)]
    history.append(user("latest"))

    built = run(history=history, ctx=4000)  # budget 3000

    assert built.left_out > 0 and built.left_out % 2 == 0
    sent = [m["content"][:11] for m in built.messages[1:]]
    assert sent[-1] == "latest"
    assert "question 0 " not in sent
    assert built.parts["turns"] <= built.budget - built.parts["system"]


def test_earlier_results_in_the_current_turn_go_before_the_newest():
    big = "q" * 3600  # ~1200 tokens each
    history = [
        user("read both"),
        assistant("", [call("a")]),
        result("a", big),
        assistant("", [call("b")]),
        result("b", big),
    ]
    built = run(history=history, ctx=2800)  # budget 2100

    contents = [m["content"] for m in built.messages if m["role"] == "tool"]
    assert contents[0].startswith("[This project.search result was released")
    assert contents[1] == big


def test_it_says_so_when_even_the_current_message_does_not_fit():
    with pytest.raises(ContextTooSmall, match="current message"):
        run(history=[user("w" * 9000)], ctx=2048)
    with pytest.raises(ContextTooSmall, match="system prompt"):
        run(history=[user("hi")], system="s" * 9000, ctx=2048)


def test_attachments_go_in_whole_or_as_a_stub():
    small = Attachment(1, "notes.md", "Pay by the 10th.")
    huge = Attachment(2, "book.txt", "b" * 30000)
    scan = Attachment(3, "scan.pdf", "", note="no text layer")

    built = run(history=[user("summarise")], attachments=[small, huge, scan], ctx=8192)

    system = built.messages[0]["content"]
    assert "## notes.md\nPay by the 10th." in system
    assert "## book.txt\n[Too long to include here. Read it with document_outline" in system
    assert "## scan.pdf\n[No text to read: no text layer.]" in system
    assert built.stubbed_files == ["book.txt"]
    assert built.parts["files"] > 0  # attached files have their own part now; pins are "pinned"


def test_the_runner_tokenizer_is_used_and_remembered():
    calls = []

    async def tokenize(text):
        calls.append(text)
        return len(text)  # one token per character

    counter = Counter(tokenize)
    built = asyncio.run(
        build(
            system="sys", tools=[], attachments=[], history=[user("hello")], ctx=4096,
            counter=counter,
        )
    )  # fmt: skip
    asyncio.run(counter.count("hello"))

    assert built.parts["system"] == 3 + 4
    assert calls.count("hello") == 1


def test_pins_files_and_summary_go_in_the_system_message_in_that_order():
    built = run(
        history=[user("next question")],
        attachments=[Attachment(1, "notes.md", "Pay by the 10th.")],
        pins=[("instruction", "Answer in Ukrainian"), ("fact", "The payment day is the 15th")],
        summary="Goals: the penalty. Read lease.md #c3.",
        ctx=8192,
    )
    system = built.messages[0]["content"]
    pinned_at = system.index("# Pinned by the person (these hold; follow them)")
    files_at = system.index("# Files attached in this chat")
    summary_at = system.index("# Earlier in this chat (summary)")
    assert pinned_at < files_at < summary_at
    assert "- (instruction) Answer in Ukrainian\n- (fact) The payment day is the 15th" in system
    assert system.endswith("Goals: the penalty. Read lease.md #c3.")
    assert built.parts["pinned"] > 0 and built.parts["summary"] > 0 and built.parts["files"] > 0


def test_pins_and_the_summary_are_never_stubbed_even_when_turns_must_go():
    history = []
    for i in range(10):
        history += [user(f"q{i} " + "y" * 900), assistant(f"a{i} " + "z" * 900)]
    history.append(user("latest"))
    pins = [("instruction", "Answer in Ukrainian")]
    built = run(history=history, pins=pins, summary="S" * 600, ctx=4000)

    system = built.messages[0]["content"]
    assert "Answer in Ukrainian" in system and "S" * 600 in system
    assert built.left_out > 0 and built.messages[-1] == user("latest")


def test_stored_message_ids_are_not_sent_to_the_model():
    built = run(history=[{"id": 7, "role": "user", "content": "hi"}], ctx=4096)
    assert built.messages[1] == {"role": "user", "content": "hi"}
