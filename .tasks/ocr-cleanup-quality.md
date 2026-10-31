# OCR on a 12-page Ukrainian lease: what cleanup and the chat got wrong

**Type:** bug
**Language:** en
**Asked:** "write all issues to the tasks to fix them" (M4 Windows run, 2026-10-29)
**Scope:** not Windows-specific: PaddleOCR-VL 1.6 reading, Qwen3.5-9B cleanup and chat. Measure
before changing: the 12-page scan is `.tasks/m4-windows-evidence/m4-lease-scan.pdf`, made by
`make_scan.py` beside it (its text is the reference); worth adding to `lectern dev ocr-check`'s
samples.

## Seen
1. **Every table transposed.** The scan's rows are months (Місяць | Сума, грн | Термін оплати);
   the Document's columns are months (`| Місяць | Січень | Лютий | … |`), on pages 3, 6, 9, 12.
   Values right. Check whether PaddleOCR's reading (`pages/p003.paddleocr-vl-1.6.ocr.md`) or
   cleanup transposed it.
2. **"Розділ" read as "Розділі"** on pages 3, 4, 7, 10, 11 by PaddleOCR; cleanup neither fixed nor
   questioned it.
3. **A page's heading dropped:** page 11's "11. Розділі 11" is missing from the Document.
4. **The running header only partly removed:** "Договір оренди № 17/2026" (every page) kept 3
   times, merged with the heading into one block on pages 4, 7, 10 (assembly drops a short line
   only when it's its own block).
5. **Questions with nothing to decide / invented blocks / no keep-or-remove:** in
   `ocr-review-skip.md`.
6. **The chat dropped `then_export`:** "Read the scan and prepare DOCX from it." → `ocr.start`
   without `then_export: docx`; nothing exported until the Export button (`ocr-questions-link.md`).
7. **The chat called OCR "оптимізація коду"** twice in its reply (the 9B mis-describing the
   tool): a wording nudge in the OCR tool's description or the system prompt.

## Acceptance criteria
- [ ] The 12-page scan in the OCR check set; its Document has tables in the scan's orientation,
      every heading, no running header, "Розділ".
- [ ] A tool-calling case: the request above queues `then_export: docx`.

## Steps
1. Add the scan to the samples, with a reference text.
2. Find where 1 and 3 happen (reading vs cleanup vs assembly); fix there.
   - Look: `modules/ocr/clean.py` (prompt, checks), `modules/ocr/assemble.py` (header rule)
3. The header rule: also strip a known running header at the start of a block.
   - Modify: `assemble.py`. Test: `tests/modules/ocr/test_assemble.py`
4. Tool-calling set case for `then_export`.

## Evidence
- `~/.lectern/data/users/local/projects/0c01aeab-…/m4-lease-scan/document.md` (on the Windows
  laptop); the scan's source text is in `.tasks/m4-windows-evidence/make_scan.py`.

## Found (2026-10-30, from the M4 session's own files on the laptop)
- Tables: PaddleOCR-VL's reading lists a table column by column as plain lines; cleanup made
  each column a row. Not cleanup's transposition.
- Page 11's heading: cleanup fixed "Розділі"→"Розділ", then `keep_this_page` dropped the block as
  "on a neighbouring page": the corrected word isn't in this page's reading, but is on page 12's
  edge. Our bug.
- Header on 4, 7, 10: cleanup merged header + heading into one block; assembly drops the header
  only as its own block.
- "Розділі": OCR misread; cleanup fixed it on page 11 only.

## Decided (2026-10-30)
- Tables: a code check after cleanup turns round a table whose rows are each one kind of value
  and whose columns mix kinds; listed as a fix.
- PR 1: the heading (corrected words count as on the page) + the header at a block's start.
  PR 2: the tables. Later, with the GPU: "Розділі", the chat's then_export and wording, #9 part 2.

- Found in review of fix/ocr-headings-on-repeat (2026-10-31, already on main): assembly drops a single-line block repeated, digits aside, at the top of 3+ pages, so a scan with no running header whose pages each start with "N. Розділ N" loses those headings (`assemble.py`, `_drop_running_lines`).
