"""Projects: the registry in the app database, and each project's own folder and database.

A managed project is a folder under `<LECTERN_HOME>/users/<user>/projects/`. Its database,
`.lectern/project.sqlite`, is opened (and migrated) the first time it's used, then kept open
until the app stops.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from lectern.config import Paths
from lectern.core.auth import User
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.core.tables import ProjectRow, UserRow
from lectern.projects import files
from lectern.projects.tables import SessionRow, now

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
    def __init__(self, paths: Paths, app_sessions: sessionmaker[Session]) -> None:
        self.paths = paths
        self.app_sessions = app_sessions
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
                select(ProjectRow).where(ProjectRow.owner_id == user.id).order_by(ProjectRow.id)
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
            row = session.get(ProjectRow, project_id)
            if row is None or row.owner_id != user.id:
                raise ProjectNotFound(project_id)
            return Project.from_row(row)

    def rename(self, user: User, project_id: int, name: str) -> Project:
        """Changes the name shown; the folder keeps its name, so nothing on disk moves."""
        with self.app_sessions.begin() as session:
            row = session.get(ProjectRow, project_id)
            if row is None or row.owner_id != user.id:
                raise ProjectNotFound(project_id)
            row.name = clean_name(name)
            session.flush()
            return Project.from_row(row)

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

    def dispose(self) -> None:
        with self._lock:
            for engine, _ in self._dbs.values():
                engine.dispose()
            self._dbs.clear()


def touch(session: Session, session_id: int) -> None:
    """Mark a session as changed now (Overview sorts by it)."""
    row = session.get(SessionRow, session_id)
    if row is not None:
        row.updated_at = now()
