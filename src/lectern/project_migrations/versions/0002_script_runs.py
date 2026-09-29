"""Script runs from the Sandbox module.

The table lives in the core project migrations for now: modules get their own migration
trees with OCR (M2), which needs them. It moves then, if it has to.

Revision ID: p0002
Revises: p0001
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0002"
down_revision = "p0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "script_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("script_id", sa.Integer(), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("script_sha", sa.String(64), nullable=False),
        sa.Column("inputs_sha", sa.String(64), nullable=False),
        sa.Column("test_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stdout", sa.Text(), nullable=False, server_default=""),
        sa.Column("stderr", sa.Text(), nullable=False, server_default=""),
        sa.Column("outputs", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_script_runs_script_id", "script_runs", ["script_id"])


def downgrade() -> None:
    op.drop_index("ix_script_runs_script_id", table_name="script_runs")
    op.drop_table("script_runs")
