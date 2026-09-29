"""Files in a project (artifacts): importing, writing new ones, and the trash.

Files live at the top of the project folder as ordinary files. A file is never overwritten:
a name that's taken gets " (2)", " (3)" before its extension. Deleting moves it to
`.lectern/trash/` for 30 days.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from lectern.projects import search
from lectern.projects.tables import ArtifactRow, now
from lectern.projects.text import extract, mime_for

IMPORT_LIMIT = 2 * 1024**3
NAME_MAX = 180
TRASH_DAYS = 30
DATA_DIR = ".lectern"
CLAIM_ATTEMPTS = 50


class BadName(ValueError):
    pass


class TooLarge(ValueError):
    pass


@dataclass(frozen=True)
class Origin:
    """Where a file came from: the From column."""

    kind: str  # "import" | "session" | "artifact"
    id: int | None = None
    note: str | None = None


def safe_name(name: str) -> str:
    """A plain file name from whatever the client sent: only its last path part, no control
    characters. Hidden names (".env") and the project's own `.lectern` are refused."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C").strip()
    name = name.rstrip(" .")
    if not name or name in {".", ".."}:
        raise BadName("The file needs a name.")
    if name.startswith("."):
        raise BadName(f"Hidden files such as {name!r} can't be imported.")
    if any(c in name for c in '<>:"|?*'):
        raise BadName(f"{name!r} has characters Windows can't use in a file name.")
    stem, suffix = os.path.splitext(name)
    if len(name) > NAME_MAX:
        name = stem[: NAME_MAX - len(suffix)] + suffix
    return name


def free_name(root: Path, name: str) -> str:
    stem, suffix = os.path.splitext(name)
    candidate, n = name, 2
    # Case-insensitive, so it's the same answer on macOS and Windows disks.
    taken = {p.name.lower() for p in root.iterdir()}
    while candidate.lower() in taken:
        candidate, n = f"{stem} ({n}){suffix}", n + 1
    return candidate


def inside(root: Path, relative: str) -> Path:
    """The absolute path of a stored relative path, refusing anything outside the project."""
    path = (root / relative).resolve()
    if (
        not path.is_relative_to(root.resolve())
        or DATA_DIR in path.relative_to(root.resolve()).parts
    ):
        raise BadName(f"{relative!r} is outside the project.")
    return path


async def import_stream(
    root: Path,
    sessions: sessionmaker[Session],
    *,
    owner_id: int,
    name: str,
    body: AsyncIterator[bytes],
    origin: Origin,
) -> ArtifactRow:
    """Save an upload as a new file in the project and index its text."""
    name = safe_name(name)
    part = root / DATA_DIR / f"upload-{os.getpid()}-{id(body)}.part"
    part.parent.mkdir(exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with part.open("wb") as out:
            async for chunk in body:
                size += len(chunk)
                if size > IMPORT_LIMIT:
                    raise TooLarge(f"Files up to {IMPORT_LIMIT // 1024**3} GB can be imported.")
                digest.update(chunk)
                await run_in_threadpool(out.write, chunk)
        return await run_in_threadpool(
            _place_in_new_session,
            root,
            sessions,
            part,
            name,
            owner_id,
            size,
            digest.hexdigest(),
            origin,
        )
    finally:
        part.unlink(missing_ok=True)


def write_text(
    root: Path, session: Session, *, owner_id: int, name: str, text: str, origin: Origin
) -> ArtifactRow:
    """A new text file written by a model. Never replaces an existing file."""
    name = safe_name(name)
    part = root / DATA_DIR / f"write-{os.getpid()}-{id(text)}.part"
    part.parent.mkdir(exist_ok=True)
    data = text.encode("utf-8")
    part.write_bytes(data)
    try:
        return _place(
            root, session, part, name, owner_id, len(data), hashlib.sha256(data).hexdigest(), origin
        )
    finally:
        part.unlink(missing_ok=True)


def _place_in_new_session(
    root: Path,
    sessions: sessionmaker[Session],
    part: Path,
    name: str,
    owner_id: int,
    size: int,
    sha256: str,
    origin: Origin,
) -> ArtifactRow:
    # All of an import's database work in one worker thread: a SQLite connection belongs to
    # the thread that opened it.
    with sessions.begin() as session:
        return _place(root, session, part, name, owner_id, size, sha256, origin)


def _place(
    root: Path,
    session: Session,
    part: Path,
    name: str,
    owner_id: int,
    size: int,
    sha256: str,
    origin: Origin,
) -> ArtifactRow:
    final = _claim(root, name, part)
    extracted = extract(final)
    row = ArtifactRow(
        owner_id=owner_id,
        path=final.name,
        kind="imported" if origin.kind == "import" else "generated",
        mime=mime_for(final.name),
        size=size,
        sha256=sha256,
        text_status="indexed" if extracted.has_text else "none",
        text_note=extracted.note,
        from_kind=origin.kind,
        from_id=origin.id,
        from_note=origin.note,
    )
    session.add(row)
    session.flush()
    search.index(session, row.id, extracted.chunks)
    return row


def _claim(root: Path, name: str, part: Path) -> Path:
    """Put `part` in the project under a free name, never replacing a file. The name is
    taken with an operation that fails if it exists (a hard link, or an exclusive create
    where the disk has no hard links), so two writes at once can't end up on one name."""
    for _ in range(CLAIM_ATTEMPTS):
        final = root / free_name(root, name)
        try:
            os.link(part, final)
        except FileExistsError:
            continue  # taken since free_name looked: try the next number
        except OSError:
            try:
                with part.open("rb") as source, final.open("xb") as target:
                    shutil.copyfileobj(source, target)
            except FileExistsError:
                continue
        return final
    raise BadName(f"Couldn't find a free name for {name!r}.")


def live(session: Session) -> list[ArtifactRow]:
    return list(
        session.scalars(
            select(ArtifactRow).where(ArtifactRow.trashed_at.is_(None)).order_by(ArtifactRow.id)
        )
    )


def trash(root: Path, session: Session, artifact_id: int) -> ArtifactRow:
    """Move a file to .lectern/trash/ and drop it from search. The row stays, so it can be
    restored until the trash is emptied."""
    row = session.get(ArtifactRow, artifact_id)
    if row is None or row.trashed_at is not None:
        raise LookupError(artifact_id)
    source = inside(root, row.path)
    bin_dir = root / DATA_DIR / "trash"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / f"{row.id}-{source.name}"
    if source.exists():
        shutil.move(source, target)
    row.trashed_at = now()
    row.trash_path = target.relative_to(root).as_posix()
    search.remove(session, row.id)
    return row


def purge_trash(root: Path, session: Session, *, days: int = TRASH_DAYS) -> int:
    """Delete files that have been in the trash longer than `days`. Returns how many."""
    cutoff = now() - timedelta(days=days)
    purged = 0
    rows = session.scalars(select(ArtifactRow).where(ArtifactRow.trashed_at.is_not(None)))
    for row in rows:
        trashed: datetime = row.trashed_at  # type: ignore[assignment]
        if trashed.tzinfo is None:  # SQLite hands back naive datetimes
            trashed = trashed.replace(tzinfo=cutoff.tzinfo)
        if trashed >= cutoff:
            continue
        if row.trash_path:
            (root / row.trash_path).unlink(missing_ok=True)
        session.delete(row)
        purged += 1
    return purged
