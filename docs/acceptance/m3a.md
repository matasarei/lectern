# M3a acceptance: cleanup, doubts and the Document

M3a adds the last two phases of an OCR session. Once every page is read, the job:
- cleans each page with the `main` model into blocks, fixes and doubts;
- assembles the pages into one Document, `document.json` plus `document.md`;
- publishes `document.md` as a project file that the chat can search, read and cite by block (`#b3.2`).

## How to run it

```sh
LECTERN_TIER=minimum LECTERN_BUDGET_MB=3500 uv run lectern serve
# in a project, import a PDF, then open it as an OCR session (the OCR tab, or):
curl -X POST …/api/projects/<id>/ocr -d '{"artifact_id": <file id>}'
curl …/api/projects/<id>/ocr/<session>/document   # once the session is done
```

The 3.5 GB budget is so the chat and OCR models don't fit together, as on the minimum tier's GPU.

## Results

### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 · llama.cpp b11236

Run on 2026-10-01 with the minimum tier's models:
- Qwen3.5-4B Q4_K_M for `main`, which does the cleanup;
- PaddleOCR-VL 1.6 for `ocr`.

There were two inputs:
- **The 12-page sample from M2b.** It's the four sample pages used in M2a, three times over: three invoices and the scanner diagnostic sheet.
- **A made-up 3-page lease, rendered to scan-like pages.** It's in Ukrainian and has:
  - a running header and "стор. N з 3" page numbers;
  - a table continued from page 1 onto page 2;
  - the word "заборго-/ваності" hyphenated across the page 2–3 break;
  - one smudged digit.

  It's blurred, slightly rotated and noisy. It's made by a throwaway script (not in the repo) that also writes the printed text of each page as the truth.

#### Timing and swaps

| | Lease, 3 pages | Sample, 12 pages |
|---|---|---|
| Reading (PaddleOCR-VL) | 1.4–1.6 s a page | 0.7–1.8 s a page |
| Cleanup (Qwen3.5-4B) | 3.6–9.1 s a page, 18 s in all | 2.8–29 s a page, 140 s in all (about 12 s a page) |
| Assembly | under 0.1 s | under 1 s |
| Swaps | 1 (ocr → main) | 1 (ocr → main) |
| Pages failed | 0 | 0 |

Each document cost one swap. The job loads the OCR model to read every page, then the main model to clean them all.

#### The lease's Document

`document.md` matches the printed text:
- the header and the page numbers are gone;
- the table is one block, `{#b1.5 p=1→2 table}`, with the repeated header row dropped;
- "заборгованості" is joined in `{#b2.4 p=2→3}`;
- the Cyrillic is right throughout.

The smudged "10" was read correctly, so there are no open questions. One miss: the model typed "1. Сторони" and the paragraph under it as one paragraph, so the Document has no headings.

#### Doubts on the sample, and `LOW_CONFIDENCE`

The sample has three words PaddleOCR-VL misread (`4id` for "Mid", `Jiniformity`, `I1l`). Against the reference texts, the unsure-word threshold scored like this:

| `LOW_CONFIDENCE` | Words flagged | Misread among them |
|---|---|---|
| −0.5 | 18 | 3 |
| −1.0 (the first value) | 6 | 0 |
| −2.0 | 2 | 0 |
| −3.0 | 0 | 0 |

At −1.0 the marks were pure noise:
- **Every flagged word was read right:** Bill, UNIT, Channel, Siemens…
- **Each one was doubted twice:** once by cleanup, which echoed the mark, and again as an OCR doubt.

The misreads themselves sat between −1.0 and −0.5, among many correct words. Cleanup doubted all three on its own.

`LOW_CONFIDENCE` is now **−3.0**. It flags nothing on these pages, and keeps marking a word the OCR model was far less sure of (a bad scan, handwriting). This rests on one sample with three misreads, all in English. It needs checking against Cyrillic, handwritten or poor scans.

The final run had **10 open doubts**, all from cleanup, each with candidates and a reason:
- `4id` → mid / lid (three times, on the three copies of the scanner sheet);
- `Jiniformity Bands` → Uniformity Bands;
- `2312/2019` and `26/02/2019` (twice each);
- `11.02.2030` → 11.02.2020;
- `Borcele Bank` → Borcelle Bank.

All of them are genuine questions about the page. The last three are printed exactly that way on the invoices, so a person confirms them once in M3b's review.

#### The chat, answering from the Document

The question asked in Ukrainian was "Яку пеню сплачує орендар за прострочення платежу?" (what penalty does the tenant pay for a late payment). The chat answered in 5 s:

> Згідно з договором (блок `lease-scan/document.md #b2.4`), орендар сплачує пеню у розмірі **0,5% від суми заборгованості за кожен день прострочення**, включно з днем оплати.

- `project.search` for "пеню" returns `lease-scan/document.md #b2.4 — Page 2` with that sentence.
- The model's own searches ("пеня", "пеня прострочення") found nothing, because search matches words, not stems. It then read the Document with `document.read` and found the block there.
- The same question in English was answered from `#b2.4` too, in 6.6 s. Its wording was slightly off ("from the day after the due date").

### What the real run found, and what was fixed

The first runs failed in ways the unit tests hadn't foreseen. Each one is fixed, with a test that fails without the fix:

1. **A table came out empty.** The model gave its text instead of rows. The text is now parsed into rows.
2. **Cleanup copied a neighbouring page's text into this page.** On the sample, a page's reply sometimes held most of the next page. Blocks that are mostly another page's words are left out and listed as fixes.
3. **A page number was typed as a signature or stamp.** It's now dropped whatever its type.
4. **A line of the page was left out.** On the lease it was the top line of page 3 ("ваності…", "belongs to the previous page"), so the hyphenated word was never joined. On the sample it was lines such as "Please make checks payable to…".
   - A line with fewer than half its words in the blocks is put back where it was.
   - Pages now keep 96–100% of their OCR words. What's missing is what cleanup corrected.
5. **Latin letters in Cyrillic words** (`tравень`). They're set back to the OCR reading.
6. **A misplaced doubt failed the whole page.** This hit 8 of 12 sample pages: the doubt named the wrong block, or kept its ⟨word?⟩ mark. A doubt now moves to the block that holds its text, and one that no block holds is dropped and listed.
7. **Everything else, smaller:**
   - a fix whose reason held the model's whole reasoning;
   - a doubt listing the same reading three times;
   - an OCR doubt duplicating cleanup's;
   - every block of a heading-less Document cited as "About this document" (now "Page 2").

### Not checked

- **The OCR tab in a browser.** Web tests cover the Clean and Assemble phases, the Raw view, the highlighted doubts and "Open the document" against a fake server.
- **Cyrillic, handwritten and poor scans from real use.** The lease is synthetic and clean enough that it raised no doubt.
- **Headings.** The 4B types few. A block without a heading above it is filed under its page.
