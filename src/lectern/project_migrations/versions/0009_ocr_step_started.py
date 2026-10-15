"""When an OCR page's current step began: its reading or its cleanup, from the moment the
model was its own. The page shows how long that step has been going, and a reload doesn't
start the count again.

Revision ID: p0009
Revises: p0008
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0009"
down_revision = "p0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ocr_pages") as batch:
        batch.add_column(sa.Column("step_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ocr_pages") as batch:
        batch.drop_column("step_started_at")
