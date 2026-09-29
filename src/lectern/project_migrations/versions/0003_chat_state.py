"""Each chat's state record: pinned items and the summary of older turns.

Revision ID: p0003
Revises: p0002
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0003"
down_revision = "p0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_state",
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("pinned", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_upto", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("chat_state")
