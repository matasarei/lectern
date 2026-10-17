"""Projects: when one was removed. A removed project's folder waits in its owner's trash for
30 days, then goes for good with its row; until then nothing shows or opens it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Plain ALTER TABLE: a batch rebuild of the table would have to carry its CHECK along.
    op.drop_column("projects", "removed_at")
