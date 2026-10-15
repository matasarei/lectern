"""Projects: the registry in the app database, and each project's own folder and database.

A managed project is a folder under `<LECTERN_HOME>/users/<user>/projects/`. Its database,
`.lectern/project.sqlite`, is opened (and migrated) the first time it's used, then kept open
until the app stops.

A project its owner removes is moved, folder and all, to `<LECTERN_HOME>/users/<user>/trash/`
for 30 days (as a file is, inside a project), then deleted with its row. Until then nothing
shows or opens it.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from lectern.config import Paths
from lectern.core.auth import User
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.core.tables import ProjectRow, UserRow
from lectern.projects import files
from lectern.projects.tables import SessionRow, now

log = logging.getLogger(__name__)

UNTITLED = "Untitled project"
NEW_CHAT = "New chat"
DATA_DIR = ".lectern"
NAME_MAX = 200
FOLDER_NAME_MAX = 80
# Names Windows refuses for a file or folder, whatever the extension.
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)
}


class ProjectNotFound(LookupError):
    """No such project for this user. Someone else's project is reported the same way."""


class NameMismatch(ValueError):
    """Removing a project needs its name typed exactly as it is."""


@dataclass(frozen=True)
class Project:
    id: int
    owner_id: int
    name: str
    root: Path
    created_at: object

    @property
    def data_dir(self) -> Path:
        return self.root / DATA_DIR

    @classmethod
    def from_row(cls, row: ProjectRow) -> Project:
        return cls(row.id, row.owner_id, row.name, Path(row.root_path), row.created_at)


def clean_name(name: str) -> str:
    """A display name: one line, no control characters, not empty."""
    name = " ".join("".join(" " if unicodedata.category(c)[0] == "C" else c for c in name).split())
    return name[:NAME_MAX] or UNTITLED


def folder_name(name: str) -> str:
    """A folder name that works on Windows, macOS and Linux: no path separators or characters
    Windows refuses, no trailing dots or spaces, not a reserved device name."""
    safe = re.sub(r'[<>:"/\\|?*]', "-", clean_name(name))
    safe = safe[:FOLDER_NAME_MAX].rstrip(" .") or UNTITLED
    if safe.startswith("."):
        safe = "_" + safe.lstrip(".")
    if safe.split(".")[0].upper() in WINDOWS_RESERVED:
        safe = f"{safe}_"
    return safe


class ProjectStore:
    def __init__(
        self,
        paths: Paths,
        app_sessions: sessionmaker[Session],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.paths = paths
        self.app_sessions = app_sessions
        self.clock = clock  # when a project was removed, and when its 30 days are up
        self._dbs: dict[int, tuple[Engine, sessionmaker[Session]]] = {}
        self._lock = threading.Lock()

    # --- the registry ------------------------------------------------------------------

    def create(self, user: User, name: str = UNTITLED) -> Project:
        """A new managed project: its folder, its database, and one empty chat."""
        name = clean_name(name)
        parent = self.paths.projects_root(user.name)
        parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            root = self._free_folder(parent, folder_name(name))
            root.mkdir()
        with self.app_sessions.begin() as session:
            row = ProjectRow(owner_id=user.id, name=name, root_kind="managed", root_path=str(root))
            session.add(row)
            session.flush()
            project = Project.from_row(row)
        with self.db(project).begin() as session:
            session.add(SessionRow(owner_id=user.id, kind="chat", title=NEW_CHAT))
        return project

    def list(self, user: User) -> list[Project]:
        with self.app_sessions() as session:
            rows = session.scalars(
                select(ProjectRow)
                .where(ProjectRow.owner_id == user.id, ProjectRow.removed_at.is_(None))
                .order_by(ProjectRow.id)
            )
            return [Project.from_row(r) for r in rows]

    def user(self, user_id: int) -> User:
        """The user with that id, for work done in their name when they aren't asking (a
        chat's follow-up)."""
        with self.app_sessions() as session:
            row = session.get(UserRow, user_id)
            return User(id=row.id, name=row.name, login=row.login, role=row.role)

    def get(self, user: User, project_id: int) -> Project:
        with self.app_sessions() as session:
            return Project.from_row(_own_row(session, user, project_id))

    def rename(self, user: User, project_id: int, name: str) -> Project:
        """Changes the name shown; the folder keeps its name, so nothing on disk moves."""
        with self.app_sessions.begin() as session:
            row = _own_row(session, user, project_id)
            row.name = clean_name(name)
            session.flush()
            return Project.from_row(row)

    # --- removing a project ------------------------------------------------------------

    def remove(self, user: User, project_id: int, typed_name: str) -> Project:
        """Mark the project removed, from now on not found, when `typed_name` is its name
        exactly (case and spaces too). Its folder stays where it is until
        `move_to_trash`: whatever still runs in it is stopped in between."""
        with self.app_sessions.begin() as session:
            row = _own_row(session, user, project_id)
            if typed_name != row.name:
                raise NameMismatch(project_id)
            row.removed_at = self.clock()
            return Project.from_row(row)

    def move_to_trash(self, project: Project) -> None:
        """A removed project's folder into its owner's trash, its database closed first. A
        folder that can't be moved (a file still open in it, on Windows) stays where it is:
        the project is removed either way, and the purge deletes it from there."""
        self._forget_db(project.id)
        with self.app_sessions.begin() as session:
            row = session.get(ProjectRow, project.id)
            owner = session.get(UserRow, row.owner_id)
            trash = self.paths.projects_trash(owner.name)
            stamp = row.removed_at.strftime("%Y%m%d-%H%M%S")
            with self._lock:
                target = self._free_folder(trash, f"{project.root.name} {stamp}")
                try:
                    trash.mkdir(parents=True, exist_ok=True)
                    project.root.rename(target)
                except OSError as exc:
                    log.warning("project %s: its folder stays in place (%s)", project.id, exc)
                    return
            row.root_path = str(target)

    def purge_removed(self, *, days: int = files.TRASH_DAYS) -> int:
        """Delete the projects removed more than `days` ago, folder and row. One whose folder
        can't be deleted keeps its row, and is tried again next time. Returns how many went."""
        cutoff = self.clock() - timedelta(days=days)
        purged = 0
        with self.app_sessions() as session:
            rows = session.scalars(select(ProjectRow).where(ProjectRow.removed_at.is_not(None)))
            due = [(r.id, Path(r.root_path)) for r in rows if _aware(r.removed_at) < cutoff]
        for project_id, root in due:
            try:
                shutil.rmtree(root)
            except FileNotFoundError:
                pass  # deleted by hand already
            except OSError as exc:
                log.warning("project %s: its folder couldn't be deleted yet (%s)", project_id, exc)
                continue
            with self.app_sessions.begin() as session:
                session.delete(session.get(ProjectRow, project_id))
            purged += 1
        return purged

    @staticmethod
    def _free_folder(parent: Path, base: str) -> Path:
        candidate, n = parent / base, 2
        while candidate.exists():
            candidate, n = parent / f"{base} {n}", n + 1
        return candidate

    # --- each project's database -------------------------------------------------------

    def db(self, project: Project) -> sessionmaker[Session]:
        with self._lock:
            opened = self._dbs.get(project.id)
            if opened is None:
                engine = make_engine(project.data_dir / "project.sqlite")
                migrate(engine, "lectern:project_migrations")
                opened = (engine, make_sessionmaker(engine))
                with opened[1].begin() as session:
                    files.purge_trash(project.root, session)  # older than 30 days
                self._dbs[project.id] = opened
            return opened[1]

    def _forget_db(self, project_id: int) -> None:
        with self._lock:
            opened = self._dbs.pop(project_id, None)
        if opened is not None:
            opened[0].dispose()

    def dispose(self) -> None:
        with self._lock:
            for engine, _ in self._dbs.values():
                engine.dispose()
            self._dbs.clear()


def _own_row(session: Session, user: User, project_id: int) -> ProjectRow:
    """The user's project, unless it's someone else's, removed, or not there at all: those
    are all "not found"."""
    row = session.get(ProjectRow, project_id)
    if row is None or row.owner_id != user.id or row.removed_at is not None:
        raise ProjectNotFound(project_id)
    return row


def _aware(moment: datetime) -> datetime:
    """SQLite hands DateTime(timezone=True) values back without their zone: they're UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def touch(session: Session, session_id: int) -> None:
    """Mark a session as changed now (Overview sorts by it)."""
    row = session.get(SessionRow, session_id)
    if row is not None:
        row.updated_at = now()
