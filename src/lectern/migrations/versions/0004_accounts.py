"""Accounts: what a user needs to sign in (a sign-in name, a password hash, a role), and the
logins table, one row per browser signed in.

The one local user becomes the admin. It has no password yet: nothing asks for one until
`lectern user init` gives it a sign-in name and a password.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("login", sa.String(64), nullable=True))
        batch.add_column(sa.Column("password_hash", sa.Text(), nullable=True))
        batch.add_column(sa.Column("role", sa.String(16), nullable=False, server_default="member"))
        batch.add_column(
            sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_users_login", ["login"])
        batch.create_check_constraint("role", "role IN ('admin', 'member')")
    users = sa.table("users", sa.column("id", sa.Integer), sa.column("role", sa.String))
    # One row: the user every install has had since 0001. It was the only one, so it is the
    # admin.
    op.execute(users.update().where(users.c.id == 1).values(role="admin"))
    op.create_table(
        "logins",
        # The SHA-256 of the id in the browser's cookie: reading this table gives nobody a
        # way in.
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_logins_user_id", "logins", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_logins_user_id", table_name="logins")
    op.drop_table("logins")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("role", type_="check")
        batch.drop_constraint("uq_users_login", type_="unique")
        for column in (
            "locked_until", "failed_logins", "disabled", "role", "password_hash", "login",
        ):  # fmt: skip
            batch.drop_column(column)
