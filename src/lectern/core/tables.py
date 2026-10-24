"""Tables in the app database (settings, users, the project registry, downloads).

Anything a person owns carries `owner_id`. An install starts with one user, `local`, and
nobody signs in; `lectern user init` gives it a sign-in name and a password, and from then on
there are accounts (see `lectern.core.accounts`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

LOCAL_USER_ID = 1
LOCAL_USER_NAME = "local"


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin', 'member')", name="role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # The folder the user's projects are under (users/<name>/projects): never changed.
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # What the person types to sign in, in lower case. None: no account yet (the local user
    # of an install where nobody signs in).
    login: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Wrong passwords in a row, and until when the next try must wait.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LoginRow(Base):
    """A browser signed in. The cookie holds an id; this holds its SHA-256, so the table
    gives nobody a way in. `token` is what that browser sends in a header with every request
    (the per-install token's place)."""

    __tablename__ = "logins"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SettingRow(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON


class ProjectRow(Base):
    __tablename__ = "projects"
    __table_args__ = (CheckConstraint("root_kind IN ('managed', 'linked')", name="root_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Set once, never changed; a new managed project's folder is named after it.
    uuid: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid4())
    )
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    root_kind: Mapped[str] = mapped_column(String(16), default="managed")
    root_path: Mapped[str] = mapped_column(Text)  # absolute path
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Set when its owner removes it: from then on it's in the trash (root_path says where).
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
