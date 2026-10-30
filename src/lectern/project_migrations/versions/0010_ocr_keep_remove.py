"""Two more answers to an OCR question: keep the reading as it is, or remove its block (an
invented fragment).

Revision ID: p0010
Revises: p0009
Create Date: 2026-10-30
"""

from __future__ import annotations

from alembic import op

revision = "p0010"
down_revision = "p0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ocr_decisions", recreate="always") as batch:
        batch.drop_constraint("action", type_="check")
        batch.create_check_constraint("action", "action IN ('accept', 'edit', 'keep', 'remove')")


def downgrade() -> None:
    # A kept reading was an edit to the same text; a removed block has no older answer.
    op.execute("UPDATE ocr_decisions SET action = 'edit' WHERE action = 'keep'")
    op.execute("DELETE FROM ocr_decisions WHERE action = 'remove'")
    with op.batch_alter_table("ocr_decisions", recreate="always") as batch:
        batch.drop_constraint("action", type_="check")
        batch.create_check_constraint("action", "action IN ('accept', 'edit')")
