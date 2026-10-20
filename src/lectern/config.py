"""Where Lectern keeps its files.

Everything lives under one home directory: the OS app-data folder by default, or
``LECTERN_HOME`` when set (tests and development use that to stay out of the real one).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

APP_NAME = "Lectern"
DEFAULT_PORT = 8765


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def token_file(self) -> Path:
        return self.home / "token"

    @property
    def web_search_key_file(self) -> Path:
        """An admin's Brave Search API key, readable by the owner only (the web module)."""
        return self.home / "brave-search.key"

    @property
    def app_db(self) -> Path:
        return self.home / "lectern.sqlite"

    @property
    def models(self) -> Path:
        return self.home / "models"

    @property
    def runners(self) -> Path:
        return self.home / "runners"

    @property
    def users(self) -> Path:
        return self.home / "users"

    def projects_root(self, user_name: str) -> Path:
        return self.users / user_name / "projects"

    def projects_trash(self, user_name: str) -> Path:
        """Removed projects, for 30 days (lectern.projects.store)."""
        return self.users / user_name / "trash"


def lectern_home() -> Path:
    override = os.environ.get("LECTERN_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return user_data_path(APP_NAME, appauthor=False)


def get_paths() -> Paths:
    return Paths(home=lectern_home())
