"""Initial app database: users (with the single local user), settings, projects.

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    users = op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("root_kind", sa.String(16), nullable=False, server_default="managed"),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("root_kind IN ('managed', 'linked')", name="root_kind"),
    )
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])
    op.bulk_insert(
        users,
        [{"id": 1, "name": "local", "created_at": datetime.now(UTC)}],
    )


def downgrade() -> None:
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_table("projects")
    op.drop_table("settings")
    op.drop_table("users")
