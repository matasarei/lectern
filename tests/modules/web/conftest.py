from __future__ import annotations

import pytest

from lectern.agent.tools import ToolContext
from lectern.core.auth import LOCAL_USER


@pytest.fixture
def ctx(store):
    project = store.create(LOCAL_USER, "Research")
    return ToolContext(project=project, sessions=store.db(project), user=LOCAL_USER, session_id=1)
