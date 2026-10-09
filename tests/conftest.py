from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn

from lectern.config import Paths
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.hardware.sandbox import SandboxRuntime
from lectern.modules import EnvProbe
from lectern.projects.store import ProjectStore

NO_ENGINE = SandboxRuntime(runtime=None, version=None, engine_running=False, found=())


@pytest.fixture(autouse=True)
def no_host_container_engine(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app made in a test doesn't ask the host's Podman or Docker whether a sandbox can run.
    Its first turn builds the tool registry, which would: on a CI runner the first `podman info`
    takes seconds, and a test waiting a few seconds for that turn fails. Tests marked `sandbox`
    keep the real engine; tests of the probe itself pass their own `detect`."""
    if request.node.get_closest_marker("sandbox") is None:
        monkeypatch.setattr("lectern.app.EnvProbe", lambda: EnvProbe(detect=lambda: NO_ENGINE))


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    """A throwaway Lectern home, so no test touches the real app-data folder."""
    home = tmp_path / "lectern-home"
    home.mkdir()
    monkeypatch.setenv("LECTERN_HOME", str(home))
    return Paths(home=home)


@pytest.fixture
def free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server() -> Callable[..., contextlib.AbstractContextManager[str]]:
    """`with live_server(app, port) as base_url:` runs a real uvicorn server in a thread.
    TestClient can't read an open event stream: it waits for the whole response body."""

    @contextlib.contextmanager
    def run(app, port: int) -> Iterator[str]:  # noqa: ANN001
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(200):
            if server.started:
                break
            threading.Event().wait(0.02)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)

    return run


@pytest.fixture
def store(paths):
    """The project store on a throwaway app database."""
    engine = make_engine(paths.app_db)
    migrate(engine)
    projects = ProjectStore(paths, make_sessionmaker(engine))
    yield projects
    projects.dispose()
