"""A project's database: sessions, the transcript, confirmations, artifacts with their
origin, the full-text index and UI state.

Revision ID: p0001
Revises:
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("titled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('chat')", name="kind"),
    )
    op.create_index("ix_sessions_owner_id", "sessions", ["owner_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_calls", sa.Text(), nullable=True),
        sa.Column("tool_call_id", sa.String(128), nullable=True),
        sa.Column("tool_name", sa.String(64), nullable=True),
        sa.Column("attachments", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant', 'tool')", name="role"),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"])
    op.create_table(
        "confirmations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("arguments", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'approved', 'declined')", name="status"),
    )
    op.create_index("ix_confirmations_session_id", "confirmations", ["session_id"])
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("text_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("text_note", sa.Text(), nullable=True),
        sa.Column("from_kind", sa.String(16), nullable=False),
        sa.Column("from_id", sa.Integer(), nullable=True),
        sa.Column("from_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trash_path", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('imported', 'generated')", name="kind"),
        sa.CheckConstraint("from_kind IN ('import', 'session', 'artifact')", name="from_kind"),
        sa.CheckConstraint("text_status IN ('none', 'indexed', 'failed')", name="text_status"),
    )
    op.create_index("ix_artifacts_owner_id", "artifacts", ["owner_id"])
    op.create_table(
        "ui_state",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    # Full-text search over the text of every artifact, in chunks (block references #c<n>).
    op.execute(
        "CREATE VIRTUAL TABLE artifact_text USING fts5("
        "artifact_id UNINDEXED, chunk UNINDEXED, heading, body, "
        "tokenize = 'unicode61 remove_diacritics 2')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE artifact_text")
    op.drop_table("ui_state")
    op.drop_index("ix_artifacts_owner_id", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index("ix_confirmations_session_id", table_name="confirmations")
    op.drop_table("confirmations")
    op.drop_index("ix_messages_session_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_sessions_owner_id", table_name="sessions")
    op.drop_table("sessions")
