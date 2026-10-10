"""Tables in the app database (settings, users, the project registry, downloads).

Anything a person owns carries `owner_id`, so login can be added after the MVP without
changing these tables. In the MVP there is exactly one user, `local`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

LOCAL_USER_ID = 1
LOCAL_USER_NAME = "local"


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SettingRow(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON


class ProjectRow(Base):
    __tablename__ = "projects"
    __table_args__ = (CheckConstraint("root_kind IN ('managed', 'linked')", name="root_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    root_kind: Mapped[str] = mapped_column(String(16), default="managed")
    root_path: Mapped[str] = mapped_column(Text)  # absolute path
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


DOWNLOAD_STATUSES = ("queued", "downloading", "verifying", "done", "failed", "paused")


class DownloadRow(Base):
    __tablename__ = "downloads"
    __table_args__ = (CheckConstraint(f"status IN {DOWNLOAD_STATUSES!r}", name="status"),)

    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    bytes_done: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_total: Mapped[int] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The catalog revision a finished download fetched: the model's version on disk. None
    # until it finishes, and for files that were put there by hand.
    revision: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
