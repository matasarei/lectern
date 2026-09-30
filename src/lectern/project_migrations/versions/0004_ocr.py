"""OCR sessions (M2b): the `ocr` session kind, each session's state, and its pages.

The tables stay in the core project migrations, like `script_runs`: OCR changes the core
`sessions` table (its kind check) anyway.

Revision ID: p0004
Revises: p0003
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0004"
down_revision = "p0003"
branch_labels = None
depends_on = None


def _kinds(kinds: str) -> None:
    # SQLite can't change a CHECK constraint, so batch mode rebuilds the table: copy, drop,
    # rename. That needs foreign keys off (messages point at sessions); migrate() runs every
    # migration that way and checks them before committing.
    with op.batch_alter_table("sessions", recreate="always") as batch:
        batch.drop_constraint("kind", type_="check")
        batch.create_check_constraint("kind", f"kind IN ({kinds})")


def upgrade() -> None:
    _kinds("'chat', 'ocr'")
    op.create_table(
        "ocr_sessions",
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column(
            "source_artifact_id", sa.Integer(), sa.ForeignKey("artifacts.id"), nullable=False
        ),
        sa.Column("folder", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("pages_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'done', 'failed')", name="status"
        ),
    )
    op.create_table(
        "ocr_pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("lease_key", sa.String(128), nullable=True),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("handwritten", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("seconds", sa.Float(), nullable=True),
        sa.Column("cut_off", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("looped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("has_hint", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'recognising', 'done', 'failed')", name="status"),
        sa.UniqueConstraint("session_id", "number", name="uq_ocr_pages_session_number"),
    )
    op.create_index("ix_ocr_pages_session_id", "ocr_pages", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_ocr_pages_session_id", table_name="ocr_pages")
    op.drop_table("ocr_pages")
    op.drop_table("ocr_sessions")
    op.execute("DELETE FROM sessions WHERE kind = 'ocr'")
    _kinds("'chat'")
