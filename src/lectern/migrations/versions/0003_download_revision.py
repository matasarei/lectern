"""Downloads: the revision each finished download fetched, so Models & memory can show a
model's version and tell when the catalog has moved on to a newer one.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# The revision the catalog pinned for each model when this was written. No pin had ever
# moved, so a download that finished before revisions were recorded fetched this one. Frozen
# here on purpose: the catalog of a later Lectern may pin something newer, and this must not.
PINS_AT_0003 = {
    "qwen3.5-4b": "e87f176479d0855a907a41277aca2f8ee7a09523",
    "qwen3.5-9b": "3885219b6810b007914f3a7950a8d1b469d598a5",
    "qwen3.5-35b-a3b": "bc014a17be43adabd7066b7a86075ff935c6a4e2",
    "qwen3.5-4b-mlx": "32f3e8ecf65426fc3306969496342d504bfa13f3",
    "qwen3.5-9b-mlx": "938d8919941c6e7efd3c7150eff7fe9d12afa631",
    "qwen3.5-35b-a3b-mlx": "1e20fd8d42056f870933bf98ca6211024744f7ec",
    "paddleocr-vl-1.6": "511b09642bb324401f15f97cc23bc67e8f0a291d",
    "qwen3-vl-4b": "1cd86afb9a95c410a6038ab3b40d8b578c892266",
    "qwen3-vl-8b": "f982a07559d4a2f6c8744d840bf6fccab30eea96",
    "chandra-ocr-2": "b9bbba6c5ee4eb14e75c114234b21cbb23911115",
    "qwen2.5-coder-1.5b": "f86cb2c1fa58255f8052cc32aeede1b7482d4361",
    "qwen2.5-coder-3b": "f74adce6aa16316c625447af059dbebe4983757c",
    "qwen2.5-coder-7b": "13fb94bfda8c8cf22497dc57b78f391a9acb426a",
    "qwen2.5-coder-14b": "d0a692ef765eefbf2fabb130b3cb2e8917e3d225",
}


def upgrade() -> None:
    with op.batch_alter_table("downloads") as batch:
        batch.add_column(sa.Column("revision", sa.String(40), nullable=True))
    downloads = sa.table(
        "downloads",
        sa.column("model_id", sa.String),
        sa.column("status", sa.String),
        sa.column("revision", sa.String),
    )
    # At most one row per model here, and only finished downloads nobody has stamped yet: a
    # half-fetched one gets its revision when it finishes.
    for model_id, pinned in PINS_AT_0003.items():
        op.execute(
            downloads.update()
            .where(
                downloads.c.model_id == model_id,
                downloads.c.status == "done",
                downloads.c.revision.is_(None),
            )
            .values(revision=pinned)
        )


def downgrade() -> None:
    # Plain ALTER TABLE: a batch rebuild of the table would have to carry its CHECK along.
    op.drop_column("downloads", "revision")
