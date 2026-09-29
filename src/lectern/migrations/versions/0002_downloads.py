"""Downloads: one row per catalog model being (or already) downloaded.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "downloads",
        sa.Column("model_id", sa.String(128), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("bytes_done", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_total", sa.BigInteger(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'downloading', 'verifying', 'done', 'failed', 'paused')",
            name="status",
        ),
    )


def downgrade() -> None:
    op.drop_table("downloads")
