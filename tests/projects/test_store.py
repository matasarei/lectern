from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select

from lectern.core.auth import LOCAL_USER, User
from lectern.core.tables import UserRow
from lectern.projects.store import ProjectNotFound, ProjectStore, folder_name
from lectern.projects.tables import SessionRow


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
