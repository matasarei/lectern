from __future__ import annotations

import pytest

from lectern.core.db import foreign_keys_checked_at_end, make_engine


def test_a_migration_that_leaves_rows_pointing_at_nothing_is_rolled_back(tmp_path):
    engine = make_engine(tmp_path / "x.sqlite")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        c.exec_driver_sql("CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER "
                          "REFERENCES parent(id))")  # fmt: skip

    with engine.connect() as c:
        with (
            pytest.raises(RuntimeError, match="rows pointing at nothing"),
            foreign_keys_checked_at_end(c),
        ):
            c.exec_driver_sql("INSERT INTO child (parent_id) VALUES (42)")
        assert c.exec_driver_sql("SELECT count(*) FROM child").scalar() == 0
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1  # back on afterwards
    engine.dispose()


def _upgrade(engine, revision: str) -> None:  # noqa: ANN001
    """As lectern.core.db.migrate does, to a revision other than the latest."""
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", "lectern:migrations")
    with engine.connect() as connection, foreign_keys_checked_at_end(connection):
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def test_downloads_made_before_revisions_were_recorded_get_the_pin_of_that_time(tmp_path):
    from sqlalchemy import text

    from lectern.core.db import migrate

    engine = make_engine(tmp_path / "app.sqlite")
    _upgrade(engine, "0002")
    rows = [
        ("qwen3.5-9b", "done"),  # downloaded: it's at the only pin the model has had
        ("qwen3.5-4b", "paused"),  # half there: nothing to say about it yet
        ("made-up-model", "done"),  # not a model of that time
    ]
    with engine.begin() as connection:
        for model_id, status in rows:
            connection.execute(
                text(
                    "INSERT INTO downloads (model_id, status, bytes_done, bytes_total, updated_at)"
                    " VALUES (:id, :status, 1, 1, '2026-10-01 00:00:00')"
                ),
                {"id": model_id, "status": status},
            )

    migrate(engine)
    migrate(engine)  # and again: nothing more to do

    with engine.connect() as connection:
        revisions = dict(connection.execute(text("SELECT model_id, revision FROM downloads")).all())
    assert revisions == {
        "qwen3.5-9b": "3885219b6810b007914f3a7950a8d1b469d598a5",
        "qwen3.5-4b": None,
        "made-up-model": None,
    }


def test_accounts_are_added_to_an_install_that_had_one_local_user(tmp_path):
    """0004: the local user becomes the admin, with no password yet, and keeps its projects."""
    import sqlalchemy as sa
    from sqlalchemy import text

    from lectern.core.db import migrate

    engine = make_engine(tmp_path / "app.sqlite")
    _upgrade(engine, "0003")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (owner_id, name, root_kind, root_path, created_at)"
                " VALUES (1, 'Lease', 'managed', '/p/lease', '2026-10-01 00:00:00')"
            )
        )

    migrate(engine)
    migrate(engine)  # and again: nothing more to do

    with engine.connect() as connection:
        user = connection.execute(text("SELECT * FROM users")).mappings().one()
        owners = connection.execute(text("SELECT owner_id, name FROM projects")).all()
        tables = sa.inspect(connection).get_table_names()
    assert (user["id"], user["name"], user["role"]) == (1, "local", "admin")
    assert (user["login"], user["password_hash"], user["locked_until"]) == (None, None, None)
    assert (user["disabled"], user["failed_logins"]) == (0, 0)
    assert owners == [(1, "Lease")]
    assert "logins" in tables
    engine.dispose()


def test_projects_made_before_removal_existed_are_not_removed(tmp_path):
    """0005: every project there was is still a project (removed_at is empty)."""
    from sqlalchemy import text

    from lectern.core.db import migrate

    engine = make_engine(tmp_path / "app.sqlite")
    _upgrade(engine, "0004")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (owner_id, name, root_kind, root_path, created_at)"
                " VALUES (1, 'Lease', 'managed', '/p/lease', '2026-10-01 00:00:00')"
            )
        )

    migrate(engine)
    migrate(engine)  # and again: nothing more to do

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT name, removed_at FROM projects")).all()
    assert rows == [("Lease", None)]
    engine.dispose()


def test_projects_made_before_uuids_get_one_each_and_keep_their_folders(tmp_path):
    """0006: a UUID for every project there was, each its own; the folder isn't renamed."""
    import uuid

    import sqlalchemy as sa
    from sqlalchemy import text

    from lectern.core.db import migrate

    engine = make_engine(tmp_path / "app.sqlite")
    _upgrade(engine, "0005")
    with engine.begin() as connection:
        for name in ("Lease", "Untitled project"):
            connection.execute(
                text(
                    "INSERT INTO projects (owner_id, name, root_kind, root_path, created_at)"
                    " VALUES (1, :name, 'managed', :path, '2026-10-01 00:00:00')"
                ),
                {"name": name, "path": f"/p/{name}"},
            )

    migrate(engine)
    migrate(engine)  # and again: nothing more to do

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT name, root_path, uuid FROM projects")).all()
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(text("UPDATE projects SET uuid = :u"), {"u": rows[0].uuid})
    assert [(r.name, r.root_path) for r in rows] == [
        ("Lease", "/p/Lease"),
        ("Untitled project", "/p/Untitled project"),
    ]
    assert len({r.uuid for r in rows}) == 2
    assert all(str(uuid.UUID(r.uuid, version=4)) == r.uuid for r in rows)
    engine.dispose()


def test_the_accounts_tables_refuse_what_cant_be(tmp_path):
    import sqlalchemy as sa
    from sqlalchemy import text

    from lectern.core.db import migrate

    engine = make_engine(tmp_path / "app.sqlite")
    migrate(engine)
    add = text(
        "INSERT INTO users (name, login, role, disabled, failed_logins, created_at)"
        " VALUES (:name, :login, :role, 0, 0, '2026-10-11 00:00:00')"
    )
    with engine.begin() as connection:
        connection.execute(add, {"name": "olena", "login": "olena", "role": "member"})
    for clash in (
        {"name": "olena2", "login": "olena", "role": "member"},  # a sign-in name taken
        {"name": "boss", "login": "boss", "role": "owner"},  # no such role
    ):
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
            connection.execute(add, clash)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(  # a login for a user that doesn't exist
            text(
                "INSERT INTO logins (id_hash, user_id, token, created_at, last_seen_at,"
                " expires_at) VALUES ('h', 99, 't', '2026-10-11', '2026-10-11', '2026-11-10')"
            )
        )
    engine.dispose()
