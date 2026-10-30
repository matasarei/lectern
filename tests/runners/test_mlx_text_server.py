"""The key in front of MLX's text server (lectern.runners.mlx_text_server). The guard works on
any request handler, so it's tested here on a stand-in, with no MLX installed."""

from __future__ import annotations

import contextlib
import http.client
import os
import subprocess
import sys
import threading
import types
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lectern.runners import mlx_text_server

KEY = "k3y-for-this-test"
ANSWER = b"the model's answer"


def _handler() -> type[BaseHTTPRequestHandler]:
    """A fresh stand-in for mlx_lm.server's handler (the guard changes a class in place). Like
    the real one, it answers any website."""

    class Handler(BaseHTTPRequestHandler):
        def answer(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))  # as the real one does
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(ANSWER)))
            self.end_headers()
            self.wfile.write(ANSWER)

        do_GET = do_POST = do_OPTIONS = do_DELETE = answer  # noqa: N815 (http.server's names)

        def log_message(self, *args: object) -> None:  # keep the test output clean
            pass

    return Handler


@contextlib.contextmanager
def _serving(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _ask(
    port: int, method: str, path: str = "/v1/models", **headers: str
) -> tuple[int, dict, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=b"{}" if method == "POST" else None, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.fixture
def port() -> Iterator[int]:
    handler = _handler()
    mlx_text_server.guard(handler, KEY)
    with _serving(handler) as port:
        yield port


@pytest.mark.parametrize("method", ["GET", "POST", "OPTIONS", "DELETE"])
def test_a_request_without_the_key_is_refused_and_told_nothing(port, method):
    status, headers, body = _ask(port, method, Origin="https://evil.example")
    assert status == 401
    assert ANSWER not in body
    # Nothing that lets a web page read even the refusal.
    assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer wrong",
        f"Bearer {KEY[:-1]}",  # a prefix of the key
        f"Bearer {KEY}x",
        f"bearer {KEY}",  # the scheme is compared as written
        f"Basic {KEY}",
        KEY,
        "",
    ],
)
def test_a_wrong_key_is_refused(port, authorization):
    status, _, body = _ask(port, "POST", Authorization=authorization)
    assert status == 401 and ANSWER not in body


def test_a_refused_body_sent_too_slowly_is_closed_unanswered(monkeypatch):
    import socket

    monkeypatch.setattr(mlx_text_server, "REFUSED_READ_SECONDS", 0.2)
    handler = _handler()
    mlx_text_server.guard(handler, KEY)
    with _serving(handler) as port, socket.create_connection(("127.0.0.1", port)) as client:
        client.settimeout(10)  # the test's own bound: the server should close long before
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                       b"Content-Length: 1000\r\n\r\n{")  # fmt: skip
        assert client.recv(1024) == b""  # closed, and told nothing


def test_a_refused_body_trickled_in_is_closed_once_the_whole_read_is_too_long(monkeypatch):
    """A byte every 50 ms never lets one wait run out; 1000 of them would take 50 s. The
    deadline is for the whole body, so the server closes it after 0.3 s."""
    import socket

    monkeypatch.setattr(mlx_text_server, "REFUSED_READ_SECONDS", 0.3)
    handler = _handler()
    mlx_text_server.guard(handler, KEY)
    done = threading.Event()
    with _serving(handler) as port, socket.create_connection(("127.0.0.1", port)) as client:
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                       b"Content-Length: 1000\r\n\r\n")  # fmt: skip

        def trickle() -> None:
            with contextlib.suppress(OSError):  # the server closed it
                while not done.wait(0.05):  # the pace a slow sender keeps: time is the point
                    client.sendall(b"x")

        sender = threading.Thread(target=trickle, daemon=True)
        sender.start()
        client.settimeout(10)  # the test's own bound: the server should close long before
        try:
            assert client.recv(1024) == b""  # closed, and told nothing
        finally:
            done.set()
            sender.join(timeout=5)


def test_the_key_in_the_address_is_not_a_key(port):
    status, _, _ = _ask(port, "GET", f"/v1/models?key={KEY}&api_key={KEY}")
    assert status == 401


@pytest.mark.parametrize("method", ["GET", "POST", "OPTIONS", "DELETE"])
def test_the_right_key_reaches_the_server(port, method):
    status, _, body = _ask(port, method, Authorization=f"Bearer {KEY}")
    assert status == 200 and body == ANSWER


def test_without_the_guard_the_stand_in_answers_anyone():
    # What the stock server does, and what the guard is for.
    with _serving(_handler()) as port:
        status, headers, body = _ask(port, "GET", Origin="https://evil.example")
    assert status == 200 and body == ANSWER and headers["Access-Control-Allow-Origin"] == "*"


def test_started_without_a_key_it_refuses_before_loading_anything():
    env = {k: v for k, v in os.environ.items() if k != mlx_text_server.KEY_ENV}
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "lectern.runners.mlx_text_server", "--model", "/nowhere"],
        env=env, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode != 0
    assert mlx_text_server.KEY_ENV in done.stderr
    assert "Traceback" not in done.stderr  # a message, and no MLX needed to give it


def test_main_guards_the_servers_handler_then_hands_over(monkeypatch):
    handler = _handler()
    started = []
    server = types.ModuleType("mlx_lm.server")
    server.APIHandler = handler
    # The key is gone from the environment by the time the server runs.
    server.main = lambda: started.append(os.environ.get(mlx_text_server.KEY_ENV))
    package = types.ModuleType("mlx_lm")
    package.server = server
    monkeypatch.setitem(sys.modules, "mlx_lm", package)
    monkeypatch.setitem(sys.modules, "mlx_lm.server", server)
    monkeypatch.setenv(mlx_text_server.KEY_ENV, KEY)

    mlx_text_server.main()

    assert started == [None]
    with _serving(handler) as port:
        assert _ask(port, "POST")[0] == 401
        assert _ask(port, "POST", Authorization=f"Bearer {KEY}")[0] == 200


def test_the_real_server_still_has_what_the_guard_holds_on_to():
    """Runs where MLX is installed (a Mac with lectern-ai[mlx]); CI is Linux and skips it. When
    it fails, mlx-lm has moved its handler: read its server before widening the version."""
    server = pytest.importorskip("mlx_lm.server")
    handler = server.APIHandler
    assert issubclass(handler, BaseHTTPRequestHandler)
    assert {"do_GET", "do_POST", "do_OPTIONS"} <= {n for n in dir(handler) if n.startswith("do_")}
    assert callable(server.main)
