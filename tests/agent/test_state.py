from __future__ import annotations

import pytest

from lectern.agent import state
from lectern.agent.state import MAX_PINS, PinError
from lectern.core.auth import LOCAL_USER


@pytest.fixture
def chat(store):
    project = store.create(LOCAL_USER, "State")
    return store.db(project), 1


def test_a_new_chat_has_nothing_pinned_and_no_summary(chat):
    sessions, sid = chat
    assert state.load(sessions, sid) == state.ChatState()


def test_pin_and_unpin_by_id_or_text(chat):
    sessions, sid = chat
    ukrainian = state.pin(sessions, sid, "  Always answer   in Ukrainian ", "instruction")
    day = state.pin(sessions, sid, "The payment day is the 15th", "fact")

    assert ukrainian.text == "Always answer in Ukrainian"
    assert [p.text for p in state.load(sessions, sid).pinned] == [
        "Always answer in Ukrainian", "The payment day is the 15th"
    ]  # fmt: skip
    assert state.pin(sessions, sid, "always answer in ukrainian", "instruction") == ukrainian

    state.unpin(sessions, sid, day.id)
    state.unpin(sessions, sid, "ALWAYS ANSWER IN UKRAINIAN")
    assert state.load(sessions, sid).pinned == []
    with pytest.raises(PinError, match="Nothing pinned matches"):
        state.unpin(sessions, sid, "missing")


@pytest.mark.parametrize(
    ("text", "kind", "error"),
    [("", "fact", "nothing to pin"), ("x" * 301, "fact", "at most 300"), ("ok", "rule", "kind")],
)
def test_bad_pins_are_refused(chat, text, kind, error):
    sessions, sid = chat
    with pytest.raises(PinError, match=error):
        state.pin(sessions, sid, text, kind)


def test_at_most_twenty_pins(chat):
    sessions, sid = chat
    for i in range(MAX_PINS):
        state.pin(sessions, sid, f"fact {i}", "fact")
    with pytest.raises(PinError, match="At most 20"):
        state.pin(sessions, sid, "one more", "fact")


def test_each_summary_is_a_new_version(chat):
    sessions, sid = chat
    first = state.save_summary(sessions, sid, "Goals: find the lease.", upto=4)
    second = state.save_summary(sessions, sid, "Goals: penalty; lease.md #c3.", upto=9)
    assert (first.summary_version, second.summary_version) == (1, 2)
    loaded = state.load(sessions, sid)
    assert (loaded.summary, loaded.summary_upto) == ("Goals: penalty; lease.md #c3.", 9)
