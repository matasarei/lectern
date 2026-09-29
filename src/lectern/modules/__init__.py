"""Modules: features that plug their agent tools (and later session types, jobs and
viewers) into the core. A module is found through the `lectern.modules` entry point, so a
module built outside this repository installs the same way the built-in ones do.

The agent's tool list is rebuilt for every turn from the core tools and each module's
tools for what's available right now (`ToolEnv`), so the sandbox's tools appear when Docker
or Podman starts and go away when it stops.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Protocol, runtime_checkable

from lectern.agent.tools import AgentTool, Registry
from lectern.hardware import sandbox

ENTRY_POINT_GROUP = "lectern.modules"
ENV_TTL_SECONDS = 30.0  # asking podman/docker can take seconds; a turn shouldn't wait each time

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolEnv:
    """What the machine offers right now, for deciding which tools a turn gets."""

    sandbox: bool  # a container engine is running


@runtime_checkable
class Module(Protocol):
    id: str

    def tools(self, env: ToolEnv) -> list[AgentTool]: ...


def load_modules() -> list[Module]:
    """The installed modules, by entry point. One that fails to load is logged and skipped,
    so a broken module can't stop the app."""
    found: dict[str, Module] = {}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            module = entry.load()
        except Exception:  # noqa: BLE001 — any import error in someone's module
            log.exception("Couldn't load the module %r", entry.name)
            continue
        if not isinstance(module, Module):
            log.error("%r isn't a Lectern module (it needs `id` and `tools(env)`).", entry.name)
            continue
        found.setdefault(module.id, module)
    return list(found.values())


class EnvProbe:
    """ToolEnv, re-checked at most every ENV_TTL_SECONDS. Called in a worker thread."""

    def __init__(self, detect: Callable[[], sandbox.SandboxRuntime] = sandbox.detect) -> None:
        self._detect = detect
        self._lock = threading.Lock()
        self._cached: tuple[float, ToolEnv] | None = None

    def __call__(self) -> ToolEnv:
        with self._lock:
            now = time.monotonic()
            if self._cached is None or now - self._cached[0] >= ENV_TTL_SECONDS:
                self._cached = (now, ToolEnv(sandbox=self._detect().engine_running))
            return self._cached[1]


def tool_registry(
    core: list[AgentTool], modules: list[Module], env: Callable[[], ToolEnv]
) -> Callable[[], Registry]:
    """A function giving the registry for one turn: the core tools, then each module's."""

    def build() -> Registry:
        current = env()
        tools = list(core)
        for module in modules:
            tools.extend(module.tools(current))
        return Registry(tools)

    return build
