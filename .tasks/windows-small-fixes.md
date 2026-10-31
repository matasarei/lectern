# Windows: small fixes from the M4 run

**Type:** bug
**Language:** en
**Asked:** "write all issues to the tasks to fix them" (M4 Windows run, 2026-10-29)
**Scope:** three independent one-to-few-line changes

## 1. The root install.ps1 exits 0 when the inner script refuses its arguments
- `install.ps1:5-6`, `uninstall.ps1` (root): `& … @args; exit $LASTEXITCODE`. A parameter-binding
  error (`install.ps1 -Wheel` with no value) is a PowerShell error, not a native exit code, so
  `$LASTEXITCODE` is unset and the wrapper exits 0.
- Fix: `$ErrorActionPreference = 'Stop'` and `try { & … @args } catch { [Console]::Error.WriteLine($_); exit 1 }; exit $LASTEXITCODE`.
- Test: `tests/test_packaging.py` (Windows-marked): `install.ps1 -Wheel` → exit ≠ 0.

## 2. First run shows the processor as "Intel64 Family 6 Model 170 Stepping 4, GenuineIntel"
- That's `platform.processor()` on Windows. The name is in the registry:
  `HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0\ProcessorNameString`
  ("Intel(R) Core(TM) Ultra 7 155H"), readable with `winreg`, no admin.
- Modify: `src/lectern/hardware/probe.py:92` (`cpu = platform.processor() or platform.machine()`).
  Test: a fake `winreg`.

## 3. The engine download shows no speed, so a slow one looks stuck
- First run: the model line shows "… 9 MB/s"; the engine line "185 MB of 577 MB (32%)" only. At
  0.5 MB/s from GitHub the bar barely moved and looked stuck (the user asked).
- Fix: the engine's progress carries a rate like the model's (`runners/llamacpp.py`
  `install_reporting`, the first-run page's engine row in `web/src`), and "no bytes for 30 s"
  says so.
- Test: `tests/runners/` (rate in the event), `web` test for the row.

<!-- postponed by /gku:fix, 2026-10-30 (the developer chose not to take it here) -->
4. [x] NIT `C:\Users\matas\Projects\lectern\packaging\windows\uninstall.ps1:9` (and install.ps1:29) —
   no `[CmdletBinding()]`, so an argument it doesn't know goes to `$args` unread:
   `uninstall.ps1 -Purgee` runs a plain uninstall. Add `[CmdletBinding()]` so it's refused
   (exit 1 through the root wrapper), with a test each.
 — **fixed in chore/m4-nit-cleanup**