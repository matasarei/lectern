# OCR review: Skip does nothing on a page's last question; a question that suggests the wrong word

**Type:** bug
**Asked:** (2026-10-29, M4 Windows run, 12-page Ukrainian scan) "skip does not work"
**Scope:** found on the Windows laptop; not Windows-specific. Merge into "Open now" on the Mac.

## 0. No way to say "it's right as read" (what was actually wanted)
The reading was correct. The person expected **Skip** to mean "not a mistake, leave it", and
had to press Edit and type the same word again to close the question.
- The card's actions are Accept <suggestion>, Edit, Skip; Skip only moves on ("later").
- The API takes `action: accept | edit` (`src/lectern/modules/ocr/api.py:59`): "keep" is
  possible today only as `edit` with the original text.
- The "Could be: вісімнадцять · вісімдесят" candidates are plain text (`review.tsx:254-255`).
- Wanted: a **Keep "<reading>"** action on the card (a key too), recorded as kept, not as an
  edit; and each candidate clickable to accept that one. Decide: is Skip renamed ("Later") so
  it isn't read as "ignore"? Does the export count a kept question as resolved (it should)?

## 1. Skip is a dead end on the page's last open question
- `web/src/project/review.tsx:82`: `skip = () => setCurrentId(after(current.id) ?? current.id)`.
  `after()` (`:55-60`) looks only at this page's open questions; with one left ("Question 1 of
  1") it returns null, so Skip stays put. Tab (`:96-98`) is the same.
- `nextPageWithQuestions(doc, page)` is already computed on `:83`; docs/ocr.md says Tab skips and
  the bar moves to the next page with questions.
- Wanted: Skip on the page's last open question goes to the next page with questions (its first
  open one), wrapping; with no other open question anywhere, it says so instead of nothing.
- Test: `review.test.tsx`: a page with one open question and another page with one: Skip opens
  the other page's; a document with one open question: Skip shows that it's the only one.

## 1b. A question with nothing to decide: "from → to" identical
Page 6 (`pages/p006.clean.json`): cleanup fixed the OCR typo (fix: вісімнадиять → вісімнадцять,
right) **and** raised a doubt on the fixed phrase whose `raw` is the corrected text and whose
only candidate equals it (`raw == candidates[0]`). The card shows the same text either side of
→ (`review.tsx:252`, `suggestion()` falls back to the first candidate when none differs), and
Accept changes nothing; the reason is the model's reasoning about the fix.
- Wanted: cleanup's checks (`clean.py`, where invalid doubts are already moved or dropped) drop
  a doubt with no candidate different from its `raw`; the fix stays listed. Until then the
  card shouldn't offer an Accept that changes nothing.
- Test: a cleanup reply with such a doubt yields no question; the fix is kept.

## 1c. Cleanup invents a fragment, and the review can't delete it
Page 7: the OCR reading (`p007.paddleocr-vl-1.6.ocr.md`) has "…комунальні послуги." once and
nothing else; cleanup returned an extra paragraph block "луги." (the word's tail again) and a
doubt on it, reasoning it came from the previous page, which ends with the table and "6".
- No way to remove it: the edit box disables Save on an empty value (`review.tsx:263, 277`) and
  the API requires `value` min_length 1 (`api.py:57`). The right answer, "delete this", is
  impossible; the workaround is a page note plus Clean again.
- Wanted: (a) cleanup's checks drop a block whose words aren't in the page's reading (the
  reverse of "lines left out are put back"), at least a short block that is the tail of the
  previous block's last word; (b) a **Remove** answer for a question on a whole short block (or
  an empty edit meaning "remove"), recorded as such.
- Test: a cleanup reply with an invented one-word block drops it; Remove on a block's doubt
  takes the block out of the Document and the export.

## 2. The question itself (cleanup quality, the 9B)
Page 3, "вісімнадцять тисяч" (correct in the scan):
- the card suggests **вісімдесят** (wrong), and **Accept вісімдесят** is the primary button, so
  Enter accepts a wrong reading;
- the reason is the model's own reasoning, in English, cut at 300 characters (`WHY_CHARS`):
  "…Wait, the next page says 'вісімнадять' … This is correct. N…". It concluded the word is
  right and raised the question anyway.
- Ideas: drop a doubt whose reason concludes the reading is right, or whose suggestion makes a
  number word disagree with the figure beside it ("18 500,00"); ask cleanup for the reason in
  the document's language and one sentence. Measure on the 12-page scan (m4 acceptance) first.

## Decided (2026-10-30)
- Split: **PR A, the review card** (0, 1, 1b's "no Accept that changes nothing", 1c(b) Remove);
  **PR B, cleanup's checks** (1b's drop of a no-op doubt, 1c(a) drop of an invented block).
  Part 2 moves to `ocr-cleanup-quality.md` (#11): it needs measuring on the 12-page scan.
- Skip is renamed **"Later"** (Tab still does it).
- "Delete this" is an explicit **Remove** button, offered only for a doubt covering a whole
  short block, recorded as `remove`; the block leaves the Document and the export.

<!-- postponed by /gku:fix, 2026-10-30 (PR A's review) -->
- [x] NIT `C:\Users\matas\Projects\lectern\web\src\project\ocr-api.ts:215` — `coversBlock`'s
  `[\p{L}\p{N}_]` is close to Python's `\w`, not identical; where they disagree the card offers
  Remove and the server answers 422 (shown). Mirror it exactly, or have the server send which
  doubts are removable.
 — **does not hold (2026-10-30): compared over all of Unicode, the two match on every character Python's tables assign; they differ only on ones newer than Python's tables**