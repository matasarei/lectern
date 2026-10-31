# Windows: a tray-started Lectern freezes at its first model load

**Status (2026-10-29):** built on branch `fix/windows-tray-stop-event`, not committed. Differs
from the design below in one place: instead of moving `read_shape` off the event loop (four
synchronous paths reach it), its cost is gone: gguf's `GGUFReader` spent 9.48 s building the
tokenizer's 744k fields (profiled), so `runtime/gguf_header.py` reads only the header's values
(0.05 s, the same shapes for all three models here). Nothing at runtime imports gguf or numpy
now; `gguf` (and numpy with it) could move to the dev dependencies — not done (changes the lock).

**Type:** bug
**Language:** en
**Asked:** "You mentioned that the tray run breaks? what can we do about it and why it not breaks on
mac? it should be a windows service I believe, not just an app" (M4 Windows run, 2026-10-29)
**Scope:** found and proven on the RTX 4060 laptop (docs/acceptance/m4.md, Windows results)

## Summary
- As installed on Windows, Lectern can't run a model: the first model load freezes the whole
  server (`/health` too) until Quit. Release blocker.
- Cause: the tray stops Lectern by closing a stdin pipe, so `serve` keeps a thread in a blocking
  read on it; numpy's OpenBLAS, loaded lazily at the first model load, waits on that handle
  inside the DLL loader. Fix: stop Lectern through a named event, never through stdin.
- A Windows service would not fix it by itself and costs the no-admin install; keep the tray.

## Cause
- `packaging/windows/tray/Lectern.cs:180-184`: `serve … --stop-on-stdin-close`,
  `RedirectStandardInput = true`; Stop is `child.StandardInput.Close()` (`:248-256`).
- `src/lectern/cli.py:331-344`: `LecternServer.serve` starts thread "stdin", which blocks in
  `stdin.read(4096)` until the pipe closes.
- First model load: `supervisor._try_admit` → `wiring.resolve:227` → `wiring._shape:150` →
  `planner.read_shape:60` → `from gguf import GGUFReader` → numpy → `libscipy_openblas64_` in
  `LdrLoadDll` → `RtlEnterCriticalSection` (py-spy --native). On Windows a pending synchronous
  read on a handle blocks other calls on it; OpenBLAS's start-up touches the std handles, under the
  loader lock. Runs on the event loop, so everything stops.
- Proven: scratch probe, same Python + numpy: a thread reading a stdin pipe → `import numpy`
  waits until stdin closes (19.6 s), 0.1 s without it. `pypdfium2`, Pillow unaffected.
- **Why not on the Mac or Linux:** the Mac app starts the server with stdin `/dev/null`
  (`packaging/macos/tray/Lectern.swift:152`) and stops it with a signal; Linux is systemd and a
  signal (`packaging/linux/lectern.service:16`). Nothing reads stdin there, and POSIX has no such
  handle lock. Windows needs the stdin trick only because a console-less process can't be sent
  Ctrl+C.

## Design
**Stop through a named event.** The tray already names its events per install folder
(`Lectern.cs:39-43`, `Local\Lectern-<dir>-open/-quit`). It creates one more,
`Local\Lectern-<dir>-stop`, passes its name to `serve --stop-event <name>`, and starts the child
with stdin `NUL` (no redirect). `serve` waits on it in a thread through `ctypes`
(`OpenEventW(SYNCHRONIZE)` + `WaitForSingleObject`; no new dependency), then calls the same
`_stop` as now. Stop = `SetEvent`; the stop timer and `job.EndAll()` stay as the fallback.
`--stop-on-stdin-close` stays for compatibility one release, unused by the tray.

**And take the import off the event loop**, so nothing like this can freeze `/health` again:
`read_shape` runs in a thread (`run_in_threadpool`/`asyncio.to_thread` at its call site), and
`gguf` is imported once at start-up, before any thread is started (it also cost ~2 s of a frozen
`/health` at the first load in the good case).

**On "it should be a Windows service":**

| | Tray app (now, fixed) | Per-user scheduled task ("at log on", as the user) | Windows service (SCM) |
|---|---|---|---|
| Admin to install | no | no | yes |
| Runs as | you | you | LocalSystem / a service account, session 0 |
| Data in `%USERPROFILE%\.lectern`, Docker Desktop's pipe | yes | yes | needs ACLs, docker-users membership |
| Shows state, opens the browser | itself | needs the tray as a client | needs the tray as a client |
| Runs with nobody signed in | no | no | yes |
| Fixes this bug | the stop event does | only with the stop event too | SCM stop signal: the same fix |

Recommendation: keep the per-user tray (it matches Linux's `systemd --user` and the Mac's
menu-bar app, and the README's no-admin install), fix the stop channel. A service is the right
shape only for a Windows *server* mode (accounts on, nobody signed in): a separate decision.

## Acceptance criteria
- [ ] Tray-started Lectern on Windows: a chat answer from the 9B; `/health` answers throughout.
- [ ] Stop/Restart/Quit from the tray stop Lectern cleanly (log: "Shutting down", no "didn't
      stop in time"); nothing left running.
- [ ] `serve` started by the tray has no thread reading stdin (py-spy dump).
- [ ] The first model load doesn't block the event loop (`/health` answers during it).
- [ ] macOS and Linux unchanged.

## Steps
1. `serve --stop-event NAME` (Windows only): a thread waits on the named event, then `_stop`.
   - Modify: `src/lectern/cli.py:46-51` (option), `:313-350` (`LecternServer`)
   - Test: `tests/test_cli.py`: a set event stops the server (Windows-marked; a fake waiter
     elsewhere)
2. The tray creates the event, starts the child with stdin NUL and `--stop-event`, Stop sets it.
   - Modify: `packaging/windows/tray/Lectern.cs:1-11` (header), `:39-43`, `:175-256`
   - Test: `tests/test_packaging.py` (compiles; argument present); by hand on Windows
3. `read_shape` off the event loop; `gguf` imported at start-up.
   - Modify: `src/lectern/runtime/wiring.py:150`, `src/lectern/runtime/planner.py:58-60`
   - Test: `tests/runtime/`: resolve doesn't call `read_shape` on the loop thread
4. Docs: `docs/acceptance/m4.md` Windows results (re-run step 5 through the tray), README
   "Install on Windows" if the stop is described there.

<!-- postponed by /gku:fix, 2026-10-29 -->
5. [x] NIT `C:\Users\matas\Projects\lectern\src\lectern\runtime\gguf_header.py:41` — a truncated
   file raises `struct.error` ("unpack_from requires a buffer of at least 76 bytes…") without the
   file's name: raise `ValueError(f"{path.name}: the header is cut short")`, with a test. — **fixed in chore/m4-nit-cleanup**
6. [x] NIT `C:\Users\matas\Projects\lectern\src\lectern\cli.py:366` — `winevent.wait_for`
   raising leaves only a traceback (the tray's 30 s timeout then ends Lectern by force): log one
   line saying the tray can't stop it cleanly; a test with a waiter that raises. — **fixed in chore/m4-nit-cleanup**
7. [x] NIT `C:\Users\matas\Projects\lectern\docs\acceptance\m4.md` — "branch
   `fix/windows-tray-stop-event`" goes stale at the merge: name the pull request instead.

## How to check it
- `uv run pytest tests/test_cli.py tests/runtime -q`
- On Windows: `install.ps1`, a chat through the tray; `uvx py-spy dump --pid <serve pid>` shows
  no "stdin" thread.

## Do not touch
- The job object (`Lectern.cs:845+`): it's what ends Lectern and its models if the tray dies.
- The Mac and Linux start/stop paths.

## Evidence
- **Code:** as listed in Cause.
- **Data:** freeze seen twice through the tray; ended at Quit both times (log 15:52:21: the
  queued request answered, then shutdown; `data\run\qwen3.5-9b` created that second).
- **Sources:** py-spy (`uvx py-spy dump --native`); the probe
  `.tasks/m4-windows-evidence/stdin_import_probe.py` (run with the install's Python:
  `& $HOME\.lectern\app\venv\Scripts\python.exe -I stdin_import_probe.py numpy`).
- **Decided in the chat:** service or app — recommendation above, the developer to confirm.
