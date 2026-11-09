"""Image timings: how long this computer took for its last image of a model at a size, for the
expected wait on the image card.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_timings",
        sa.Column("model_id", sa.String(128), primary_key=True),
        sa.Column("width", sa.Integer(), primary_key=True),
        sa.Column("height", sa.Integer(), primary_key=True),
        sa.Column("load_seconds", sa.Float(), nullable=False),
        sa.Column("step_seconds", sa.Float(), nullable=False),
        sa.Column("decode_seconds", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("image_timings")
