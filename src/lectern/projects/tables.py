"""Tables in a project's own database, `<project>/.lectern/project.sqlite`.

The folder is the project: its files stay ordinary files, and this database holds what can't
live in them — chats and their transcripts, where each file came from, the search index and
the open tabs. Rows a person owns carry `owner_id` (the app database's user id), so login
can be added after the MVP without changing these tables.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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


SESSION_KINDS = ("chat", "ocr")


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


class ChatStateRow(ProjectBase):
    """A chat's state record: what's pinned (always sent to the model, word for word) and the
    summary of older turns (sent instead of them). The transcript itself never changes."""

    __tablename__ = "chat_state"

    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    pinned: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of pins
    summary: Mapped[str] = mapped_column(Text, default="")
    summary_upto: Mapped[int] = mapped_column(Integer, default=0)  # last message id it covers
    summary_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UiStateRow(ProjectBase):
    """Per-project UI state, such as the open tabs."""

    __tablename__ = "ui_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON


OCR_SESSION_STATUSES = ("queued", "running", "paused", "done", "failed")
OCR_PAGE_STATUSES = ("pending", "recognising", "done", "failed")
CLEAN_STATUSES = ("pending", "cleaning", "done", "failed")


class OcrSessionRow(ProjectBase):
    """An OCR session's state: its source file, its output folder and where its job stands."""

    __tablename__ = "ocr_sessions"
    __table_args__ = (CheckConstraint(one_of("status", OCR_SESSION_STATUSES), name="status"),)

    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    source_artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id"))
    folder: Mapped[str] = mapped_column(Text)  # relative to the project, "/" separated
    status: Mapped[str] = mapped_column(String(16), default="queued")
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pages_total: Mapped[int] = mapped_column(Integer, default=0)  # 0 until rasterised
    document_version: Mapped[int] = mapped_column(Integer, default=0)  # 0: none yet
    document_artifact_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifacts.id"), nullable=True
    )  # its document.md
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OcrPageRow(ProjectBase):
    """One page of an OCR session: its image and text files, and how it was last read."""

    __tablename__ = "ocr_pages"
    __table_args__ = (
        CheckConstraint(one_of("status", OCR_PAGE_STATUSES), name="status"),
        CheckConstraint(one_of("clean_status", CLEAN_STATUSES), name="clean_status"),
        UniqueConstraint("session_id", "number", name="uq_ocr_pages_session_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)  # from 1
    key: Mapped[str] = mapped_column(String(16))  # "p001": names the page's files
    status: Mapped[str] = mapped_column(String(16), default="pending")
    lease_key: Mapped[str | None] = mapped_column(String(128), nullable=True)  # chosen model
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # last read by
    handwritten: Mapped[bool] = mapped_column(default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    seconds: Mapped[float | None] = mapped_column(nullable=True)
    cut_off: Mapped[bool] = mapped_column(default=False)
    looped: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    has_hint: Mapped[bool] = mapped_column(default=False)  # the PDF had a text layer here
    reading: Mapped[int] = mapped_column(Integer, default=0)  # counts the page's readings
    clean_status: Mapped[str] = mapped_column(String(16), default="pending")
    clean_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleaned_reading: Mapped[int] = mapped_column(Integer, default=0)  # the reading it cleaned
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChunkLabelRow(ProjectBase):
    """A chunk's own reference, beside its number in the full-text index: a Document's block
    id ("b3.2"), so the chat cites #b3.2 where an ordinary file's chunk is #c3."""

    __tablename__ = "artifact_chunk_labels"

    artifact_id: Mapped[int] = mapped_column(ForeignKey("artifacts.id"), primary_key=True)
    chunk: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(32))


DECISION_ACTIONS = ("accept", "edit")


class OcrDecisionRow(ProjectBase):
    """The person's answer to one of a Document's doubts. Kept apart from document.json:
    the Document is assembled again from the page cleanups whenever a page is cleaned again,
    and each assembly applies these. A doubt is named by its page, its id and the text it was
    about, so a decision still finds it when cleaning the page again renumbers its doubts."""

    __tablename__ = "ocr_decisions"
    __table_args__ = (
        CheckConstraint(one_of("action", DECISION_ACTIONS), name="action"),
        UniqueConstraint("session_id", "page", "doubt_id", name="uq_ocr_decisions_doubt"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    page: Mapped[int] = mapped_column(Integer)
    doubt_id: Mapped[str] = mapped_column(String(32))  # "d3.1"
    raw: Mapped[str] = mapped_column(Text)  # what the page seemed to say
    value: Mapped[str] = mapped_column(Text)  # the reading the person chose
    action: Mapped[str] = mapped_column(String(16))  # accepted the suggestion, or typed it
    decided_by: Mapped[int] = mapped_column(Integer)  # the user's id
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
