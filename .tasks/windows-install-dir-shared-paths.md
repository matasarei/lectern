# Windows: a second install (LECTERN_DIR) takes over the first's shortcuts and log

**Type:** bug
**Language:** en
**Asked:** "write all issues to the tasks to fix them" (M4 Windows run, 2026-10-29)
**Scope:** `packaging/windows/install.ps1`, `uninstall.ps1`, the tray app

## Summary
- The Start-menu and Startup shortcuts and `%LOCALAPPDATA%\Lectern\Logs` don't depend on
  `LECTERN_DIR`. Installing a second Lectern repointed both shortcuts to it; uninstalling it
  removed them and tried to delete the first's log; both servers wrote one log (a line cut in two).
- The Mac has this guarded (`test_an_install_in_another_folder_doesnt_take_over_the_app`);
  Windows doesn't.

## Cause
- `install.ps1:71-72` fixed paths `…\Programs\Lectern.lnk`, `…\Startup\Lectern.lnk`; `:259-268`
  writes them for whichever `$dir`.
- `uninstall.ps1:68-80` removes both and `%LOCALAPPDATA%\Lectern\Logs` regardless of `$dir`.
- `Lectern.cs:3, :124` the log under `LocalApplicationData\Lectern\Logs` for every install.

## Design
As on the Mac: the default install (`%USERPROFILE%\.lectern`) owns the plain names; another
`LECTERN_DIR` gets shortcuts named after its folder ("Lectern (lectern-m4)") or none, and only
touches shortcuts whose target is its own `app\tray\Lectern.exe`. The log goes under the install
(`<dir>\logs\lectern.log`), or `Logs\<install-name>\`. Uninstall removes only what points into
its `$dir`.

## Acceptance criteria
- [ ] Installing to another `LECTERN_DIR` leaves the default install's shortcuts pointing at it.
- [ ] Uninstalling the other one removes only its own shortcuts and log.
- [ ] Two installs running at once write two logs.

## Steps
1. Shortcuts per install, checked by target before write/remove.
   - Modify: `packaging/windows/install.ps1:71-72, 259-268`; `uninstall.ps1:68-80`
   - Test: `tests/test_packaging.py` (the scripts' text: target check present); M4 step 1+9 by hand
2. The log per install.
   - Modify: `packaging/windows/tray/Lectern.cs:3, 124`, README "Install on Windows" (log path),
     `install.ps1:314, 341` (printed path)
3. Docs: README, m4.md runbook (Show Log path).

<!-- postponed by /gku:fix, 2026-10-30 -->
4. [x] NIT `C:\Users\matas\Projects\lectern\tests\test_windows_install_paths.py` —
   `test_the_tray_app_names_an_installs_log_folder_as_the_scripts_do` matches source text; a
   small compiled harness calling `Program.LogPath` would check the behaviour. — **closed, no change (2026-10-30): a compiled harness needs csc in the test job**

## How to check it
- M4 Windows steps 1 and 9 with `LECTERN_DIR` set, the default install running.

## Do not touch
- The per-folder event and mutex names (`Lectern.cs:39-43`): already per install.

## Evidence
- **Data:** after the second install, both shortcuts → `%TEMP%\lectern-m4\app\tray\Lectern.exe`;
  after its uninstall both gone; "lectern.log - The process cannot access the file because it is
  being used by another process"; log line `"GET /api/f2026-10-29 15:22:30 INFO: Started server…"`.
