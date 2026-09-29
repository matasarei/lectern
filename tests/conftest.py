from __future__ import annotations

from pathlib import Path

import pytest

from lectern.config import Paths


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    """A throwaway Lectern home, so no test touches the real app-data folder."""
    home = tmp_path / "lectern-home"
    home.mkdir()
    monkeypatch.setenv("LECTERN_HOME", str(home))
    return Paths(home=home)
