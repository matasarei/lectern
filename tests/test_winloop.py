from __future__ import annotations

import asyncio
import gc
import socket
import struct
import sys
import threading

import pytest

from lectern.core.winloop import gone_before_accepted

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows' proactor event loop")


class _WinError(OSError):
    """An OSError with a Windows error number, on any platform."""

    def __init__(self, winerror: int) -> None:
        super().__init__(22, "gone")
        self.winerror = winerror


@pytest.mark.parametrize(
    ("exc", "gone"),
    [
        (ConnectionResetError(), True),
        (ConnectionAbortedError(), True),
        (_WinError(64), True),  # ERROR_NETNAME_DELETED
        (_WinError(1236), True),  # ERROR_CONNECTION_ABORTED
        (_WinError(10048), False),  # address in use: a real failure, not a client gone
        (OSError(9, "bad file descriptor"), False),
        (ValueError(), False),
        (None, False),
    ],
)
def test_only_a_client_gone_counts_as_gone(exc, gone):
    assert gone_before_accepted(exc) is gone


def test_the_log_leaves_out_only_a_gone_clients_accept_task():
    from lectern.core.winloop import _without_gone_clients

    async def accept_coro() -> None:  # named as asyncio's own, inside IocpProactor.accept
        raise ConnectionResetError

    async def something_else() -> None:
        raise ConnectionResetError

    loop = asyncio.new_event_loop()
    logged: list[str] = []
    loop.default_exception_handler = lambda context: logged.append(context["message"])  # type: ignore[method-assign]
    try:
        for coro in (accept_coro(), something_else()):
            task = loop.create_task(coro)
            loop.run_until_complete(asyncio.wait([task]))
            _without_gone_clients(loop, {
                "message": "Task exception was never retrieved", "exception": task.exception(),
                "future": task,
            })  # fmt: skip
        _without_gone_clients(loop, {"message": "Accept failed on a socket"})
    finally:
        loop.close()
    assert logged == ["Task exception was never retrieved", "Accept failed on a socket"]


class _Listener:
    def fileno(self) -> int:
        return 5


def _accept_with(monkeypatch, outcomes):  # noqa: ANN001, ANN202
    """The loop's accept over a stand-in for asyncio's own: each call gives the next outcome,
    an exception to raise at once or a future's (result, exception)."""
    from lectern.core.winloop import new_loop

    loop = new_loop()
    calls: list[asyncio.Future] = []

    def accept(self, listener):  # noqa: ANN001, ANN202
        outcome = outcomes[len(calls)]
        if isinstance(outcome, BaseException):
            calls.append(None)  # type: ignore[arg-type]
            raise outcome
        future = loop.create_future()
        result, exc = outcome
        if exc is not None:
            future.set_exception(exc)
        elif result is not None:
            future.set_result(result)
        calls.append(future)
        return future

    monkeypatch.setattr(asyncio.IocpProactor, "accept", accept)  # type: ignore[attr-defined]
    return loop, loop._proactor.accept(_Listener()), calls  # type: ignore[attr-defined]


@windows_only
def test_a_gone_client_is_followed_by_the_next(monkeypatch):
    loop, accepted, calls = _accept_with(
        monkeypatch,
        [(None, _WinError(64)), (None, ConnectionResetError()), (("conn", "addr"), None)],
    )
    try:
        assert loop.run_until_complete(asyncio.wait_for(accepted, 1)) == ("conn", "addr")
    finally:
        loop.close()
    assert len(calls) == 3


@windows_only
def test_an_accept_that_cant_start_again_ends_with_its_error(monkeypatch):
    """Not left waiting: the server would stop accepting without a word."""
    loop, accepted, _calls = _accept_with(monkeypatch, [(None, _WinError(64)), _WinError(10055)])
    try:
        with pytest.raises(OSError) as raised:
            loop.run_until_complete(asyncio.wait_for(accepted, 1))
    finally:
        loop.close()
    # Not wait_for giving up: TimeoutError is an OSError too.
    assert not isinstance(raised.value, TimeoutError)
    assert raised.value.winerror == 10055


@windows_only
def test_closing_the_server_cancels_the_accept_under_way(monkeypatch):
    loop, accepted, calls = _accept_with(monkeypatch, [(None, _WinError(64)), (None, None)])
    try:
        loop.run_until_complete(asyncio.sleep(0.01))  # the first gone, the second waiting
        accepted.cancel()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    assert len(calls) == 2 and calls[1].cancelled()


def test_off_windows_the_server_runs_on_uvicorns_own_loop(paths, monkeypatch):
    from lectern.app import create_app
    from lectern.cli import server_config

    app = create_app(paths)
    monkeypatch.setattr(sys, "platform", "linux")  # only for server_config: the app is made
    assert server_config(app, 18765).loop == "auto"


def _reset_connections(port: int, count: int, refused: list[int]) -> None:
    for _ in range(count):
        with socket.socket() as s:
            s.settimeout(1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            try:
                s.connect(("127.0.0.1", port))
            except OSError:
                refused.append(1)
        # closed with SO_LINGER 0: a reset, before the server may have accepted it


def _answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.sendall(b"ping")
            return s.recv(4) == b"pong"
    except OSError:
        return False


@windows_only
# What a failed accept leaves: asyncio's own accept doesn't close its socket; the garbage
# collector does (cpython#156365's kind), with a ResourceWarning.
@pytest.mark.filterwarnings("ignore::ResourceWarning")
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_clients_that_reset_at_once_leave_the_server_accepting():
    """On asyncio's own proactor loop this burst ends with "Accept failed on a socket" and a
    server that answers nothing (4 runs of 4, Windows 11)."""
    from lectern.core.winloop import new_loop

    async def scenario() -> tuple[bool, list[str], list[int]]:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                if await reader.read(4) == b"ping":
                    writer.write(b"pong")
                    await writer.drain()
            except OSError:
                pass
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        logged: list[str] = []
        handler = loop.get_exception_handler()
        assert handler is not None

        def recording(loop: asyncio.AbstractEventLoop, context: dict) -> None:
            logged.append(context["message"])
            handler(loop, context)

        loop.set_exception_handler(recording)
        refused: list[int] = []
        threads = [
            threading.Thread(target=_reset_connections, args=(port, 20, refused)) for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        await asyncio.to_thread(lambda: [t.join() for t in threads])
        answered = await asyncio.to_thread(_answers, port)
        server.close()
        await server.wait_closed()
        return answered, logged, refused

    answered, logged, refused = asyncio.run(scenario(), loop_factory=new_loop)
    gc.collect()  # those sockets go now, inside this test, not in whichever runs next
    assert answered
    assert "Accept failed on a socket" not in logged
    assert refused == []


@windows_only
def test_lecterns_server_runs_on_that_loop(paths):
    from lectern.app import create_app
    from lectern.cli import server_config
    from lectern.core.winloop import new_loop

    assert server_config(create_app(paths), 18765).get_loop_factory() is new_loop
