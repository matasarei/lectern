"""A card keeps its question, and one cut off before it was answered (Stop, or Lectern shutting
down) is "cancelled" with why, so the chat can still show it after a reload. Cards made before
this have no question kept; one that was cut off stays "declined".

Revision ID: p0012
Revises: p0011
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0012"
down_revision = "p0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("confirmations", recreate="always") as batch:
        batch.add_column(sa.Column("text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("reason", sa.String(16), nullable=True))
        batch.drop_constraint("status", type_="check")
        batch.create_check_constraint(
            "status", "status IN ('pending', 'approved', 'declined', 'cancelled')"
        )


def downgrade() -> None:
    # Cut off was recorded as declined before.
    op.execute("UPDATE confirmations SET status = 'declined' WHERE status = 'cancelled'")
    with op.batch_alter_table("confirmations", recreate="always") as batch:
        batch.drop_constraint("status", type_="check")
        batch.create_check_constraint("status", "status IN ('pending', 'approved', 'declined')")
        batch.drop_column("reason")
        batch.drop_column("text")
