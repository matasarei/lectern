"""Is there a container runtime for the sandbox? Podman is preferred (rootless, and no
Docker Desktop subscription); Docker works too."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass

# What each CLI prints when its engine (daemon or VM) is answering.
VERSION_COMMANDS = {
    "podman": ["info", "--format", "{{.Version.Version}}"],
    "docker": ["version", "--format", "{{.Server.Version}}"],
}


@dataclass(frozen=True)
class SandboxRuntime:
    runtime: str | None  # "podman" | "docker" | None
    version: str | None
    engine_running: bool
    found: tuple[str, ...]  # CLIs present on PATH, running or not

    def to_dict(self) -> dict:
        return asdict(self)


def _engine_version(binary: str, args: list[str]) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 (fixed argv, no shell)
            [binary, *args], capture_output=True, text=True, timeout=8, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def detect(
    which: Callable[[str], str | None] = shutil.which,
    engine_version: Callable[[str, list[str]], str | None] = _engine_version,
) -> SandboxRuntime:
    found = tuple(name for name in VERSION_COMMANDS if which(name))
    for name in found:
        version = engine_version(which(name) or name, VERSION_COMMANDS[name])
        if version:
            return SandboxRuntime(runtime=name, version=version, engine_running=True, found=found)
    return SandboxRuntime(
        runtime=found[0] if found else None, version=None, engine_running=False, found=found
    )
