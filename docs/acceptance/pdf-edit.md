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

### The call check (2026-11-08)

The exact-results check now also asks before a call writes a number nobody gave
(`docs/agent.md`, "Exact answers"). The same pdf set on the 4B, Docker running:

| Run | Call-check wording | Agreement (13) | Forms (2) | Sum turns through `code_run` | Wrong total written |
|---|---|---|---|---|---|
| 5–6 | "If any is an exact result…" | 11, 8 | 2, 2 | 0 of 4 (each check answered NO_CALC) | 3 |
| 7–8 | "you worked it out yourself… call code_write now" | 9, 10 | 2, 2 | 4 of 4 | 1 ("2,580", from the script) |
| 9–10 | the same, quoting what the call would write | 10, 10 | 2, 2 | 3 of 4 (run 9's birth certificate computed, then claimed an edit it didn't make) | 0 |
| 11–12 | the same, quoting the call's arguments; the dropped call held, a note quoting it on each later model call, a guard before the turn ends without it | 11, 9 | 2, 2 | 3 of 4 (run 12's birth certificate wrote the script, never ran it) | 2 (birth certificate: 1,350.00 after the script printed 1325.00; 1,275.00) |
| 13–14 | the same, the note and the guard with the worked-out number blanked | 9, 11 | 2, 2 | 4 of 4 | 1 (run 13's price and total: 1,380.00, below) |

Replayed directly on the 4B, each of the 11 sum turns twice: the first wording computed 1 of
22, the second and third 22 of 22. The third computed a clause's "5" needlessly 1 time in 8.
`calc` set after the change, twice: 19 and 20 of 23 passed, `code.run` used in 22 and 21 of 23.
At its last record in [`m1b.md`](m1b.md) the set had 22 prompts (20, 20 and 22 passed;
`code.run` in 22 of 22 each run); it has 23 now, so the counts aren't directly comparable.
Here `code.run` was skipped in 1 and 2 prompts.

#### The held call (2026-11-08)

After computing, the model re-sent the dropped call with only the total: the birth certificate
lost its price edit in all 4 turns of runs 7–10, and in run 9 it said "I have updated the
text" with no call. So the dropped call is now held for the rest of the turn
(`docs/agent.md`, "Exact answers"). The first note named the draft's number and quoted the
call with it in. In runs 11–12 the birth certificate still lost its price edit, and its total
was wrong twice, once with the script's 1325.00 just above. The draft isn't stored, so it's
unconfirmed that the model copied the number from the note, but blanking that number fixed
it:

- **Replayed directly** (6 birth-certificate turns, the model's own draft, the recorded
  `code_write`/`code_run`, twice each): with the blanked note, the call kept both edits with
  the script's 1,325.00 10 times in 12. The other 2 called `code_run`, which that turn still
  needed. Without the note: the total only 8 times, both edits 2 times. The first note was
  replayed only on runs 7–10's 4 turns, twice each: where the model's draft had both edits,
  it kept both 4 times in 4. In the other two turns the draft itself had only the total.
- **Runs 13–14:** the birth certificate kept both edits with 1,325.00 in both runs. No turn in
  runs 11–14 claimed an edit it didn't make.
- **Price and total, run 13:** the one wrong total was this turn's. It computed from a
  document it hadn't read ("Diploma price: EUR 1,200.00"), its edit failed, and it read the
  PDF and wrote 1,380.00, worked out in its head. The call check had already asked that turn,
  and it asks only once.
- **Other cases:** the failures (instalments in every run, a Ukrainian city edit stopped after
  8 calls, contractor or client failing on `find`) were in turns where no call was held.

## Cyrillic in filled fields (2026-11-12)

`pdf.fill` types a value through PDFium's form environment, and PDFium draws it. For letters
the field's own font (`/Helv`, WinAnsi) can't draw, PDFium adds a TrueType "Arial" in the
cp1251 layout, but doesn't embed it. The agent-check form's "name" filled with
"Євген Матасар, вул. Ґонти 5" gives `pdffonts`: `Arial TrueType Custom emb no`. poppler draws
it right with this Mac's fonts, as Courier squeezed into Arial's widths with only Courier New,
and draws nothing with no fonts at all.

PDFium can't be pointed at an embedded font. An embedded Cyrillic font in the form's `/DR`,
named in the field's `/DA`, didn't change what it drew: it still added its own Arial. That
held for a cp1251 simple TrueType and for a Type0 Identity-H font with a ToUnicode CMap.

What works is completing PDFium's font afterwards, with pypdf 7.0.1:
- **Embedding:** pypdf adds ArialMT, cut to the letters in the value, as `/FontFile2` of the
  font PDFium wrote. `pdffonts` then shows `ArialMT TrueType Custom emb yes`. The field shows the
  text in poppler with no fonts, in PDFium, and in macOS Quick Look.
- **Incremental write:** pypdf appends to PDFium's output and leaves its bytes as they were.
  PDFium's output had neither an xref stream nor object streams.
- **Encrypted forms:** a form with only an owner password (RC4-128) is filled, and PDFium keeps
  it encrypted. pypdf doesn't write incremental updates to encrypted files, and AES needs the
  `cryptography` package, which isn't installed. So an encrypted form is filled with the font
  not embedded, and the result says so.

As built (`pdf_edit/embed.py`, on `fill_form`), checked with the agent-check form's "name" set
to "Євген Матасар, вул. Ґонти 5":
- **This Mac:** PDFium names Arial, and the fill embeds ArialMT (`pdffonts`:
  `TTEQRQ+ArialMT TrueType Custom emb yes`). The file is 34,417 bytes, against 10,428 from
  PDFium alone. poppler with no fonts shows the text. A Latin-only fill is unchanged:
  4,918 bytes, Helvetica only.
- **Linux** (`python:3.12-slim` in Docker, only `fonts-dejavu-core`): PDFium names
  "DejaVuSansMono,Bold", which isn't the PostScript name of any installed file. So the
  fallback embeds DejaVu Sans with its own widths (`FKCYSY+DejaVuSans … emb yes`, 28,717
  bytes), and poppler there shows the text.
- **`agent-check --set pdf` on the 4B** (residents qwen3.5-4b and qwen2.5-coder-1.5b): 14/15
  passed, forms 2/2. The one failure was `instalments`, as in every earlier run. The set's
  form values are Latin, so this run shows nothing regressed rather than testing the new
  embedding.
