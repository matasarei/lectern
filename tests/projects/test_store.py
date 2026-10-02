from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select

from lectern.core.auth import LOCAL_USER, User
from lectern.core.tables import UserRow
from lectern.projects.store import ProjectNotFound, ProjectStore, folder_name
from lectern.projects.tables import ConfirmationRow, MessageRow, SessionRow


def test_a_new_project_is_a_folder_with_its_database_and_one_empty_chat(store, paths):
    project = store.create(LOCAL_USER, "Lease archive 1998")

    assert project.root == paths.home / "users" / "local" / "projects" / "Lease archive 1998"
    assert (project.root / ".lectern" / "project.sqlite").is_file()
    with store.db(project)() as session:
        chats = session.scalars(select(SessionRow)).all()
    assert [(c.kind, c.title, c.owner_id) for c in chats] == [("chat", "New chat", LOCAL_USER.id)]


def test_projects_with_the_same_name_get_their_own_folders(store):
    first = store.create(LOCAL_USER, "Notes")
    second = store.create(LOCAL_USER, "Notes")
    assert (first.root.name, second.root.name) == ("Notes", "Notes 2")
    assert first.name == second.name == "Notes"


@pytest.mark.parametrize(
    ("name", "folder"),
    [
        ("a/b\\c:d", "a-b-c-d"),
        ("../..", "_-"),
        ("CON", "CON_"),
        ("nul.txt", "nul.txt_"),
        ("trailing. ", "trailing"),
        ("", "Untitled project"),
        ("  Договір\tоренди\n1998 ", "Договір оренди 1998"),
    ],
)
def test_folder_names_are_safe_everywhere(name, folder):
    assert folder_name(name) == folder


def test_folder_name_never_leaves_the_projects_root(store, paths):
    project = store.create(LOCAL_USER, "../../escape")
    assert project.root.parent == paths.projects_root("local")


def test_someone_elses_project_is_not_found(store):
    with store.app_sessions.begin() as session:
        session.add(UserRow(id=2, name="other"))
    other = User(id=2, name="other")
    theirs = store.create(other, "Private")

    with pytest.raises(ProjectNotFound):
        store.get(LOCAL_USER, theirs.id)
    with pytest.raises(ProjectNotFound):
        store.get(LOCAL_USER, 999)
    assert store.list(LOCAL_USER) == []
    assert [p.id for p in store.list(other)] == [theirs.id]


def test_rename_keeps_the_folder(store):
    project = store.create(LOCAL_USER, "Untitled project")
    renamed = store.rename(LOCAL_USER, project.id, "Payment terms\n")
    assert renamed.name == "Payment terms"
    assert renamed.root == project.root


def test_reopening_a_project_uses_the_migrated_database(store, paths):
    project = store.create(LOCAL_USER, "Reopen")
    store.dispose()

    fresh = ProjectStore(paths, store.app_sessions)
    with fresh.db(project)() as session:
        assert session.scalars(select(SessionRow)).one().title == "New chat"
    fresh.dispose()
    db = sqlite3.connect(project.root / ".lectern" / "project.sqlite")
    tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
    db.close()
    assert {
        "sessions",
        "messages",
        "confirmations",
        "artifacts",
        "ui_state",
        "artifact_text",
    } <= tables


def test_a_project_made_before_m1b_gets_the_script_runs_table(store, paths):
    from alembic import command
    from alembic.config import Config

    from lectern.core.db import make_engine

    project = store.create(LOCAL_USER, "Older")
    store.dispose()
    # Take it back to M1a's schema, as a project made then would be.
    engine = make_engine(project.root / ".lectern" / "project.sqlite")
    config = Config()
    config.set_main_option("script_location", "lectern:project_migrations")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "p0001")
    engine.dispose()

    fresh = ProjectStore(paths, store.app_sessions)
    fresh.db(project)
    fresh.dispose()
    db = sqlite3.connect(project.root / ".lectern" / "project.sqlite")
    tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
    version = db.execute("select version_num from alembic_version").fetchone()[0]
    db.close()
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(config).get_current_head()
    assert "script_runs" in tables and "chat_state" in tables
    assert version == head  # brought all the way up, whatever the latest revision is


def _migrate_to(project, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    from lectern.core.db import foreign_keys_checked_at_end, make_engine

    engine = make_engine(project.root / ".lectern" / "project.sqlite")
    config = Config()
    config.set_main_option("script_location", "lectern:project_migrations")
    with engine.connect() as connection, foreign_keys_checked_at_end(connection):
        config.attributes["connection"] = connection
        if revision == "head":
            command.upgrade(config, "head")
        else:
            command.downgrade(config, revision)
    engine.dispose()


def test_a_project_with_chats_takes_ocr_sessions_after_p0004(store, paths):
    project = store.create(LOCAL_USER, "Before OCR")
    with store.db(project).begin() as session:
        chat = session.scalars(select(SessionRow)).one()
        session.add(MessageRow(session_id=chat.id, role="user", content="Привіт"))
        session.add(
            ConfirmationRow(session_id=chat.id, tool_call_id="c1", tool="artifact.trash",
                            arguments="{}")
        )  # fmt: skip
    store.dispose()
    _migrate_to(project, "p0003")  # as a project made in M1c is

    _migrate_to(project, "head")  # the chat's messages point at the rebuilt sessions table

    fresh = ProjectStore(paths, store.app_sessions)
    with fresh.db(project).begin() as session:
        assert session.scalars(select(MessageRow)).one().content == "Привіт"
        assert session.scalars(select(ConfirmationRow)).one().tool == "artifact.trash"
        session.add(SessionRow(owner_id=LOCAL_USER.id, kind="ocr", title="lease-scan"))
    with fresh.db(project)() as session:
        assert {s.kind for s in session.scalars(select(SessionRow))} == {"chat", "ocr"}
    fresh.dispose()


def test_an_unknown_session_kind_is_still_refused(store):
    from sqlalchemy.exc import IntegrityError

    project = store.create(LOCAL_USER, "Kinds")
    with pytest.raises(IntegrityError), store.db(project).begin() as session:
        session.add(SessionRow(owner_id=LOCAL_USER.id, kind="stt", title="x"))


def test_a_project_with_an_ocr_session_migrates_to_p0005_with_its_pages_to_clean(store, paths):
    from lectern.projects.tables import ArtifactRow, OcrPageRow, OcrSessionRow

    project = store.create(LOCAL_USER, "OCR before M3a")
    with store.db(project).begin() as session:
        source = ArtifactRow(
            owner_id=LOCAL_USER.id,
            path="scan.pdf",
            kind="imported",
            mime="application/pdf",
            size=1,
            sha256="0" * 64,
            from_kind="import",
        )
        session.add(source)
        ocr = SessionRow(owner_id=LOCAL_USER.id, kind="ocr", title="scan.pdf")
        session.add(ocr)
        session.flush()
        session.add(
            OcrSessionRow(
                session_id=ocr.id,
                source_artifact_id=source.id,
                folder="scan",
                status="done",
                pages_total=1,
            )
        )
        session.add(OcrPageRow(session_id=ocr.id, number=1, key="p001", status="done"))
    store.dispose()
    _migrate_to(project, "p0004")  # as a project made in M2b is
    _migrate_to(project, "head")

    fresh = ProjectStore(paths, store.app_sessions)
    with fresh.db(project)() as session:
        page = session.scalars(select(OcrPageRow)).one()
        ocr_row = session.scalars(select(OcrSessionRow)).one()
    fresh.dispose()
    assert (page.status, page.clean_status, page.reading, page.cleaned_reading) == (
        "done",
        "pending",
        0,
        0,
    )
    assert (ocr_row.document_version, ocr_row.document_artifact_id) == (0, None)


def test_a_project_at_p0005_gets_the_table_of_decisions_on_doubts(store, paths):
    from sqlalchemy.exc import IntegrityError

    from lectern.projects.tables import OcrDecisionRow

    project = store.create(LOCAL_USER, "Before M3b")
    store.dispose()
    _migrate_to(project, "p0005")  # as a project made in M3a is
    _migrate_to(project, "head")

    fresh = ProjectStore(paths, store.app_sessions)
    with fresh.db(project).begin() as session:
        chat = session.scalars(select(SessionRow)).first()
        decision = dict(session_id=chat.id, page=3, doubt_id="d3.1", raw="1?", value="10")
        session.add(OcrDecisionRow(**decision, action="accept", decided_by=LOCAL_USER.id))
    with pytest.raises(IntegrityError), fresh.db(project).begin() as session:  # one per doubt
        session.add(OcrDecisionRow(**decision, action="edit", decided_by=LOCAL_USER.id))
    with pytest.raises(IntegrityError), fresh.db(project).begin() as session:
        session.add(OcrDecisionRow(**decision | {"doubt_id": "d3.2"}, action="skip",
                                   decided_by=LOCAL_USER.id))  # fmt: skip
    fresh.dispose()
