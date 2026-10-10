"""The settings store (lectern.core.settings): JSON values in the app database."""

from __future__ import annotations

import pytest

from lectern.core import settings
from lectern.core.db import make_engine, make_sessionmaker, migrate

DEFAULTS = {"colour": "red", "size": None}


@pytest.fixture
def sessions(tmp_path):
    engine = make_engine(tmp_path / "app.sqlite")
    migrate(engine)
    return make_sessionmaker(engine)


def test_update_changes_some_values_and_says_what_they_were(sessions):
    before = settings.update(sessions, "look", DEFAULTS, {"size": 3})

    assert before == DEFAULTS  # nothing was stored: the defaults
    assert settings.get(sessions, "look", DEFAULTS) == {"colour": "red", "size": 3}

    before = settings.update(sessions, "look", DEFAULTS, {"colour": "blue"})

    assert before == {"colour": "red", "size": 3}
    assert settings.get(sessions, "look", DEFAULTS) == {"colour": "blue", "size": 3}


def test_update_keeps_no_value_the_setting_does_not_have(sessions):
    settings.put(sessions, "look", {"colour": "green", "left over": True})

    before = settings.update(sessions, "look", DEFAULTS, {"size": 1})

    assert before == {"colour": "green", "size": None}
    assert settings.get(sessions, "look", {**DEFAULTS, "left over": None})["left over"] is None


def test_update_is_one_transaction(sessions):
    """Read, change and write happen in one transaction, so two changes made at the same
    moment can't each write back the other's old value."""

    class Counting:
        """The session maker, noting each session or transaction it's asked for."""

        opened: list[str] = []

        def begin(self):  # noqa: ANN202
            self.opened.append("transaction")
            return sessions.begin()

        def __call__(self):  # noqa: ANN204
            self.opened.append("session")
            return sessions()

    settings.update(Counting(), "look", DEFAULTS, {"size": 2})

    assert Counting.opened == ["transaction"]
    assert settings.get(sessions, "look", DEFAULTS)["size"] == 2
