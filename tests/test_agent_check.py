from __future__ import annotations

import pytest

from lectern.dev.agent_check import Case, Outcome, answer_matches, load_set, numbers_in, summary


@pytest.mark.parametrize(
    ("text", "values"),
    [
        ("The total is 4,172.04 UAH.", [4172.04]),
        ("Разом 4 172,04 грн", [4172.04]),
        ("penalty: 62,50", [62.5]),
        ("1,099,511,627,776 exactly", [1099511627776]),
        ("It is 23.5, and the median 23", [23.5, 23]),
        ("3 hours 47 minutes + 5:38 = 9 h 25 min", [3, 47, 5, 38, 9, 25]),
        ("1.234.567,89 EUR", [1234567.89]),
        ("no numbers here", []),
    ],
)
def test_numbers_are_read_the_ways_people_write_them(text, values):
    assert numbers_in(text) == values


def test_an_answer_needs_every_expected_number_and_word():
    case = Case("ratio", "Split 1,000 in 3:5:2", numbers=(300, 500, 200))
    assert answer_matches(case, "300, 500 and 200")
    assert not answer_matches(case, "300 and 500")
    day = Case("weekday", "What day?", text=("saturday",))
    assert answer_matches(day, "It was a Saturday.")
    assert not answer_matches(day, "Sunday")


@pytest.mark.parametrize(
    ("answer", "matches"),
    [
        ("Three payments were made after the 10th.", True),  # seen on the 4B, 2026-10-17
        ("There were three.", True),
        ("There were two.", False),
        ("Twelve payments.", False),
        ("Threefold more.", False),  # a word, not part of one
    ],
)
def test_a_small_number_written_as_a_word_counts(answer, matches):
    assert answer_matches(Case("csv-count", "How many?", numbers=(3,)), answer) is matches


def test_passing_needs_the_tool_and_the_right_answer():
    case = Case("sum", "Add", numbers=(4172.04,))
    good = Outcome(
        case, tools=["code.write", "code.run"], answer="4,172.04", required=("code.run",)
    )
    guessed = Outcome(case, tools=[], answer="4172.04", required=("code.run",))
    wrong = Outcome(case, tools=["code.run"], answer="4170", required=("code.run",))
    failed = Outcome(
        case, tools=["code.run"], answer="4172.04", error="boom", required=("code.run",)
    )
    assert [o.passed for o in (good, guessed, wrong, failed)] == [True, False, False, False]
    assert summary([good, guessed, wrong]) == (
        "1/3 passed · required tools (code.run) used in 2/3 · right answer in 2/3"
    )


def test_the_calc_set_has_22_prompts_and_its_files():
    cases, files = load_set("calc")
    assert len(cases) == 22 and len({c.id for c in cases}) == 22
    assert all(c.numbers or c.text for c in cases)
    assert [f.name for f in files] == ["payments.csv", "hours.xlsx"]
    assert all(f.is_file() for f in files)


def test_an_unknown_set_names_the_ones_there_are():
    with pytest.raises(ValueError, match="No prompt set 'nope'. Sets: calc, long, tools"):
        load_set("nope")


def test_a_list_of_numbers_stays_a_list():
    assert numbers_in("300, 500 and 200") == [300, 500, 200]
    assert numbers_in("parts: 300,500,200") == [300500200]  # no spaces: read as one number


def test_a_forbidden_tool_fails_the_prompt():
    case = Case("q", "What does notes.md say?", text=("payment",))
    rules = {"required": ("document.read",), "forbidden": ("artifact.trash",)}
    safe = Outcome(case, tools=["document.read"], answer="The payment day", **rules)
    trashed = Outcome(
        case, tools=["document.read", "artifact.trash"], answer="The payment day", **rules
    )
    assert (safe.passed, trashed.passed) == (True, False)


def test_extra_checks_must_all_hold():
    case = Case("long", "Quote 3.3", text=("пеню",))
    outcome = Outcome(
        case, answer="…сплачує пеню…", checks={"compacted": True, "transcript kept": False}
    )
    assert outcome.right and not outcome.passed


def test_the_summary_says_right_tools_when_prompts_need_different_ones():
    a = Outcome(Case("a", "x"), tools=["project.find"], required=("project.find",))
    b = Outcome(Case("b", "y"), tools=[], required=("document.read",))
    assert summary([a, b]) == "1/2 passed · right tools in 1/2 · right answer in 2/2"


def test_the_lease_is_made_at_run_time_with_clause_3_3(tmp_path):
    from lectern.dev.agent_check import CLAUSE_3_3, PROMPTS_DIR, materialise
    from lectern.projects.text import extract

    (lease,) = materialise([PROMPTS_DIR / "files" / "lease-1998.docx"], tmp_path)
    text = "\n".join(c.body for c in extract(lease).chunks)
    assert CLAUSE_3_3 in text
    with pytest.raises(ValueError, match="needs nope.txt"):
        materialise([PROMPTS_DIR / "files" / "nope.txt"], tmp_path)


class StubClient:
    """Answers like a server whose model echoes; every third turn reports a compaction."""

    def __init__(self, saves_on: int = 0):
        self.sent: list[str] = []
        self.turn = 0
        self.saves_on = saves_on  # this turn calls artifact.write

    def post(self, url, json=None):  # noqa: A002, ANN001
        class R:
            status_code = 202
            text = ""

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

        if url.endswith("/sessions"):
            return R({"id": 7})
        if url.endswith("/messages"):
            self.sent.append(json["text"])
        return R({})

    def get(self, url):  # noqa: ANN001
        class R:
            def json(inner):  # noqa: N805
                return {"messages": [{"role": "user"}] * len(self.sent)}

        return R()

    def stream(self, method, url, timeout=None):  # noqa: ANN001
        import contextlib
        import json as j

        self.turn += 1
        text = "…сплачує пеню у розмірі 0,5%…" if self.sent[-1].startswith("Quote") else "ok"
        events = [{"type": "delta", "text": text}]
        if self.turn == self.saves_on:
            events[:0] = [
                {"type": "tool", "call_id": "c1", "name": "artifact.write"},
                {"type": "tool_result", "call_id": "c1", "error": False},
            ]
        if self.turn % 3 == 0:
            events.append({"type": "compacted", "summarised": 4})
        events.append({"type": "turn", "status": "done"})

        class S:
            def iter_lines(inner):  # noqa: N805
                return [f"data: {j.dumps(e)}" for e in events]

        return contextlib.nullcontext(S())


def test_a_conversation_run_checks_compaction_and_the_transcript():
    from lectern.dev.agent_check import Conversation, run_conversation

    client = StubClient()
    conversation = Conversation(
        ("Tell me a fact.", "Another one."), 5, Case("long", "Quote 3.3", text=("пеню",))
    )

    outcome = run_conversation(client, 1, conversation, allow_cards=True, echo=lambda _l: None)

    assert client.sent[:5] == ["Tell me a fact.", "Another one."] * 2 + ["Tell me a fact."]
    assert client.sent[-1] == "Quote 3.3"
    assert outcome.compactions == 2 and outcome.checks == {
        "compacted": True,
        "transcript kept": True,
    }
    assert outcome.passed


@pytest.mark.parametrize("name", ["calc", "tools", "long", "web"])
def test_every_set_loads_and_every_prompt_checks_something(name):
    from lectern.dev.agent_check import load

    prompt_set = load(name)
    cases = list(prompt_set.cases)
    if prompt_set.conversation:
        cases.append(prompt_set.conversation.final)
    assert len({c.id for c in cases}) == len(cases)
    assert all(c.numbers or c.text or c.tools or c.forbid for c in cases)
    for path in prompt_set.files:
        assert path.is_file() or path.name == "lease-1998.docx"


def test_the_tools_set_has_about_30_prompts_and_ends_with_the_trash():
    from lectern.dev.agent_check import load

    cases = load("tools").cases
    assert 28 <= len(cases) <= 34  # ~30 (blueprint); grows by a case per real failure seen
    assert cases[-1].tools == ("artifact.trash",)
    assert not any("artifact.trash" in c.tools for c in cases[:-1])


def test_the_web_set_keeps_the_projects_own_questions_off_the_web():
    from lectern.dev.agent_check import load

    cases = {c.id: c for c in load("web").cases}
    web = {"web.search", "wiki.lookup", "web.fetch"}
    for case_id in ("project-penalty", "project-tenant", "greeting"):
        assert web <= set(cases[case_id].forbid), case_id
    assert {t for c in cases.values() for t in c.tools} >= web  # each tool is reached for


def test_the_long_set_is_100_turns_and_quotes_clause_3_3():
    from lectern.dev.agent_check import CLAUSE_3_3, load

    conversation = load("long").conversation
    assert conversation is not None and conversation.turns + 1 == 100
    assert all(fragment in CLAUSE_3_3 for fragment in conversation.final.text)


def test_alternatives_in_required_tools():
    case = Case("x", "?")
    either = ("project.find|project.list",)
    assert Outcome(case, tools=["project.list"], required=either).used_tools
    assert not Outcome(case, tools=["document.read"], required=either).used_tools


def test_a_failed_call_doesnt_count_as_using_the_tool():
    from lectern.dev.agent_check import _line

    case = Case("pin", "Always answer in Ukrainian")
    refused = Outcome(case, tools=[], attempted=["chat.pin"] * 3, required=("chat.pin",))
    assert not refused.used_tools
    assert "(+3 failed)" in _line(refused)
    tried_trash = Outcome(
        Case("memo", "?"),
        tools=["document.read"],
        attempted=["document.read", "artifact.trash"],
        forbidden=("artifact.trash",),
    )
    assert not tried_trash.used_tools  # trying a forbidden tool fails, even if it was refused


def test_a_conversation_fails_when_a_filler_turn_saves_a_file():
    from lectern.dev.agent_check import Conversation, run_conversation

    conversation = Conversation(
        ("Tell me a fact.",),
        5,
        Case("long", "Quote 3.3", text=("пеню",)),
        forbid=("artifact.write",),
    )
    said: list[str] = []

    clean = run_conversation(StubClient(), 1, conversation, allow_cards=True, echo=lambda _l: None)
    saved = run_conversation(
        StubClient(saves_on=2), 1, conversation, allow_cards=True, echo=said.append
    )

    assert clean.passed and clean.checks["no forbidden tools in filler"]
    assert not saved.passed and not saved.checks["no forbidden tools in filler"]
    assert "  turn 2: called artifact.write" in said
