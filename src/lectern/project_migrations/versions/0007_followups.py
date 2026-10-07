"""The chat's pending follow-ups (M3c): tool calls queued to run when an OCR session
finishes (task.after), kept in the chat's state record beside its pins.

Revision ID: p0007
Revises: p0006
Create Date: 2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0007"
down_revision = "p0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("chat_state") as batch:
        batch.add_column(sa.Column("followups", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("chat_state") as batch:
        batch.drop_column("followups")
