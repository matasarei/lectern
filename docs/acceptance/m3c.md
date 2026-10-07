# M3c acceptance: the exporters, and the chat's OCR tools

M3c ends M3:
- **Part 1, the exporters:** Export · MD, Export · DOCX and Export · ODT, each its own module reading only a Document. The chat drives them with `export.formats` and `export.run`.
- **Part 2, the chat's OCR tools:** `ocr.start`, `ocr.note` and `task.after`. A follow-up runs when its OCR session finishes, so "convert this scan to ODT" works from one message.

## How to run it

```sh
LECTERN_TIER=minimum LECTERN_BUDGET_MB=3500 uv run lectern serve
# in a project with a scanned PDF, ask the chat: "Take lease-scan.pdf from the project and convert it to ODT."
```

## Results

### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 · llama.cpp b11236 · Chrome

Run on 2026-10-07 with the minimum tier's models:
- Qwen3.5-4B Q4_K_M for `main`;
- PaddleOCR-VL 1.6 for `ocr`.

LibreOffice isn't installed here, so the files were read back with odfdo and python-docx instead.

#### Part 1: exporting the real 12-page Document

This is M3b's sample: 186 blocks, 15 open questions, 7 tables and 20 figures.

| Format | Size | Time | Tables | Open questions |
|---|---|---|---|---|
| md | 8.3 KB | under 0.01 s | 7 | 15 inline marks |
| docx | 40.1 KB | 0.05 s | 7 | 15 comments |
| odt | 12.9 KB | 0.03 s | 7 | 15 comments |

- **The first export kept only 10 of the 15 open questions.** The other five were in invoice tables that cleanup had given both as text and as rows. M3a's assembly marked a question in the block's text first, but tables are shown from their rows. So those questions were invisible everywhere: in `document.md` for the chat, in the OCR tab, and in every export.
  - **Fixed in assembly** (`0d8c64e`): cells are tried first.
  - **And in the exporters** (`a9bf6e5`): a question a block doesn't show comes out right after it ("Not shown above: …"). That covers Documents assembled before the fix, such as this one.
  - **Now:** 15 of 15 in every format.
- **The chat, asked** "Export the sample-12 document to DOCX, with the open questions as comments", called `export_formats`, then `export_run(name="sample-12", format="docx", open_doubts="comment")`. It answered: "Saved as sample-12.docx … 15 open questions, as comments."

#### Part 2: "Take lease-scan.pdf from the project and convert it to ODT"

This is the 3-page Ukrainian lease from M3a, in a new project.

| Time | What happened |
|---|---|
| 0 s | The chat calls `ocr_start(name="lease-scan.pdf", then_export="odt")`. The card reads: "Start an OCR session for lease-scan.pdf? 3 pages · about 1 minute on this PC · opens in a new tab. The chat model is unloaded while pages are read, so replies wait until then. When it finishes: export to ODT." |
| — | Allowed. The OCR tab opens at once ("Waiting for the chat to be idle"). The chat says once that the export will follow. |
| ~120 s | The chat has been idle for two minutes. The OCR model loads, reads the 3 pages, and the main model cleans them. |
| 135 s | The session finishes. The follow-up runs: "OCR · lease-scan.pdf finished · 3 pages · 0 open questions." → `export_run(name="lease-scan/", format="odt")` → "Saved as lease-scan.odt, 10.4 KB". |
| 139 s | The model reports: "The file has been successfully converted and saved as `lease-scan.odt`." |

**`lease-scan.odt`, read back:**
- the table is one table across pages 1→2 (березень, квітень, травень, Разом);
- "заборгованості" is joined across the page break;
- the header and page numbers are gone;
- a heading and its paragraph that cleanup gave as one block are kept apart by a line break.

**The first run of this check found two bugs.** Both are fixed, each with a test that fails without it:
- **The tab opened as an empty chat** (`6f9f0ce`). The page's list of sessions didn't have the new OCR session yet; that list is read again only when the turn ends. A tab for something the page hasn't loaded now reads the items again and shows "Loading…" meanwhile.
- **The 4B called `task_after` seven times** with empty arguments after `ocr.start` had already queued the export, until the turn hit its call limit (`cda7947`).
  - The cause: `ocr.start`'s result said "After it finishes I'll export to ODT".
  - Now it says the export is queued and there's nothing more to do.
  - `task.after` answers "Already queued" for a follow-up that's already waiting.

The second run, quoted above, had neither problem. The browser console showed no errors.

### Not checked here

- **An open question as a comment, through `task.after`.** The lease came out with no open questions. Comments are checked by the shared exporter tests and by the DOCX export of the sample above.
- **`ocr.note` from the chat.** Tests cover it; the 4B wasn't asked to set a note.
- **Opening the files in LibreOffice or Word.** They aren't installed here.
