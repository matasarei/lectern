"""Windows' event loop, kept accepting when a client is gone before its connection is accepted.

asyncio's proactor loop closes its listening socket on any error from an accept
(asyncio/proactor_events.py, `_start_serving`: "Accept failed on a socket"), and a client that
resets before its accept completes gives one, ERROR_NETNAME_DELETED. One such client, the browser
reconnecting while Lectern started, and nothing answered on Lectern's port again, the process and
the tray app still running (docs/acceptance/m4.md, Windows results). Open upstream:
https://github.com/python/cpython/issues/93821.

Here the accept is tried again for such a client, so the loop never sees the error. Windows only:
`asyncio.IocpProactor` exists nowhere else.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

# A connection that was gone by the time it was accepted (Windows error numbers).
ERROR_NETNAME_DELETED = 64
ERROR_CONNECTION_ABORTED = 1236


def gone_before_accepted(exc: BaseException | None) -> bool:
    if isinstance(exc, ConnectionResetError | ConnectionAbortedError):
        return True
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in (
        ERROR_NETNAME_DELETED,
        ERROR_CONNECTION_ABORTED,
    )


def new_loop() -> asyncio.AbstractEventLoop:
    """A proactor event loop (the one asyncio and uvicorn use on Windows) whose accepts outlast
    a client that's gone."""

    class SteadyProactor(asyncio.IocpProactor):  # type: ignore[name-defined]
        def accept(self, listener: socket.socket) -> asyncio.Future:
            accepted = self._loop.create_future()
            current: asyncio.Future | None = None  # the accept under way

            def attempt() -> None:
                nonlocal current
                current = super(SteadyProactor, self).accept(listener)
                current.add_done_callback(finished)

            def finished(done: asyncio.Future) -> None:
                if accepted.done():
                    return
                if done.cancelled():
                    accepted.cancel()
                    return
                exc = done.exception()
                if exc is None:
                    accepted.set_result(done.result())
                elif gone_before_accepted(exc) and listener.fileno() != -1:
                    try:
                        attempt()  # the next client
                    except OSError as error:  # couldn't start one: as asyncio's own accept would
                        accepted.set_exception(error)
                else:
                    accepted.set_exception(exc)

            def cancelled(future: asyncio.Future) -> None:
                # The server closing cancels what it waits on: the accept under way goes too.
                if future.cancelled() and current is not None:
                    current.cancel()

            attempt()
            accepted.add_done_callback(cancelled)
            return accepted

    loop = asyncio.ProactorEventLoop(proactor=SteadyProactor())  # type: ignore[attr-defined]
    loop.set_exception_handler(_without_gone_clients)
    return loop


def _without_gone_clients(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Inside asyncio's accept a task still ends with the gone client's error, and nothing
    waits on it: asyncio would log "Task exception was never retrieved", a traceback in
    Lectern's log for every such client. Those alone are left out."""
    task = context.get("future")
    coro = task.get_coro() if isinstance(task, asyncio.Task) else None
    if (
        context.get("message") == "Task exception was never retrieved"
        and gone_before_accepted(context.get("exception"))
        and getattr(coro, "__name__", "") == "accept_coro"
    ):
        return
    loop.default_exception_handler(context)
