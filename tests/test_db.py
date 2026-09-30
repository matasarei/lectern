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
