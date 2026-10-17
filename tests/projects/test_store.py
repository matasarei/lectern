from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from lectern.core.auth import LOCAL_USER, User
from lectern.core.tables import ProjectRow, UserRow
from lectern.projects.store import NameMismatch, ProjectNotFound, ProjectStore, folder_name
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


def test_a_project_at_p0007_keeps_its_messages_and_gains_their_refs(store, paths):
    from lectern.projects.tables import MessageRow

    project = store.create(LOCAL_USER, "Before message refs")
    with store.db(project).begin() as session:
        chat = SessionRow(owner_id=LOCAL_USER.id, kind="chat", title="Chat")
        session.add(chat)
        session.flush()
        session.add(MessageRow(session_id=chat.id, role="tool", content="lease.md · file"))
    store.dispose()
    _migrate_to(project, "p0007")  # as a project made before the OCR line in the chat
    _migrate_to(project, "head")

    fresh = ProjectStore(paths, store.app_sessions)
    with fresh.db(project)() as session:
        row = session.scalars(select(MessageRow)).one()
    fresh.dispose()
    assert (row.content, row.refs) == ("lease.md · file", None)


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
    with pytest.raises(IntegrityError), fresh.db(project).begin() as session:
        session.add(OcrDecisionRow(**decision | {"doubt_id": "d3.2"}, action="skip",
                                   decided_by=LOCAL_USER.id))  # fmt: skip
    fresh.dispose()


def test_a_project_at_p0006_gets_follow_ups_in_its_chats_state(store, paths):
    from lectern.agent import followups

    project = store.create(LOCAL_USER, "Before M3c")
    store.dispose()
    _migrate_to(project, "p0006")  # as a project made in M3b is
    _migrate_to(project, "head")

    fresh = ProjectStore(paths, store.app_sessions)
    db = fresh.db(project)
    followups.add(db, 1, ocr_session_id=2, tool="export.run", arguments={}, label="export")
    assert [f.label for f in followups.pending(db, 1)] == ["export"]
    fresh.dispose()


# --- removing a project ------------------------------------------------------------------


class _Clock:
    """The time the store goes by, moved by the test."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def timed(store, clock):
    """The store, on the test's clock."""
    store.clock = clock
    return store


@pytest.mark.parametrize("typed", ["lease 1998", "Lease 1998 ", " Lease 1998", "", "Lease"])
def test_a_project_is_removed_only_by_its_name_typed_exactly(timed, typed):
    project = timed.create(LOCAL_USER, "Lease 1998")
    with pytest.raises(NameMismatch):
        timed.remove(LOCAL_USER, project.id, typed)
    assert timed.get(LOCAL_USER, project.id).root.is_dir()


def test_a_removed_project_is_gone_and_its_folder_is_in_the_trash(timed, paths):
    project = timed.create(LOCAL_USER, "Lease 1998")
    twin = timed.create(LOCAL_USER, "Lease 1998")  # same name, its own folder
    (project.root / "scan.pdf").write_bytes(b"%PDF")
    timed.db(project)  # its database is open, as after any request

    removed = timed.remove(LOCAL_USER, project.id, "Lease 1998")
    assert removed.root == project.root  # not moved yet: what runs in it is stopped first
    for gone in (timed.get, lambda user, pid: timed.rename(user, pid, "x")):
        with pytest.raises(ProjectNotFound):
            gone(LOCAL_USER, project.id)
    assert [p.id for p in timed.list(LOCAL_USER)] == [twin.id]
    with pytest.raises(ProjectNotFound):  # removing it again: it isn't there to remove
        timed.remove(LOCAL_USER, project.id, "Lease 1998")

    timed.move_to_trash(removed)
    trashed = paths.projects_trash("local") / "Lease 1998 20261015-093000"
    assert (trashed / "scan.pdf").read_bytes() == b"%PDF"
    assert not project.root.exists()
    assert twin.root.is_dir() and timed.get(LOCAL_USER, twin.id).root == twin.root
    assert project.id not in timed._dbs


def test_someone_elses_project_cant_be_removed(timed):
    with timed.app_sessions.begin() as session:
        session.add(UserRow(id=2, name="other"))
    theirs = timed.create(User(id=2, name="other"), "Private")
    with pytest.raises(ProjectNotFound):
        timed.remove(LOCAL_USER, theirs.id, "Private")
    assert timed.get(User(id=2, name="other"), theirs.id).root.is_dir()


def test_two_removed_projects_with_one_name_keep_their_own_folders_in_the_trash(timed, paths):
    first = timed.create(LOCAL_USER, "Notes")
    second = timed.create(LOCAL_USER, "Notes")
    for project in (first, second):
        timed.move_to_trash(timed.remove(LOCAL_USER, project.id, "Notes"))
    names = sorted(p.name for p in paths.projects_trash("local").iterdir())
    assert names == ["Notes 2 20261015-093000", "Notes 20261015-093000"]


def test_a_removed_project_is_deleted_after_30_days_and_not_before(timed, clock, paths):
    project = timed.create(LOCAL_USER, "Old")
    kept = timed.create(LOCAL_USER, "Kept")
    timed.move_to_trash(timed.remove(LOCAL_USER, project.id, "Old"))
    trashed = next(paths.projects_trash("local").iterdir())

    clock.now += timedelta(days=29, hours=23)
    assert timed.purge_removed() == 0
    assert trashed.is_dir()

    clock.now += timedelta(hours=2)
    assert timed.purge_removed() == 1
    assert not trashed.exists()
    with timed.app_sessions() as session:
        assert session.get(ProjectRow, project.id) is None
    assert timed.purge_removed() == 0  # and again: nothing more to do
    assert [p.id for p in timed.list(LOCAL_USER)] == [kept.id]


def test_a_folder_that_cant_be_moved_stays_and_is_purged_from_where_it_is(timed, clock):
    project = timed.create(LOCAL_USER, "Busy")
    removed = timed.remove(LOCAL_USER, project.id, "Busy")
    timed.paths.projects_trash("local").write_text("a file where the trash folder goes")

    timed.move_to_trash(removed)  # the trash can't be made: logged, and nothing is moved
    assert project.root.is_dir()
    with pytest.raises(ProjectNotFound):
        timed.get(LOCAL_USER, project.id)

    clock.now += timedelta(days=31)
    assert timed.purge_removed() == 1
    assert not project.root.exists()


def test_a_folder_that_refuses_the_move_leaves_the_project_removed(timed, monkeypatch):
    project = timed.create(LOCAL_USER, "Locked")
    removed = timed.remove(LOCAL_USER, project.id, "Locked")

    def refuse(self, target):  # noqa: ANN001, ARG001 — as Windows does with a file open in it
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "rename", refuse)
    timed.move_to_trash(removed)  # logged, not raised: the project is removed either way
    monkeypatch.undo()
    assert project.root.is_dir()
    with timed.app_sessions() as session:
        assert session.get(ProjectRow, project.id).root_path == str(project.root)


def test_a_removed_project_whose_folder_was_deleted_by_hand_is_purged_too(timed, clock):
    project = timed.create(LOCAL_USER, "Gone")
    timed.move_to_trash(timed.remove(LOCAL_USER, project.id, "Gone"))
    with timed.app_sessions() as session:
        shutil.rmtree(session.get(ProjectRow, project.id).root_path)
    clock.now += timedelta(days=31)
    assert timed.purge_removed() == 1


def test_a_removed_linked_project_keeps_its_folder_where_it_is(timed, clock, tmp_path, paths):
    """A linked folder is the person's own: removing the project never moves or deletes it."""
    mine = tmp_path / "Documents/lease"
    mine.mkdir(parents=True)
    (mine / "contract.docx").write_bytes(b"PK")
    with timed.app_sessions.begin() as session:
        row = ProjectRow(
            owner_id=LOCAL_USER.id, name="Lease", root_kind="linked", root_path=str(mine)
        )
        session.add(row)
        session.flush()
        project_id = row.id

    timed.move_to_trash(timed.remove(LOCAL_USER, project_id, "Lease"))
    assert (mine / "contract.docx").read_bytes() == b"PK"
    assert not paths.projects_trash("local").exists()

    clock.now += timedelta(days=31)
    assert timed.purge_removed() == 1
    assert (mine / "contract.docx").read_bytes() == b"PK"
    with timed.app_sessions() as session:
        assert session.get(ProjectRow, project_id) is None


def test_a_project_looked_up_before_its_removal_doesnt_bring_its_folder_back(timed):
    project = timed.create(LOCAL_USER, "Lease")  # a request has it, from before the removal
    timed.move_to_trash(timed.remove(LOCAL_USER, project.id, "Lease"))
    with pytest.raises(ProjectNotFound):
        timed.db(project)
    assert not project.root.exists()


def test_a_project_at_p0008_keeps_its_ocr_pages_with_no_step_started(store, paths):
    from lectern.projects.tables import ArtifactRow, OcrPageRow, OcrSessionRow

    project = store.create(LOCAL_USER, "Before step times")
    with store.db(project).begin() as session:
        source = ArtifactRow(owner_id=LOCAL_USER.id, path="scan.pdf", kind="imported",
                             mime="application/pdf", size=1, sha256="0" * 64,
                             from_kind="import")  # fmt: skip
        session.add(source)
        ocr = SessionRow(owner_id=LOCAL_USER.id, kind="ocr", title="scan.pdf")
        session.add(ocr)
        session.flush()
        session.add(OcrSessionRow(session_id=ocr.id, source_artifact_id=source.id,
                                  folder="scan", status="done", pages_total=1))  # fmt: skip
        session.add(OcrPageRow(session_id=ocr.id, number=1, key="p001", status="done"))
    store.dispose()
    _migrate_to(project, "p0008")  # as a project made before the OCR steps were timed
    _migrate_to(project, "head")

    fresh = ProjectStore(paths, store.app_sessions)
    with fresh.db(project)() as session:
        page = session.scalars(select(OcrPageRow)).one()
    fresh.dispose()
    assert (page.status, page.step_started_at) == ("done", None)
