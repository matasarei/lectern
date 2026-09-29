from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn

from lectern.config import Paths


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
