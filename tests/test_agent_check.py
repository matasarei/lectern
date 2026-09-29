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


def test_the_calc_set_has_20_prompts_and_its_files():
    cases, files = load_set("calc")
    assert len(cases) == 20 and len({c.id for c in cases}) == 20
    assert all(c.numbers or c.text for c in cases)
    assert [f.name for f in files] == ["payments.csv", "hours.xlsx"]
    assert all(f.is_file() for f in files)


def test_an_unknown_set_names_the_ones_there_are():
    with pytest.raises(ValueError, match="No prompt set 'nope'. Sets: calc"):
        load_set("nope")


def test_a_list_of_numbers_stays_a_list():
    assert numbers_in("300, 500 and 200") == [300, 500, 200]
    assert numbers_in("parts: 300,500,200") == [300500200]  # no spaces: read as one number
