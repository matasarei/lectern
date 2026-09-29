from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time

import httpx2
import pytest

from lectern.cli import SHUTDOWN_GRACE_SECONDS, server_config


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_config_gives_up_on_stuck_connections(paths):
    from lectern.app import create_app

    config = server_config(create_app(paths), 18765)
    assert config.host == "127.0.0.1"
    assert config.timeout_graceful_shutdown == SHUTDOWN_GRACE_SECONDS


@pytest.mark.skipif(sys.platform == "win32", reason="sends SIGINT to a child process")
def test_ctrl_c_exits_while_the_page_holds_event_streams(tmp_path):
    """The browser tab `lectern serve` opens keeps /api/runtime/events and /api/models/events
    open. Ctrl+C must still end the process, promptly and without a traceback."""
    home = tmp_path / "home"
    port = _free_port()
    env = {**os.environ, "LECTERN_HOME": str(home)}
    log = tmp_path / "serve.log"
    with log.open("wb") as out:
        process = subprocess.Popen(  # noqa: S603 (fixed argv)
            [sys.executable, "-c", "from lectern.cli import app; app()", "serve",
             "--no-browser", "--port", str(port)],
            stdout=out, stderr=subprocess.STDOUT, env=env,
        )  # fmt: skip
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx2.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx2.HTTPError:
                time.sleep(0.1)
        token = (home / "token").read_text()
        connected = threading.Barrier(3)

        def hold(path: str) -> None:
            with (
                httpx2.Client(base_url=base, headers={"X-Lectern-Token": token}, timeout=30) as c,
                c.stream("GET", path) as stream,
            ):
                lines = stream.iter_lines()
                next(lines)  # ": connected"
                connected.wait()
                for _ in lines:  # until the server ends the stream
                    pass

        holders = [
            threading.Thread(target=hold, args=(p,), daemon=True)
            for p in ("/api/runtime/events", "/api/models/events")
        ]
        for h in holders:
            h.start()
        connected.wait(timeout=10)

        started = time.monotonic()
        process.send_signal(signal.SIGINT)
        process.wait(timeout=SHUTDOWN_GRACE_SECONDS + 5)
        took = time.monotonic() - started
    finally:
        if process.poll() is None:
            process.kill()

    output = log.read_text()
    assert took < SHUTDOWN_GRACE_SECONDS, f"exited after {took:.1f} s, not by itself"
    assert "Application shutdown complete" in output
    assert "Traceback" not in output
    assert "timeout graceful shutdown exceeded" not in output


def test_handle_exit_hands_the_flag_to_the_running_loop(paths):
    import asyncio

    from lectern.app import create_app
    from lectern.cli import LecternServer

    app = create_app(paths)
    server = LecternServer(server_config(app, 18765))

    async def go():
        server.handle_exit(signal.SIGINT, None)
        # Scheduled on the loop, not set inside the signal handler itself.
        assert not app.state.shutting_down.is_set()
        await asyncio.wait_for(app.state.shutting_down.wait(), timeout=1)

    asyncio.run(go())
    assert server.should_exit


def test_handle_exit_without_a_running_loop_sets_the_flag_directly(paths):
    from lectern.app import create_app
    from lectern.cli import LecternServer

    app = create_app(paths)
    LecternServer(server_config(app, 18765)).handle_exit(signal.SIGINT, None)
    assert app.state.shutting_down.is_set()
