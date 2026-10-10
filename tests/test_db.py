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
