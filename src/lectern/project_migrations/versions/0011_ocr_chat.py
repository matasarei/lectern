"""The chat that started an OCR session (ocr.start): once its questions are answered, the OCR
tab offers the way back to it. None for a session started from the Files tab, and for those
made before this was kept.

Revision ID: p0011
Revises: p0010
Create Date: 2026-10-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0011"
down_revision = "p0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ocr_sessions", recreate="always") as batch:
        batch.add_column(sa.Column("chat_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_ocr_sessions_chat_id", "sessions", ["chat_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("ocr_sessions", recreate="always") as batch:
        batch.drop_constraint("fk_ocr_sessions_chat_id", type_="foreignkey")
        batch.drop_column("chat_id")
