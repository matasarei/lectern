# Windows: two models loaded at once run a 16 GB PC to the commit limit

**Type:** bug
**Language:** en
**Asked:** "please check how RAM is utilised on this device…" and "write all issues to the tasks
to fix them" (M4 Windows run, 2026-10-29)
**Scope:** the runtime planner (`src/lectern/runtime/`), Windows; worth a look on Linux too

## Summary
- During OCR cleanup PaddleOCR-VL stayed loaded beside the 9B (they fit the 8 GB card: 7.8 of
  8.2 GB); PaddleOCR was still loaded 30+ minutes later. Windows (WDDM) backs GPU allocations with
  system commit: the two llama-servers had 3.3 + 8.3 GB private, while the 9B's weights in RAM
  were 0.5 GB. With an IDE, Docker Desktop and browsers: 0.3 GB RAM free, commit 28.8 of 29.2 GB.
- The planner budgets VRAM and RAM, not commit. Fix: on Windows, count a model's VRAM toward
  commit too, and unload an idle model (the reader after its phase) when commit is short.

## Cause (to confirm in the planner)
- `runtime/planner.py` budgets (`budget`, `resident`, `gpu_share`, ~118-147) — VRAM-centred.
- Models stay resident until something else needs room (the reader after reading).
- Measured: `llama-server` PaddleOCR private 3.25 GB (CUDA0 688 MiB), 9B private 8.28 GB (CUDA0
  4861 MiB, CPU_Mapped 546 MiB), both `--fit on`, 19/19 and 33/33 layers on the GPU.

## Design
- Read free commit on Windows (`GlobalMemoryStatusEx`: `ullAvailPageFile`) beside free RAM.
- A model's commit cost on Windows ≈ its VRAM + host buffers; admit a model only if commit
  stays above a margin (say 2 GB); otherwise unload the least-recently-used idle model first.
- After an OCR job's reading phase ends, the reader is idle: a candidate for unloading at once
  when commit is short.

## Acceptance criteria
- [ ] On a Windows PC with commit < margin + the next model's cost, the idle reader is unloaded
      before cleanup loads (log line says why).
- [ ] With plenty of commit, behaviour unchanged (both stay, no swap).
- [ ] The Models & memory page shows commit on Windows, or the budget accounts for it.

## Steps
1. Windows commit probe.
   - Modify: `src/lectern/hardware/probe.py`
   - Test: `tests/hardware/` (fake numbers)
2. Commit in admission and eviction.
   - Modify: `src/lectern/runtime/planner.py`, `supervisor.py` (`_try_admit`)
   - Test: `tests/runtime/test_planner.py`: two models that fit VRAM but not commit
3. Docs: `docs/models.md` (memory and leases).

<!-- postponed by /gku:fix, 2026-10-30 -->
4. [x] NIT `C:\Users\matas\Projects\lectern\tests\hardware\test_probe.py` —
   `test_windows_reports_the_commit_left` only bounds the number (`<= RAM + swap + 64 GiB`);
   compare it with WMI's `Win32_OperatingSystem.FreeVirtualMemory` within 1 GB to check it. — **fixed in chore/m4-nit-cleanup**

## How to check it
- `uv run pytest tests/runtime tests/hardware -q`; a 12-page OCR on the 8 GB laptop with the usual
  apps open: commit stays below 90 %.

## Do not touch
- macOS unified-memory budgeting; the 8 GB tier's choice of models.

## Evidence
- **Data:** snapshot 16:0x during cleanup (m4.md, Windows results); `nvidia-smi` 7788/8188 MiB;
  page file then shrank (commit limit 23.8 GB at 17:45).
