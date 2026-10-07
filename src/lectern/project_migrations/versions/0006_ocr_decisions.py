"""OCR review (M3b): the person's decisions on a Document's doubts.

Revision ID: p0006
Revises: p0005
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0006"
down_revision = "p0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ocr_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("doubt_id", sa.String(32), nullable=False),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('accept', 'edit')", name="action"),
    )
    op.create_index("ix_ocr_decisions_session_id", "ocr_decisions", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_ocr_decisions_session_id", "ocr_decisions")
    op.drop_table("ocr_decisions")
