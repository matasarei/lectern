# PDF editing: choosing the engine

Run on 2026-11-07/08 on the developer's Mac (Apple Silicon, macOS, pypdfium2 5.13.0 with PDFium
153.0.7999.0), for the `pdf-edit` module. The question was which engine changes a PDF's text
while keeping its fonts, sizes, colours and positions: pdf-edit-engine 0.2.0 (MIT, on pikepdf
and fontTools), or our own on PDFium, which Lectern already uses to read PDFs.

**Verdict: our own engine on PDFium, with fontTools to subset a font it has to add.**
pdf-edit-engine did 9 of 23 edits not at all, and broke the layout of pages where it
made them. Its report said `font_preserved=True` for edits whose pages render broken. The PDFium
prototype's misses all came from its own line-finding code, which the module replaces with
PDFium's text page.

## The test PDFs

Every PDF was generated for this run; none comes from anyone's files.

| File | Made by | What it tests |
|---|---|---|
| c1 contract | Chrome (Skia) | Times subset fonts, bold run split by kerning, a Cyrillic paragraph, a table |
| c2 two columns | Chrome | Georgia, justified two-column text, Verdana heading |
| c3 statement | Chrome | Helvetica 8.5 pt table, right-aligned amounts, Cyrillic |
| c4 Open Sans | Chrome, `@font-face` from a file | a font not installed on the machine, one text object per glyph |
| c5 notice | macOS `cupsfilter` (Quartz) | one text object per line, Monaco |
| c7 form | written by hand (as in the tests) | two text fields and a checkbox |
| c8 order | Chrome | Arial, all Ukrainian |

Not covered: PDFs from Word or LibreOffice (neither runs here; ONLYOFFICE's converter needs its
font cache), scans, rotated text, Type 3 fonts.

## Results

Edits that came out right, judged by rendering each page and looking at it, not by the
engine's report:

| PDF | Edits | pdf-edit-engine 0.2.0 | PDFium prototype |
|---|---|---|---|
| c1 contract | 6 | 6 in the text, but the page is broken: "Lingua Pro LLC" drawn over "between", the first line and the signature drawn larger, a gap in the Cyrillic line | 6, looks like the original; 2.5 MB (each edit embedded the whole Times font again) |
| c2 two columns | 4 | 2 (a name across a line break and the heading not done) | 2 (the same two not found) |
| c3 statement | 3 | 1: the Cyrillic name and description refused (`font_extension_failed`), amount squeezed | 3, but the shorter description pulled the next table cells left |
| c4 Open Sans | 3 | 0, and no file written | 0: the bold name refused (font not installed), the others not found by the line code |
| c5 notice | 3 | 1: "Jovanović" refused (ć), the date not done (`multi_match_same_operator_unsupported`) | 3; "February" overlapped the next word |
| c8 order | 4 | 4 in the text, every one squeezed with `Tz` and the lines left with gaps | 3, looks like the original; one not found (object order after an edit) |
| c7 form | 3 fields | text values only through `/NeedAppearances` (shown by viewers that regenerate fields); checkbox not ticked | — (built in the module) |

**What the prototype got wrong, and the fix in the module:**
- **Lines were built from text objects in drawing order, joined by gaps.** That missed text in
  per-glyph PDFs (c4) and after an edit (c8). The module reads lines from PDFium's text page,
  where every character knows its text object (`FPDFText_GetTextObject`).
- **The rest of the line was shifted across table cells** (c3). The module shifts only the run
  of text that touches the edit, and stops at a gap wider than a space or two.
- **The edited object was re-texted in place**, and Quartz positions each glyph, so new text
  overlapped (c5). The module draws a fresh text object in the same font instead.
- **The whole system font was embedded** (+490 KB per edit). The module subsets it to the
  letters needed with fontTools.

**Why pdf-edit-engine was not chosen:** it didn't make 9 of 23 edits and damaged the page
in two of the PDFs where it did, and its report doesn't match the result (`font_preserved=True` on c1 and c8, both broken on the page). It's also a
4-month-old project with one maintainer. Its approach of squeezing letter spacing (`Tz`) to fit
the old width is the visible damage in c1 and c8. Its source makes no network calls and starts
no processes (checked with grep, 16,067 lines).

## The language model

Before the tools existed, Qwen3.5-4B turned 13 requests into exact edits 10 times out of 13,
given the agreement's lines numbered in its prompt (`.tasks/pdf-edit-in-place.md`). Through
the chat, with `pdf.edit` and `pdf.fill`, it has to find and read the PDF itself:
`lectern dev agent-check --set pdf`, on 2026-11-08, Qwen3.5-4B Q4_K_M (`/api/runtime`
residents: `qwen3.5-4b`), `LECTERN_TIER=minimum` (context 8,192), llama.cpp b11236, Docker
running (so `code_run` was there), cards answered Allow.

| Run | Tool wording | Agreement edits (13) | Forms (2) | Wrong total written (of 2) |
|---|---|---|---|---|
| 1 | first | 5 | 2 | 2 ("1,330", 1,250 + 75 → "1,475") |
| 2 | first | 8 | 2 | 2 |
| 3 | read first, totals from `code_run`; slips answered, not applied | 9 | 2 | 2 ("1,580", "1,275") |
| 4 | the same | 10 | 2 | 0 |

**Target: at least 10 of 13, and no sum written unchecked. Met in one run of the two on the
final wording.**

What changed between runs 2 and 3, each from a turn seen failing:

- **Blind guesses.** In runs 1–2 the model often called `pdf.edit` without reading the PDF,
  guessing `find`: "Client" became "Petro Bondarenko" ("the Petro Bondarenko"), "Contractor"
  was replaced everywhere, the new name was sent as `find`. The description now says to read
  the PDF first and copy the words that are there now; most turns of runs 3–4 start with
  `document_read`.
- **A slip applied.** "місто Київ" → "місто Львів" matched "місті Київ" and wrote "у місто
  Львів" into the document. A near match is now answered with the exact text instead.
- **A clause as one line.** "Add a confidentiality clause" became a `replace` with line breaks
  and 300 characters, 1,783 pt past the margin, saved because the card was answered Allow. A
  `replace` with a line break is now refused; run 4's model then gave up without saving.

Still failing on the final wording:

- **Sums.** The description asks for a new total from `code_run`; the 4B called it in none of
  the four runs and wrote a wrong total in six of the eight turns that needed one. The system prompt's
  exact-results rule covers sums, and the loop's check for a sum from memory looks only at
  replies made without a tool, so it never sees a total inside `pdf.edit`'s arguments.
- **"Payment is in three equal instalments instead of two"**: searched for, never edited, in
  all four runs (twice it hit the 8-call limit).
- **"Rename the contractor"** sometimes renames the word "Contractor" rather than the
  company the agreement names as contractor.

Forms filled right in every run.
