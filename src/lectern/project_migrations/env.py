"""Alembic environment for project databases (`<project>/.lectern/project.sqlite`).
Run in-process by lectern.core.db.migrate(engine, "lectern:project_migrations")."""

from __future__ import annotations

from alembic import context

from lectern.projects.tables import ProjectBase

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Run migrations through lectern.core.db.migrate(), not the alembic CLI.")

context.configure(
    connection=connection,
    target_metadata=ProjectBase.metadata,
    render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
)
with context.begin_transaction():
    context.run_migrations()
