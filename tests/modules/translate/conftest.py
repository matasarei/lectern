from __future__ import annotations

import pytest

from lectern.catalog import catalog
from lectern.modules.translate import languages

# Measured scores for the tests, whatever the shipped file says: a translation model scores
# by its id (else OTHER), the 4 GB card's chat model CHAT, for every language it translates.
SCORE = {"salamandrata-2b": 60.0}
OTHER = 55.0
CHAT = 50.0


@pytest.fixture(autouse=True)
def measured(monkeypatch):
    """Every translation model measured better than the chat model, Salamandra the best. Read
    when asked, so a model a test adds to the catalog is measured too."""

    def scores() -> dict[str, dict[str, float]]:
        translators = catalog.load().choices_for("translate")
        every = sorted({code for m in translators for code in m.translate.languages})
        found = {
            m.id: {code: SCORE.get(m.id, OTHER) for code in m.translate.languages}
            for m in translators
        }
        return {**found, "qwen3.5-4b": dict.fromkeys(every, CHAT)}

    monkeypatch.setattr(languages, "scores", scores)
