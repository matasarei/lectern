"""Modules: features that plug their agent tools (and later session types, jobs and
viewers) into the core. A module is found through the `lectern.modules` entry point, so a
module built outside this repository installs the same way the built-in ones do.

The agent's tool list is rebuilt for every turn from the core tools and each module's
tools for what's available right now (`ToolEnv`), so the sandbox's tools appear when Docker
or Podman starts and go away when it stops.

A module may also bring API routes: a `router` attribute (a FastAPI APIRouter), mounted
under /api behind the same user check as every other route. An export module brings
exporters: an `exporters()` method (see lectern.documents.export).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Protocol, runtime_checkable

from fastapi import APIRouter

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


def module_routers(modules: list[Module]) -> list[APIRouter]:
    """The routers the modules bring, if any (`router` is optional)."""
    return [r for m in modules if (r := getattr(m, "router", None)) is not None]


def module_exporters(modules: list[Module]) -> list:
    """The exporters the modules offer (`exporters()` is optional), in module order."""
    return [e for m in modules if (offer := getattr(m, "exporters", None)) for e in offer()]


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
    """ToolEnv for each turn, without the turn waiting on Podman or Docker (an answer can take
    seconds, and the model isn't asked until the tools are known). The first check starts with
    the app (`start`); after it, a turn gets the last answer at once, and an answer older than
    ENV_TTL_SECONDS is checked again in the background for the turns after it. Only a turn
    that comes before the first answer waits for it. Called in a worker thread."""

    def __init__(self, detect: Callable[[], sandbox.SandboxRuntime] = sandbox.detect) -> None:
        self._detect = detect
        self._lock = threading.Lock()
        self._cached: tuple[float, ToolEnv] | None = None
        self._checking: threading.Thread | None = None
        self._first = threading.Event()

    def start(self) -> None:
        """Check now, in the background, so the first turn rarely has to wait."""
        with self._lock:
            self._check_in_background()

    def settle(self, timeout: float | None = None) -> None:
        """Wait for a check in progress to end (for tests, and a clean shutdown)."""
        checking = self._checking
        if checking is not None:
            checking.join(timeout)

    def __call__(self) -> ToolEnv:
        with self._lock:
            cached = self._cached
            if cached is None or time.monotonic() - cached[0] >= ENV_TTL_SECONDS:
                self._check_in_background()
        if cached is not None:
            return cached[1]
        self._first.wait()
        with self._lock:
            return self._cached[1]  # type: ignore[index]  # set before _first

    def _check_in_background(self) -> None:
        """Start a check unless one is running. Call with the lock held."""
        if self._checking is not None and self._checking.is_alive():
            return
        self._checking = threading.Thread(target=self._check, name="env-probe", daemon=True)
        self._checking.start()

    def _check(self) -> None:
        try:
            env = ToolEnv(sandbox=self._detect().engine_running)
        except Exception:  # noqa: BLE001 — a turn waiting on the first answer mustn't hang
            log.exception("Couldn't tell whether a container engine is running")
            env = ToolEnv(sandbox=False)
        with self._lock:
            self._cached = (time.monotonic(), env)
        self._first.set()


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
