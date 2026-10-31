# Windows: one dropped connection closes Lectern's listening socket for good

**Status (2026-10-29):** built on branch `fix/windows-listener-closes`, not committed. Differs
from the design below: (1) no override of asyncio's private `_start_serving` — a subclass of the
public `asyncio.IocpProactor` retries `accept()` for a client gone first, handed to uvicorn by its
own `Config(loop="lectern.core.winloop:new_loop")`; (2) the tray app **says** it in the menu and
the log after 3 missed checks (30 s apart), and does **not** restart Lectern (the developer's
choice: a busy Lectern would lose its work). Proven: 4/4 → 0 on a 160-reset burst; the tray's
check with the server process suspended. Left: CPython's failed accept leaves its socket to the
GC (a ResourceWarning, closed within a GC cycle), and #156365 (transport creation failing) is not
covered by the retry — the tray's check is what tells you then.

**Type:** bug
**Language:** en
**Asked:** "write all issues to the tasks to fix them" (M4 Windows run, 2026-10-29)
**Scope:** seen once (after step 7's update), cause read in the installed Python's stdlib

## Summary
- A second after the updated Lectern started, an accept failed with `WinError 64`
  (`ERROR_NETNAME_DELETED`, a client gone first) and nothing listened on 8765 again; the process
  and the tray stayed up, so it looked running. A Quit and Start fixed it.
- Python's Windows event loop closes the listening socket on any accept error: open upstream
  (python/cpython#93821). Lectern needs its own guard.

## Cause
- `asyncio/proactor_events.py` `_start_serving` (3.12.15, lines 864-871): on `OSError` from an
  accept, "Accept failed on a socket" and `sock.close()`.
- uvicorn 0.54 picks `asyncio.ProactorEventLoop` on Windows (`uvicorn/loops/asyncio.py:9-10`);
  Lectern needs the proactor for its subprocesses (llama-server, docker).
- Upstream: python/cpython#93821 (open, updated 2026-10-20); #156365 (open: stops accepting when
  transport creation fails; the `AssertionError` in `_attach` seen at a Quit, log 15:52:21).

## Design
Two layers:
1. **The loop:** `LecternServer.run` (cli.py) passes uvicorn a loop factory that, on Windows,
   returns a `ProactorEventLoop` subclass whose accept loop treats `ERROR_NETNAME_DELETED` (64),
   `ConnectionResetError`/`ConnectionAbortedError` as one failed connection: logged at debug, the
   next accept armed, the listening socket kept. It overrides a private method; pin the reason
   and the CPython issue in the docstring, with a test that fails if the method's name changes.
2. **The tray, as a safety net:** its health timer already polls; if Lectern was answering and
   stops answering for N seconds while its process is alive, the tray restarts it and says so in
   the log and the menu.

## Acceptance criteria
- [ ] A client that connects and resets before accept (a test that does it on a real socket on
      Windows) leaves the server answering.
- [ ] The tray restarts a Lectern whose process is alive but hasn't answered for N s after
      answering, once per incident, and writes why.
- [ ] Nothing changes on macOS/Linux (the factory returns the default there).

## Steps
1. Loop factory with the tolerant accept loop.
   - Modify: `src/lectern/cli.py:313+` (`LecternServer.run`), new small module
     `src/lectern/core/winloop.py`
   - Test: `tests/test_winloop.py` (Windows-marked: RST before accept; elsewhere: the factory is
     the default)
2. The tray's watchdog.
   - Modify: `packaging/windows/tray/Lectern.cs` (health timer)
3. Docs: m4.md Windows results, after a re-run of step 7.

<!-- postponed by /gku:fix, 2026-10-29 -->
4. [x] NIT `C:\Users\matas\Projects\lectern\docs\acceptance\m4.md` — "branch
   `fix/windows-listener-closes`" (and the tray fix's "branch `fix/windows-tray-stop-event`")
   go stale at the merge: name the pull requests instead (#132 for the tray fix).

## How to check it
- `uv run pytest tests/test_winloop.py -q` on Windows; then step 7 of the M4 runbook, with the
  browser tab open while it restarts.

## Do not touch
- The proactor itself (Lectern needs subprocesses); uvicorn's version pin, unless a release fixes it.

## Evidence
- **Data:** log 17:55:55–56: "Task exception was never retrieved … OSError(22, …, 64)", "Accept
  failed on a socket … laddr=('127.0.0.1', 8765)"; then refused connections, no listener.
- **Sources:** https://github.com/python/cpython/issues/93821 (open);
  https://github.com/python/cpython/issues/156365 (open).
