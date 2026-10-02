# M3b acceptance: reviewing the Document

M3b lets the person answer the Document's open questions. Each answer:
- publishes a new version of the Document, with the same block ids;
- survives the page being cleaned again;
- goes into the cleanup of pages cleaned later.

This file has two parts:
- **Part 1:** reviewing in the OCR tab.
- **Part 2:** the Document in Overview. It follows with the second PR.

## Part 1: reviewing in the OCR tab

### How to run it

```sh
LECTERN_TIER=minimum LECTERN_BUDGET_MB=3500 uv run lectern serve
# open a PDF as an OCR session; when it's done, open its tab and select a page with questions
```

### Results

#### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 · llama.cpp b11236 · Chrome

Run on 2026-10-02 with the minimum tier's models:
- Qwen3.5-4B Q4_K_M for `main`;
- PaddleOCR-VL 1.6 for `ocr`.

The source was the 12-page sample from M2b and M3a. The session finished in 166 s with one swap, and the Document had 10 open questions.

**The OCR tab:**
- A cleaned page is drawn from the Document's blocks: tables as tables, figures as placeholders.
- Page 1 had no questions, and its bar read "Page 1: 0 questions · document: 10". "Next page with questions ›" went to page 4.
- **Page 4:** the question card sat under the bullet holding `4id`. It showed "4id → mid", the other candidates (4id · mid · lid · did), the cleanup's reason, and Accept / Edit / Skip.
  - The review had focus, so **E** opened the edit box at once. The scan says "Mid", so it was corrected to that, and Enter saved it.
  - The strip then read "Open the document (version 2) · 9 open questions". The bullet showed "Mid", underlined, and the panel showed "4id → Mid · Reopen".
- **Page 5:** both questions are dates printed as they are on the invoice.
  - **Tab** skipped to the second question ("Question 2 of 2"), and nothing was saved.
  - **Enter** accepted it, and **Enter** again accepted the first.
  - The result was version 4, with 7 open questions.
- **The note on page 5:**
  - Saving it offered "Clean this page again with this note".
  - Pressing it cleaned only page 5 ("Clean 11/12", then back to 12/12), and assembled version 5.
  - **Both decisions on page 5 were still applied in version 5.**
- The browser console showed no errors.

**The chat:**
- The project search for "Mid" returns `sample-12/document.md #b4.13` as "- Mid", the answered reading from version 5. The same line on pages 8 and 12, which weren't answered, still shows `⟦4id | open | …⟧`. A decision applies only to its own page.
- Asked about the uniformity bands, the 4B didn't reach the answer, in two tries:
  - **First:** it searched for words the sheet doesn't contain, then opened tabs.
  - **Second:** told the file's name, it read the wrong pages' headings.

  The tools return the answered text, as above. The 4B finding its way through a 188-block Document is a chat question, not M3b's.

### What the check found

- **Cleaning page 5 again gave a worse cleanup than the first time:**
  - lines from the next page at the top ("100 Fifth Avenue, New York, NY 10023", "DESCRIPTION OF ITEM", "PAYMENT DETAILS");
  - 8 new doubts on plain numbers (10023, 100.00, 15.00…).

  Each copied line has exactly half its words on page 5, so the neighbouring-page check, which needs fewer than half, keeps it. That's M3a's cleanup heuristic, which this plan doesn't touch; it needs its own fix.

  Whether the confirmed readings in the message made the model doubt more numbers can't be told from one run.
- **A heading that holds an open question becomes the section name of the blocks under it:** "⟦Jiniformity Bands | open | …⟧". Answering it fixes the name.

### Not checked in a browser

- **Reopen.** Web tests and API tests cover it.
- **A decision whose text is gone after a page is read again.** Its "unplaced" listing is tested in the API and the job; the tab doesn't show it yet.

## Part 2: the Document in Overview

### How to run it

The same server as part 1. Open the project's Overview after an OCR session has finished.

### Results

#### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 · Chrome

Run on 2026-10-02 against the same project as part 1, with the server restarted on part 2's code.

- **Overview:**
  - The session's output folder is one row: "sample-12/ · OCR output · folder · document · 12 pages · 20 image regions · made by OCR · sample-12.pdf". The 20 image regions are the scanner sheet's figure blocks, three copies of it.
  - Expanded, it shows "sample-12 · Document · version 5 · 15 open · inside sample-12/".
- **The Document row opened "Document · sample-12" in a new tab:**
  - the whole text, with the open questions marked and "Mid" shown as answered;
  - the side panel: version 5, 12 pages, 15 open questions, the source file and the OCR session as links, page 5's note, and Download.
- **Clicking the open question "Jiniformity Bands"** opened the OCR tab at page 12, with that question's card current ("Question 1 of 2").
- The browser console showed no errors.

### Not checked in a browser

- **A folder that isn't an OCR session's,** such as `scripts/`. Web tests cover it.
