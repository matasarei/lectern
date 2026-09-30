"""The app database: one SQLite file in the Lectern home, migrated on start."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import Request
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def make_engine(db_file: Path) -> Engine:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_file}")

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def migrate(engine: Engine, script_location: str = "lectern:migrations") -> None:
    """Bring a database to the latest schema. Migrations ship inside the package: the app
    database's in `lectern:migrations`, each project database's in
    `lectern:project_migrations`."""
    config = Config()
    config.set_main_option("script_location", script_location)
    with engine.connect() as connection, foreign_keys_checked_at_end(connection):
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@contextlib.contextmanager
def foreign_keys_checked_at_end(connection: Connection) -> Iterator[None]:
    """A transaction with foreign keys off, checked before it commits. SQLite changes a table
    by rebuilding it (copy, drop, rename), and dropping a table other rows point at fails
    with foreign keys on; deferring doesn't help, since the rename never clears the count.
    This is SQLite's own recipe: the pragma only works outside a transaction, so it is set
    before one starts and `foreign_key_check` runs before it commits."""
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.commit()
    try:
        with connection.begin():
            yield
            broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(f"A migration left rows pointing at nothing: {broken[:5]}")
    finally:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: a session on the app database."""
    with request.app.state.sessions() as session:
        yield session
