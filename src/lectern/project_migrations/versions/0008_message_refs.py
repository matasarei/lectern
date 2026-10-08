"""What a tool result pointed at (its refs), kept with the message: a chat-started OCR
session's line in the reply card, and a finished session's Open/Export, after a reload.

Revision ID: p0008
Revises: p0007
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0008"
down_revision = "p0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("refs", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("refs")
