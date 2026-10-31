# Text files read and written without an encoding: cp1251 on a Russian/Ukrainian Windows

**Status (2026-10-29):** built on branch `fix/text-encoding-utf8`, not committed. Of the AST
scan's 23 hits in `src/lectern`, 11 were text I/O (fixed); 12 weren't (`os.open` ×3,
`Image.open` ×2, `tarfile.open`, `webbrowser.open`, `fetch.py`'s binary `part.open(mode)`, and
`files.write_text` ×4, which encodes UTF-8 itself) — the guard test allows those by name. Tests:
only the 5 bare reads behind the 4 Windows failures were fixed (168 bare calls in `tests/`
overall; a sweep would be its own change). Not done: a line in CLAUDE.md's Python conventions
("name the encoding") — CLAUDE.md is the developer's to change.

**Type:** bug
**Language:** en
**Asked:** "write all issues to the tasks to fix them" (M4 Windows run, 2026-10-29)
**Scope:** Windows, when Lectern isn't started by the tray (`lectern.cmd serve`, `uv run lectern
serve`, the test suite); the tray sets `PYTHONUTF8=1` (`Lectern.cs:194`)

## Summary
- About 20 `read_text()`/`write_text()` calls in `src/lectern` have no `encoding`; on this laptop
  the default is cp1251. Worst: the sandbox's test-run `main.py` (`modules/sandbox/tools.py:431`):
  Cyrillic → `SyntaxError: Non-UTF-8 code` in the sandbox; "₴" → `UnicodeEncodeError`, which kills
  the turn (agent-turn-dies-silently.md).
- Fix: `encoding="utf-8"` everywhere, enforced by a repository test (ruff can't: its
  `PLW1514` is preview-only in 0.16.9 and finds 0 of these, since it doesn't check pathlib's
  `read_text`/`write_text`); set `PYTHONUTF8=1` in `bin\lectern.cmd` too, as the tray does.

## Cause
- `tools.py:431` `candidate.write_text(code)`; `:286` `manifest.read_text()` (paths with Cyrillic
  in a project → `except ValueError` returns `[]` silently); `:495` `script.write_text(...)`.
- Others (ASCII today, fix anyway): `downloads/manager.py:425, 435, 550`,
  `runners/binaries.py:210`, `runners/mlx.py:78`; find all with
  `grep -rnE "read_text\(|write_text\(" src/lectern | grep -v encoding`.
- `install.ps1` writes `bin\lectern.cmd` without `PYTHONUTF8` (`packaging/windows/install.ps1:234-244`).
- Proven in `lectern-sandbox:2`: a cp1251 `print("Сума: 409,62 грн")` → SyntaxError, exit 1; a
  cp1251 Cyrillic *comment* after `# -*- coding: utf-8 -*-` runs (why a comment-only script
  "passed").

## Acceptance criteria
- [ ] No `read_text`/`write_text`/`open()` in text mode without `encoding` in `src/lectern`;
      a test fails on a new one.
- [ ] With `PYTHONUTF8` unset on a cp1251 Windows, a Ukrainian script with "₴" is written, runs in
      the sandbox and prints correctly.
- [ ] `bin\lectern.cmd` sets `PYTHONUTF8=1`.
- [ ] The tests that read the app's output use `encoding="utf-8"` (the 19 Windows failures that
      are tests' own: see windows-ci-and-tests.md).

## Steps
1. Fix every hit with `encoding="utf-8"`; add a test that scans `src/lectern` (AST: calls to
   `read_text`/`write_text`/`open` in text mode without an `encoding` keyword) and fails.
   - Create: `tests/test_text_encoding.py`
   - Modify: the files above
   - Test: also a unit test writing a script with "Сума ₴" through `_write_and_test`'s write,
     read back as UTF-8 (`tests/modules/test_sandbox_tools.py`)
2. `PYTHONUTF8=1` in the generated `lectern.cmd`/`lectern-url.cmd`.
   - Modify: `packaging/windows/install.ps1:234-244`
   - Test: `tests/test_packaging.py` (the generated text)
3. The same rule in `tests/` (`read_text()` of the app's output).

## How to check it
- `uv run pytest tests/test_text_encoding.py -q && uv run pytest -q` (on Windows: with
  `PYTHONUTF8` unset)

## Do not touch
- Binary reads/writes (`read_bytes`, models, archives).

## Evidence
- **Data:** `locale.getpreferredencoding(False)` = cp1251 here; step 6 Ukrainian run: "Couldn't
  write a working script after 3 fixes … exit 1"; the ₴ run: dead turn.
