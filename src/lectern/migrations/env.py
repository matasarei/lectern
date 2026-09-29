"""Alembic environment. Lectern runs migrations in-process (see lectern.core.db.migrate),
passing an open connection; there is no alembic.ini."""

from __future__ import annotations

from alembic import context

from lectern.core.tables import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Run migrations through lectern.core.db.migrate(), not the alembic CLI.")

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
)
with context.begin_transaction():
    context.run_migrations()
