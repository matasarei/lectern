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

Measured earlier in the plan (`.tasks/pdf-edit-in-place.md`): Qwen3.5-4B turned 13 requests
into exact edits 10 times out of 13. Its misses were a `find` one letter off, a wrong sum, and a
name transliterated. The tools' result for the chat loop is in the section below, once step 6
has run.
