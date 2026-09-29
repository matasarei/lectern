"""Tables in a project's own database, `<project>/.lectern/project.sqlite`.

The folder is the project: its files stay ordinary files, and this database holds what can't
live in them — chats and their transcripts, where each file came from, the search index and
the open tabs. Rows a person owns carry `owner_id` (the app database's user id), so login
can be added after the MVP without changing these tables.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    """A stored time as ISO 8601 in UTC. SQLite hands times back without their zone, and a
    browser would read those as local time."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def one_of(column: str, values: tuple[str, ...]) -> str:
    """A CHECK expression; also right for a single value, where a tuple's repr isn't SQL."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ProjectBase(DeclarativeBase):
    pass


SESSION_KINDS = ("chat",)  # modules add theirs (OCR in M2)


class SessionRow(ProjectBase):
    __tablename__ = "sessions"
    __table_args__ = (CheckConstraint(one_of("kind", SESSION_KINDS), name="kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    titled: Mapped[bool] = mapped_column(default=False)  # named by a person, or from a message
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


MESSAGE_ROLES = ("user", "assistant", "tool")


class MessageRow(ProjectBase):
    """The transcript, word for word. Compaction (M1b) never changes these rows."""

    __tablename__ = "messages"
    __table_args__ = (CheckConstraint(one_of("role", MESSAGE_ROLES), name="role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON, assistant only
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # tool only
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)  # tool only
    attachments: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON artifact ids
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


CONFIRMATION_STATUSES = ("pending", "approved", "declined")


class ConfirmationRow(ProjectBase):
    """A tool call waiting for the person's Allow / Don't allow."""

    __tablename__ = "confirmations"
    __table_args__ = (CheckConstraint(one_of("status", CONFIRMATION_STATUSES), name="status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(128))
    tool: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[str] = mapped_column(Text)  # JSON
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


ARTIFACT_KINDS = ("imported", "generated")
FROM_KINDS = ("import", "session", "artifact")
TEXT_STATUSES = ("none", "indexed", "failed")


class ArtifactRow(ProjectBase):
    """A file in the project, and where it came from (the From column)."""

    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(one_of("kind", ARTIFACT_KINDS), name="kind"),
        CheckConstraint(one_of("from_kind", FROM_KINDS), name="from_kind"),
        CheckConstraint(one_of("text_status", TEXT_STATUSES), name="text_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    path: Mapped[str] = mapped_column(Text)  # relative to the project folder, "/" separated
    kind: Mapped[str] = mapped_column(String(16))
    mime: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    text_status: Mapped[str] = mapped_column(String(16), default="none")
    text_note: Mapped[str | None] = mapped_column(Text, nullable=True)  # e.g. "no text layer"
    from_kind: Mapped[str] = mapped_column(String(16))
    from_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trash_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScriptRunRow(ProjectBase):
    """One run of a script in the sandbox. A run with the same script and inputs (by
    sha256) is returned instead of running again."""

    __tablename__ = "script_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    script_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id"), index=True)
    script_sha: Mapped[str] = mapped_column(String(64))
    inputs_sha: Mapped[str] = mapped_column(String(64))  # of the inputs' names and sha256s
    test_run: Mapped[bool] = mapped_column(default=False)  # made by code.write
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(default=False)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    outputs: Mapped[str] = mapped_column(Text, default="[]")  # JSON artifact ids
    seconds: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UiStateRow(ProjectBase):
    """Per-project UI state, such as the open tabs."""

    __tablename__ = "ui_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON
