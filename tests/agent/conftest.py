from __future__ import annotations

import pytest

from lectern.agent.tools import ToolContext
from lectern.core.auth import LOCAL_USER
from lectern.projects import files
from lectern.projects.files import Origin


@pytest.fixture
def ctx(store):
    project = store.create(LOCAL_USER, "Lease archive")
    return ToolContext(project=project, sessions=store.db(project), user=LOCAL_USER, session_id=1)


def add_file(ctx: ToolContext, name: str, text: str, origin: Origin | None = None) -> int:
    with ctx.sessions.begin() as session:
        row = files.write_text(
            ctx.project.root,
            session,
            owner_id=ctx.user.id,
            name=name,
            text=text,
            origin=origin or Origin("import"),
        )
        return row.id
