# OCR steps line: "N open questions" opens the review at the first one

**Type:** feature
**Asked:** (2026-10-29, M4 Windows run) whould be nice to have a direct link to the tab, where the
questions are waiting, so click on the "8 pending questions" -> open exacly where they are waiting for
**Scope:** written on the Windows laptop; merge into "Open now" in `lectern-ui-followups.md` on the Mac.

## Now
- The chat's OCR line (`web/src/project/ocr-line.tsx`, `Line`) links only the session's name:
  `onOpen({ kind: "session", id })` opens the OCR tab at its default page and view.
- "Your questions · 8 open questions" (`ocr-steps.tsx`) is plain text.
- The review already marks each doubt in the page (`review.tsx`, `data-doubt={doubt.id}`), and
  the OCR tab takes a page (`ocr.tsx`, `onSelectPage`), so the pieces to land on one exist.

## Wanted
- "N open questions" is a link (while N > 0) that opens the session's tab on the page of the
  first open question, in the view that shows the question bar, with that question selected.
- The same from the Overview's session row and the OCR tab's own status strip, if they show the
  count.

## And the way back, when the last question is answered
(Same run.) After the last answer the OCR tab's bar reads only "Page 9: 0 questions · document:
0"; the strip at the top turns to "✓ Done". Nothing says what happens next, and the chat that
started the session still shows its notice "…isn't finished yet: 12 pages read · 8 open
questions" (a notice-only follow-up, taken once, never updated).
- Wanted: on the last answer, the bar says all questions are answered and links back to the
  chat that started it ("Back to the chat"), or says the queued export is running; the chat's
  stale notice is replaced by the session's current state.
- In this run nothing was queued: the 9B called `ocr.start` without `then_export` although the
  person asked for a DOCX (the 4B on the Mac did pass it, m3c). Worth a tool-calling test case:
  "Read the scan and prepare DOCX from it." must queue `then_export: docx`.

## Open
- How the tab is told where to land: a field on `Tab` (`{ kind: "session", id, at: "questions" }`)
  or the first open doubt's id; and whether it survives a reload (the tab list is stored).
- "First" = lowest page, then block order (`d<page>.<n>`), or newest?

## Decided (2026-10-30)
- "First" = lowest page, then the order on the page (as the review walks them).
- The tab is told through `Tab.page` plus a review hint and the question's id, never saved
  (like `page`): a reload opens it as usual.
- PR 1: the link. PR 2: the way back after the last answer and the chat's stale notice. The
  `then_export` tool-calling case moves to `ocr-cleanup-quality.md` (#11 has it already).

<!-- postponed by /gku:fix, 2026-10-30 (PR 1's review) -->
- [ ] NIT `C:\Users\matas\Projects\lectern\web\src\project\ocr.tsx:61` — from the tab's own
  steps bar, already on the first question's page but at another question there, the link
  changes nothing: pass the first question's id down to `ReviewPage` to select it.

## Decided for PR 2 (2026-10-30)
- The session records the chat that started it: `ocr_sessions.chat_id`, nullable (migration
  p0011), set by `ocr.start` from its chat; none when started from the Files tab, or for older
  sessions (no link then).
- The chat's notice stays as written; under it, the session's live steps line shows where it
  stands now (as under `ocr.start`).

<!-- postponed by /gku:fix, 2026-10-30 (PR 2's review) -->
- [ ] NIT `C:\Users\matas\Projects\lectern\web\src\project\chat.tsx:424` — a chat that started a
  session shows its live steps line twice (under `ocr.start` and under the notice). By design
  for now; show it only under the latest message about that session if it reads repetitive.
