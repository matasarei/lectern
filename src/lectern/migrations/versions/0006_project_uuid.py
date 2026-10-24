"""Projects: a UUID each, set once. A new project's folder is named after it, so renaming the
project never has to move the folder; and it names the project the same way on every
install, for sharing later. Projects made before keep their folders; they get a UUID too.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-24
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ADD COLUMN and CREATE INDEX: a batch rebuild of the table would have to carry its
    # CHECK along. So the column stays nullable here; the app always sets it.
    op.add_column("projects", sa.Column("uuid", sa.String(36), nullable=True))
    connection = op.get_bind()
    ids = connection.execute(sa.text("SELECT id FROM projects WHERE uuid IS NULL")).scalars()
    for project_id in list(ids):
        connection.execute(
            sa.text("UPDATE projects SET uuid = :uuid WHERE id = :id"),
            {"uuid": str(uuid.uuid4()), "id": project_id},
        )
    op.create_index("ix_projects_uuid", "projects", ["uuid"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_projects_uuid", "projects")
    op.drop_column("projects", "uuid")
