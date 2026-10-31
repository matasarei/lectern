# A script that prints nothing passes its test run, and the chat's own arithmetic reads as computed

**Type:** bug
**Language:** en
**Asked:** user: "for me it looks like the answer can rot the model. did you check that it actually
calcualted it?" (M4 Windows run, 2026-10-29)
**Scope:** all platforms

## Summary
- `code_write` passes a test run on exit 0 alone. Twice in this run the saved script was two
  comments (`# -*- coding: utf-8 -*-`, `# Сума: 855.232`), printed nothing, "passed", and `code.run`
  returned "(no output)"; the chat then gave the number itself, after "Ran … (exit 0)", so it read
  as the sandbox's result.
- Fix: a test run with no output (when the brief asks for printed output) fails, with a fix
  prompt; `expected_output` is checked loosely; a "(no output)" run is said plainly to the person.

## Cause
- `src/lectern/modules/sandbox/tools.py:437`: `if result.ok:` → saved; stdout not looked at.
- `expected_output` is only text in the coder's request (`:531-532`) and the manifest (`:596`).
- `:237`: `code.run` gives the model "(no output)" and the model answers anyway.
- M1b (Mac) already noted "The test run only catches scripts that crash".

## Design
- In `_write_and_test`, after `result.ok`: empty stdout and no output files → `problem = "The
  script printed nothing. Print the result."`, another fix round.
- With `expected_output`: a cheap shape check (non-empty; if it names a label like "Сума:", the
  label is in stdout); no exact match (the coder can't know the number).
- The chat prompt rule: a `code.run` result "(no output)" means not computed; say so. And the UI
  marks a run with no output on the `code.run` line.

## Acceptance criteria
- [ ] A coder reply that's only comments fails the test run and gets a fix round; after
      `MAX_FIXES` it's "couldn't write a working script", not saved.
- [ ] A script whose output lacks the brief's label gets a fix round.
- [ ] The chat never shows a number after a "(no output)" run as if computed (tool-calling set
      case).

## Steps
1. The no-output and label checks.
   - Modify: `src/lectern/modules/sandbox/tools.py:437-450`
   - Test: `tests/modules/test_sandbox_tools.py` (fake box returning ok with empty stdout)
2. A tool-calling test case: "Скільки становить 23% від 3 718,40 грн? Обчисли скриптом…" —
   the answer must come from stdout.
   - Modify: the chat's test sets (`docs/acceptance/m1c.md` names them)
3. Docs: `docs/agent.md` (the script sandbox: what a test run checks).

<!-- postponed by /gku:fix, 2026-10-30 -->
4. [x] NIT `C:\Users\matas\Projects\lectern\docs\acceptance\m4.md:379` — step 6 is still ✗ with no
   "**Fixed**" note like the other fixed items; add it once the live case has been run. — **already resolved: the **Fixed** paragraph under the table records the live rerun, as rows 5 and 7 are marked**

## How to check it
- `uv run pytest tests/modules -q`; the tool-calling set.

## Do not touch
- The sandbox's isolation (`modules/sandbox/container.py`).

## Evidence
- **Data:** `scripts/viprocent.py` and `scripts/percent-calc.py` in the M4 project: comments only;
  `scripts/percentage-calc.py` (English prompt): computes. Lesson: a test prompt mustn't contain
  the answer (the first Ukrainian prompt did).
