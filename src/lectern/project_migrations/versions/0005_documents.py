"""OCR Documents (M3a): each page's cleanup, the session's Document, and block labels for
the chat's search (`#b3.2` beside a chunk's number).

Revision ID: p0005
Revises: p0004
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0005"
down_revision = "p0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ocr_pages", recreate="always") as batch:
        batch.add_column(sa.Column("reading", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(
            sa.Column("clean_status", sa.String(16), nullable=False, server_default="pending")
        )
        batch.add_column(sa.Column("clean_error", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("cleaned_reading", sa.Integer(), nullable=False, server_default="0")
        )
        batch.create_check_constraint(
            "clean_status", "clean_status IN ('pending', 'cleaning', 'done', 'failed')"
        )
    with op.batch_alter_table("ocr_sessions") as batch:
        batch.add_column(
            sa.Column("document_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "document_artifact_id",
                sa.Integer(),
                # batch mode can only add a foreign key that has a name
                sa.ForeignKey("artifacts.id", name="fk_ocr_sessions_document_artifact"),
                nullable=True,
            )
        )
    op.create_table(
        "artifact_chunk_labels",
        sa.Column("artifact_id", sa.Integer(), sa.ForeignKey("artifacts.id"), primary_key=True),
        sa.Column("chunk", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("artifact_chunk_labels")
    with op.batch_alter_table("ocr_sessions") as batch:
        batch.drop_column("document_artifact_id")
        batch.drop_column("document_version")
    with op.batch_alter_table("ocr_pages", recreate="always") as batch:
        batch.drop_constraint("clean_status", type_="check")
        batch.drop_column("cleaned_reading")
        batch.drop_column("clean_error")
        batch.drop_column("clean_status")
        batch.drop_column("reading")
