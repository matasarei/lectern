# M4 Windows run (2026-10-29): the tasks, in fix order

Results: `docs/acceptance/m4.md`, "Results: RTX 4060 Laptop, Core Ultra 7 155H, 16 GB, Windows 11".
Evidence kept in `m4-windows-evidence/` (the 12-page scan and its generator, the stdin probe,
pytest's Windows output). All of `.tasks/` is git-ignored: copy to the Mac's "Open now".

| # | Task | Platform | Severity | Why this place |
|---|---|---|---|---|
| 1 | `windows-tray-freeze.md` | Windows | blocker | **done: #132, merged 2026-10-29** (3 nits left in the file) |
| 2 | `windows-listener-closes.md` | Windows | high | **done: #133, merged 2026-10-29** (1 nit left in the file) |
| 3 | `agent-turn-dies-silently.md` | all | high | **done: #134, merged 2026-10-29** |
| 4 | `text-encoding-utf8.md` | Windows (non-tray runs, tests) | high | **done: #135, merged 2026-10-29** (1 review nit open) |
| 5 | `sandbox-no-output-passes.md` | all | high | **done: #138, merged 2026-10-30** (follow-up: `expected_output` asks for the shape, not values) |
| 6 | `windows-ci-and-tests.md` | Windows | medium | **done: #136, merged 2026-10-30** (3 review nits left in the file) |
| 7 | `windows-memory-two-models.md` | Windows | medium | **done: #139, merged 2026-10-30** (1 nit left in the file) |
| 8 | `windows-install-dir-shared-paths.md` | Windows | medium | **done: #140, merged 2026-10-30** (1 nit left in the file) |
| 9 | `ocr-review-skip.md` | all | medium | **review card: #142, merged 2026-10-30**; cleanup's checks: **#143, merged 2026-10-30** (part 2 moved to #11; 1 nit left in the file) |
| 10 | `ocr-questions-link.md` | all | low | **done: #145 and #147, merged 2026-10-30** (2 nits left in the file) |
| 11 | `ocr-cleanup-quality.md` | all | low | **PR 1 (heading, header): #148; PR 2 (tables): #149, both merged 2026-10-31; PR 3 (headings lost on the re-run) + tray record: draft #150**; open: "Розділі", then_export miss, question-card quality, iGPU memory, re-run results not yet in m4.md |
| 12 | `windows-small-fixes.md` | Windows | low | **done: #141, merged 2026-10-30** (1 nit left in the file: `[CmdletBinding()]`) |

1–2 and 4 can only be proven on Windows: do them on the laptop, or land 6's Windows CI job first.
Still by hand on Windows (m4.md "Not run here"): the tray menu, light/dark, Start menu, sign-in
start, End task, the DOCX in Word.
