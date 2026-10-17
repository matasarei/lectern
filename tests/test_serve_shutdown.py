from __future__ import annotations

import base64
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


def _exactly(conn: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        piece = conn.recv(count - len(data))
        if not piece:
            raise ConnectionError("the server closed the connection")
        data += piece
    return data


def _hold_socket(port: int, token: str, path: str, connected: threading.Barrier) -> None:
    """Open a WebSocket as a browser does and keep it until the server closes it, answering
    its close as a browser does. Written out by hand: no WebSocket client is a dependency."""
    key = base64.b64encode(os.urandom(16)).decode()
    with socket.create_connection(("127.0.0.1", port), timeout=30) as conn:
        conn.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Origin: http://127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Protocol: lectern.v1, lectern.token.{token}\r\n\r\n"
            ).encode()
        )
        answer = b""
        while b"\r\n\r\n" not in answer:
            answer += _exactly(conn, 1)
        assert answer.startswith(b"HTTP/1.1 101"), answer
        connected.wait()
        while True:  # frames from the server (never masked), until its close
            first, second = _exactly(conn, 2)
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(_exactly(conn, 2), "big")
            elif length == 127:
                length = int.from_bytes(_exactly(conn, 8), "big")
            _exactly(conn, length)
            if first & 0x0F == 0x8:
                conn.sendall(bytes([0x88, 0x80, 0, 0, 0, 0]))  # close, masked, no payload
                return


def _hold_stream(port: int, token: str, path: str, connected: threading.Barrier) -> None:
    base = f"http://127.0.0.1:{port}"
    with (
        httpx2.Client(base_url=base, headers={"X-Lectern-Token": token}, timeout=30) as c,
        c.stream("GET", path) as stream,
    ):
        lines = stream.iter_lines()
        next(lines)  # ": connected"
        connected.wait()
        for _ in lines:  # until the server ends the stream
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="sends SIGINT to a child process")
@pytest.mark.parametrize("hold", [_hold_socket, _hold_stream], ids=["sockets", "http streams"])
def test_ctrl_c_exits_while_the_page_holds_event_streams(tmp_path, hold):
    """The browser tab `lectern serve` opens keeps /api/runtime/events and /api/models/events
    open: as sockets, which is what the page follows, or as HTTP streams. Ctrl+C must still
    end the process, promptly and without a traceback."""
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
        holders = [
            threading.Thread(target=hold, args=(port, token, p, connected), daemon=True)
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


def _serving(server):  # noqa: ANN001, ANN202
    """The server running in a thread of its own, as `lectern serve` runs it."""
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "the server didn't start"
    return thread


def test_closing_standard_input_stops_the_server_as_ctrl_c_does(paths):
    from lectern.app import create_app
    from lectern.cli import LecternServer

    app = create_app(paths)
    read, write = os.pipe()
    with os.fdopen(read, "rb") as stdin:
        server = LecternServer(
            server_config(app, _free_port()), stop_on_stdin_close=True, stdin=stdin
        )
        thread = _serving(server)
        os.write(write, b"anything written is ignored\n")
        os.close(write)
        thread.join(timeout=SHUTDOWN_GRACE_SECONDS + 5)
    assert not thread.is_alive()
    assert server.should_exit and app.state.shutting_down.is_set()


def test_standard_input_is_left_alone_unless_asked(paths):
    from lectern.app import create_app
    from lectern.cli import LecternServer

    app = create_app(paths)
    read, write = os.pipe()
    with os.fdopen(read, "rb") as stdin:
        server = LecternServer(server_config(app, _free_port()), stdin=stdin)
        thread = _serving(server)
        os.close(write)
        thread.join(timeout=1)
        assert thread.is_alive() and not server.should_exit  # still serving
        server.handle_exit(signal.SIGINT, None)
        thread.join(timeout=SHUTDOWN_GRACE_SECONDS + 5)
    assert not thread.is_alive()


def test_serve_stops_when_its_standard_input_closes(tmp_path):
    """The flag as the Windows tray app uses it: a real `lectern serve`, its stdin a pipe."""
    port = _free_port()
    env = {**os.environ, "LECTERN_HOME": str(tmp_path / "home")}
    log = tmp_path / "serve.log"
    with log.open("wb") as out:
        process = subprocess.Popen(  # noqa: S603 (fixed argv)
            [sys.executable, "-c", "from lectern.cli import app; app()", "serve",
             "--no-browser", "--port", str(port), "--stop-on-stdin-close"],
            stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, env=env,
        )  # fmt: skip
    try:
        for _ in range(100):
            try:
                if httpx2.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx2.HTTPError:
                time.sleep(0.1)
        process.stdin.close()
        assert process.wait(timeout=SHUTDOWN_GRACE_SECONDS + 10) == 0
    finally:
        if process.poll() is None:
            process.kill()
    output = log.read_text()
    assert "Application shutdown complete" in output and "Traceback" not in output
