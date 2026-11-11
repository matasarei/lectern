"""Secrets Lectern keeps in files of their own, such as the Brave Search API key: read, and
written readable by the owner only, as the access token is (`core/security.py`)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def read_secret(path: Path) -> str | None:
    """A secret kept in a file of its own (an API key), or None when there is none."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def write_secret(path: Path, value: str) -> None:
    """Keep a secret in a file readable by the owner only, as the token is. Written to a new
    file and moved over the old one, so a reader never sees half a key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    if sys.platform != "win32":
        part.chmod(0o600)
    os.replace(part, path)
