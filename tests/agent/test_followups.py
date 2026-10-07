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
