# A justfile that does what the Makefile does, on Windows too

**Type:** feature
**Language:** en
**Asked:** We need to test this project on windows, but first lets add one thing for the smooth installtion: the justfile, which runs the same that make runs on linux and mac, but this will also support windows installations

## Summary
- Add a `justfile` at the repository root, beside the `Makefile`, with the same four entry points:
  `just` (dev build), `just install`, `just uninstall` and `just purge`. On Linux and macOS each one
  calls the same scripts as make. On Windows they call `install.ps1` / `uninstall.ps1` through
  Windows PowerShell 5.1.
- The Makefile stays as it is. Document `just` in the README's Quick start and Windows sections
  (`winget install Casey.Just`).
- The biggest risk is Windows exit codes and prompts: a failed step must fail the recipe, and a purge
  must ask only once. Both were checked with just 1.58 on this PC (see Evidence).

## Design

**Where it hooks in.** A new `justfile` at the repository root. Nothing that exists changes behaviour.
The recipes only call what already exists:

| Make target | `just` recipe | Linux / macOS (`[unix]`) | Windows (`[windows]`) |
|---|---|---|---|
| `make` (`all`) | `just` / `just build` | `uv sync`, `npm --prefix web run build` | the same two lines |
| npm ci when the lock changed | `web-deps` (dependency of `build`) | `[ web/node_modules/.package-lock.json -nt web/package-lock.json ] \|\| npm --prefix web ci` | the same check with `Get-Item … LastWriteTime` |
| `make install` | `just install` | `./install.sh` | `powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1` |
| `make uninstall` | `just uninstall` | `./uninstall.sh` | `… -File uninstall.ps1` |
| `make purge` / `make uninstall PURGE=1` | `just purge` | asks (same text and same y/yes rule as the Makefile), then `./uninstall.sh --purge` | `… -File uninstall.ps1 -Purge` (uninstall.ps1 asks for itself, so the recipe does not ask again) |

**Shape of the file:**
- `set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]`. Use Windows
  PowerShell 5.1, which every Windows 10/11 has and which the installers target. Do not use `pwsh`.
  Without this setting, just runs `sh` on Windows, which needs Git Bash on `PATH`.
- `build` comes first, so a bare `just` runs it, as a bare `make` runs `all`. Its body is the same on
  both systems, so it has no OS attribute. Only `web-deps`, `install`, `uninstall` and `purge` come in
  `[unix]`/`[windows]` pairs with the same name.
- Unix `purge` is a shebang recipe (`#!/usr/bin/env sh`), so the question and the wipe are one shell
  run, for the same reason the Makefile gives (a `No` must stop the wipe). It also prints
  `${LECTERN_DIR:-$HOME/.lectern}` as the Makefile does. Don't use just's `[confirm]` here: its message
  is fixed text and can't name the directory.
- `make uninstall PURGE=1` has no equivalent. `just purge` is the one way to wipe. That also avoids
  the leftover-`PURGE`-in-the-environment problem the Makefile guards against.
- The Makefile's "run it in <dir>" guard isn't needed: just looks upward for the justfile and runs
  recipes in the justfile's own folder, including with `just -f <path>`.
- `LECTERN_DIR` and the installers' other settings reach the scripts through the environment, as they
  do with make.
- At the top, a comment block in the Makefile's style lists the recipes. Each recipe gets a `#`
  doc-comment, so `just --list` explains it.

**Long-running work:** none. This is a CLI that the developer runs and waits on, and the installers
already print their own progress.

## Acceptance criteria
- [ ] `just --list` in the repository root lists `build`, `install`, `uninstall` and `purge` (and
  `web-deps`) on Windows, and the same names on macOS/Linux.
- [ ] On Windows, a bare `just` runs `npm --prefix web ci` only when `web/node_modules` is missing or
  older than `web/package-lock.json`, then `uv sync` and `npm --prefix web run build`.
  `src/lectern/web_dist/` is rebuilt.
- [ ] On Windows, `just install` runs `install.ps1` from any shell (PowerShell, cmd, Git Bash), and
  the recipe's exit code is non-zero when the installer fails.
- [ ] On Windows, `just purge` asks once (uninstall.ps1's question). Answering `n` prints
  `Nothing removed.` and exits non-zero.
- [ ] On macOS/Linux, `just purge` asks with the Makefile's text and directory. `n`, or no input
  (`just purge </dev/null`), removes nothing and exits non-zero. `yes | just purge` goes on.
- [ ] `just -f <repo>/justfile --dry-run install`, run from another folder, shows the commands run from
  the repository root.
- [ ] The Makefile is unchanged, and `make` behaves as before.
- [ ] The README's Quick start and "Install on Windows" sections mention `just` and how to get it.

## Steps
Test order: build, then check by hand. There is no test harness for the Makefile, and CLAUDE.md
doesn't ask for test-first here.

- [x] 1. Add the justfile: `justfile` (new) — a73f279. A real `just` on this PC ran npm ci, uv
  sync and the web build (exit 0). The second run skipped npm ci.
  Ruling: windows-shell has `-ExecutionPolicy Bypass`. Under the default policy PowerShell
  refused npm.ps1 and the npm ci line still exited 0. The line now checks `$?` and
  `$LASTEXITCODE`.
- [x] 2. install/uninstall/purge pairs in `justfile` (36b4d00). On Windows: `echo n | just purge`
  printed "Nothing removed." with exit 1. `just uninstall` on a dir that isn't an install exits 1.
  The Unix purge body was run under Git Bash with the wipe swapped for an echo: y/Y/yes went on,
  n/empty/EOF exited 1. Not run on a real Mac/Linux yet.
  Ruling: on Windows each recipe runs the script with `powershell -File`, which passes the exit
  code through.
- [x] 3. README Quick start and Install on Windows (a6fbad4): the just forms beside the
  make/powershell ones, `winget install Casey.Just` as optional, and a line on `just` for the
  dev build.

1. Add the justfile with the build recipes (`build`, `web-deps`) and the shells.
   - Create: `justfile`
   - Modify: none
   - Test: on Windows, `just --list`, then `just` (expect npm ci on the first run only, then
     `uv sync`, then the web build). Run `just` again: npm ci is skipped.
2. Add `install`, `uninstall` and `purge` as `[unix]`/`[windows]` pairs.
   - Create: none
   - Modify: `justfile`
   - Test: on Windows, `just --dry-run install`, `just --dry-run uninstall` and
     `just --dry-run purge`, then `echo n | just purge` (expect uninstall.ps1's question,
     `Nothing removed.`, non-zero exit). On a Mac, run `just --dry-run` for each, then
     `echo n | just purge` and `just purge </dev/null` (expect `Nothing removed.`, non-zero exit).
3. Document it.
   - Modify: `README.md:46-63` (Quick start): `just install` as an option beside `make install`, and
     `just uninstall` / `just purge`.
   - Modify: `README.md:387-430` (Install on Windows): add `winget install Casey.Just` to **Needs** as
     optional. In step 1, **Update** and **Uninstall**, put `just install` / `just uninstall` /
     `just purge` beside the `powershell -ExecutionPolicy Bypass -File …` forms. Keep the
     `powershell` forms: just is optional, and the installer needs no extra tools.
   - Test: none. Read it against the justfile.

## How to check it
- Windows (this PC): `just --list; just; just --dry-run install; just --dry-run purge`, then
  `echo n | just purge; echo $LASTEXITCODE` (expect non-zero, nothing removed)
- macOS: `just --list && just --dry-run install && (echo n | just purge; echo $?)`
- `git diff --stat` shows only `justfile` (new) and `README.md`.

## Do not touch
- `Makefile`: kept for Mac/Linux users and for `docs/acceptance/m4.md`, which records
  `make install` runs.
- `install.ps1`, `uninstall.ps1`, `install.sh`, `uninstall.sh`, `packaging/**`: the justfile only
  calls them. Their behaviour, including uninstall.ps1's own purge question, stays the same.
- `.github/workflows/ci.yml`: no CI check for the justfile (decided below).
- `docs/acceptance/*`: these record what was tested. The Windows test run will add to them later,
  not this change.

## Evidence
- **Code:** `Makefile` defines what to match: `all` (npm ci only when the lock file changed, then
  `uv sync`, then the web build), `install`, `uninstall`, and `purge` (asks y/yes in the same shell
  command, then `./uninstall.sh --purge`).
- **Code:** `packaging/linux/uninstall.sh:10-11` and `packaging/macos/uninstall.sh:9-10`:
  `--purge` doesn't ask, so the Unix recipe has to ask itself.
- **Code:** `packaging/windows/uninstall.ps1:39-45`: `-Purge` asks itself with `Read-Host` (y/yes
  only), so the Windows recipe must not ask a second time.
- **Code:** `install.ps1` / `uninstall.ps1` are wrappers that pass `$LASTEXITCODE` through. The
  README runs them with `powershell -ExecutionPolicy Bypass -File …`.
- **Data:** just 1.58.0 is on this PC (`WindowsApps\just`) `[local check]`.
- **Data:** with `windows-shell` set to `powershell.exe … -Command`, a failing line stops the recipe
  with a non-zero exit, `os()` reports `windows`, and `[confirm]` refuses on `n` and on EOF. This was
  checked in a throwaway justfile in the scratchpad `[local check]`.
- **Decided in the chat:** justfile beside the Makefile, or replacing it? Keep both `[answered]`
- **Decided in the chat:** match the Makefile only, or also add test/lint/serve? Match the Makefile
  only `[answered]`
- **Decided in the chat:** add a CI parse check for the justfile? No CI change `[answered]`
- **Decided here:** no `just uninstall PURGE=1` form. `just purge` is the only way to wipe. Every
  option led to the same steps, so this was not asked.
- **Still open:** none.

## Notes
- Blocker found by `just install` on Windows (7eb75f7): `packaging/windows/install.ps1` Find-Wheel
  returned npm/uv output along with the wheel path, so Split-Path failed. Fixed with `| Out-Host`.
  It also failed with plain `powershell -File install.ps1`, so the bug wasn't in the justfile.
  It hadn't shown up before because the Windows build from a working copy had never been run.
- /gku:fix, 2026-10-29: the `just` tests are in `tests/test_packaging.py` (12d23fd), and web-deps
  is `[private]` (05d908b). The Unix purge tests skip on Windows, and CI's Linux job doesn't install
  just, so they still have to run on a Mac (`uv run pytest tests/test_packaging.py -k just`).

<!-- postponed by /gku:fix, 2026-10-29 -->
4. [x] NIT `C:\Users\matas\Projects\lectern\justfile:25`: `[ -nt ]` isn't strict POSIX. No change
   needed while dash, bash, zsh and macOS sh all support it. — **closed, no change (2026-10-30): the nit itself says none is needed**
5. [x] NIT `C:\Users\matas\Projects\lectern\packaging\windows\install.ps1:144-150`: the
   `| Out-Host` fix has no automated test. Nothing in the repository runs its working-copy build.
   For now the proof is a manual `just install` on Windows.
 — **closed, no change (2026-10-30): nothing in CI runs a working-copy build; a manual `just install` on Windows is the proof**