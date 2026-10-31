# Run the test suites on Windows in CI, and fix the tests that fail there

**Status (2026-10-30):** built on branch `ci/windows-tests`, not committed. A `windows-tests` job
(bash shell: the runner's PowerShell reports a step's last command only). Installer tests skip
on Windows (`_bash()` in test_packaging.py; a module skip for the Mac one). POSIX assumptions
fixed: `str(Path(...))` expectations, a `.cmd` launcher for fake servers (`_executable()`), a
`symlinks` fixture that skips where links can't be made, the POSIX-mode asserts split out.
Unreliable tests fixed at the cause, not retried: the runtime takes a `clock` (Windows' 15 ms
steps made two readings equal); the MLX guard reads a refused request's body before its 401
(Windows reset the connection: ConnectionAbortedError); one lock around every Alembic
migration (its `op`/`context` are process-wide: two at once took each other's away); the web
tests wait up to 5 s for what findBy* looks for (the lazy Markdown module under load).

**Type:** feature
**Language:** en
**Asked:** "write all issues to the tasks to fix them" (M4 Windows run, 2026-10-29)
**Scope:** `.github/workflows/ci.yml`, `tests/`

## Summary
- CI runs the suites on Linux only. On Windows (this laptop): 67 failures in the Mac and Linux
  installer tests (they run shell scripts), 19 more that are the tests' own. The Windows-only
  bugs of this run (tray-freeze, listener, encoding) can only be kept fixed with a Windows job.
- Fix: a `windows-latest` job running `pytest` (and the web tests), with installer tests skipped
  off their platform and the 19 made portable.

## The 19 (from `pytest` on Windows, 2026-10-29; full output `.tasks/m4-windows-evidence/pytest-win.txt`)
- **Encoding (tests read UTF-8 output with a bare `read_text()`):** `tests/modules/ocr/test_jobs.py`
  (2), `test_store.py`, `tests/modules/web/test_tools.py`.
- **POSIX paths expected:** `tests/runners/test_llamacpp.py` (`'\\run\\key' == '/run/key'`,
  mmproj path), `tests/modules/test_container.py` (`source=\p\a,b`).
- **Symlinks need Developer Mode (WinError 1314):** `tests/test_downloads.py` (3),
  `tests/test_models_api.py` (link case), `test_container.py` (outputs). Skip without the
  privilege, or use a junction where the code means "a link".
- **A script run as an exe (WinError 193):** `test_llamacpp.py` fake server cases.
- **POSIX modes:** `test_container.py` (`0o700` vs `0o777`).
- **The real clock (15 ms steps on Windows):** `tests/agent/test_loop.py::…waiting_for_a_card…`
  (`assert during > 0`), `tests/runtime/test_supervisor.py::test_start_now_lets_a_held_job_in`.
  CLAUDE.md: inject the clock.
- **Setup error:** `tests/modules/ocr/test_api.py` (an ERROR at fixture setup; `KeyError: 'config'`
  / `NoneType.create_table`) — look first. Flaky: it didn't come back in the next full run.
- **Flaky on Windows, on `main` too:** `tests/runners/test_mlx_text_server.py` — a different test
  fails on each run (`test_a_wrong_key_is_refused[…]`, `test_the_right_key_reaches_the_server[POST]`,
  `test_main_guards_the_servers_handler_then_hands_over`); 1 of 3 runs passed.
- **The web suite on Windows** (`npm --prefix web test`): 2 failures once, right after a full
  Python run; 374 of 374 the next time. Not looked into.

## Acceptance criteria
- [ ] `tests/test_packaging_macos.py` and the Linux installer tests skip on Windows with a reason.
- [ ] The 19 pass on Windows (or skip with a stated reason where the OS can't).
- [ ] CI has a Windows job (pytest + web tests); `gpu`/`sandbox` markers skip as on Linux.
- [ ] The new tests of windows-tray-freeze.md / windows-listener-closes.md run there.

## Steps
1. Platform skips for installer tests. Modify: `tests/test_packaging*.py`
2. Fix the 19 (above). Modify: the listed tests; a `clock` fixture in `tests/conftest.py`
3. CI job. Modify: `.github/workflows/ci.yml` (a `windows-latest` matrix entry; the existing
   csc compile step stays)
4. Docs: CLAUDE.md "CI" line.

<!-- postponed by /gku:fix, 2026-10-30 -->
5. [x] NIT `C:\Users\matas\Projects\lectern\src\lectern\runners\mlx_text_server.py:46` — the
   refused body is read with no timeout: a client announcing ≤ 64 KiB and sending it slowly
   holds a handler thread (as slow headers already do; localhost, Mac only). A socket timeout
   for that read. — **fixed in chore/m4-nit-cleanup**
6. [x] NIT `C:\Users\matas\Projects\lectern\tests\modules\test_container.py` —
   `test_mount_values_quote_awkward_paths` computes the expected quoting as the code does
   (`.replace('"', '""')`); a literal per OS would check it independently. — **fixed in chore/m4-nit-cleanup**
7. [x] NIT `C:\Users\matas\Projects\lectern\tests\runners\test_llamacpp.py` — on Windows a
   stopped fake server exits by itself within 60 s (stopping ends only the .cmd above it). By
   design; a job object or a tree kill in the test would end it at once. — **fixed in chore/m4-nit-cleanup**

## How to check it
- On Windows: `uv run pytest -q` all green; the PR's Windows job green.

## Do not touch
- What the tests assert on Linux/macOS (make them portable, don't weaken them).
