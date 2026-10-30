# OCR sessions and Documents

An OCR session turns a PDF or a page image into a **Document**: text the chat can search and
quote, and that exports to Markdown, DOCX or ODT. The code is in `src/lectern/modules/ocr/`
and `src/lectern/documents/`.

## Starting one

Choose **Open as OCR session** on a PDF or an image in Overview, or ask the chat
(`ocr.start`, which asks first with a card: the page count, about 15 seconds a page, any model
to download, and whether the chat's model is unloaded meanwhile). The session won't start
while its OCR or cleanup model isn't downloaded. A PDF that already has a text layer is
refused by `ocr.start` unless `keep_layout` is set: its text can be read directly.

A project runs one OCR job at a time; the others wait in line. A session can be paused,
resumed, or started ahead of the queue. After a restart it shows as paused ("Lectern
restarted"); Resume carries on from the first page that isn't done.

## How a session runs

The job goes through the whole document one phase at a time (`jobs.py`):

1. **Rasterise.** Every PDF page becomes a 300 dpi PNG through pypdfium2; a page over
   50 megapixels is rendered at a lower resolution. A document may have up to 1000 pages.
   An image is turned upright from its EXIF data, flattened to RGB or grey, and saved as a
   PNG; a multi-page TIFF gives a page per frame (`pages.py`).
2. **Recognise.** The `ocr` model reads one page image at a time, scaled to the model's
   `max_side`, and returns Markdown with HTML tables. A page marked **Handwritten** goes to
   `ocr.hand` instead. Where the runner returns token log-probabilities they are kept; a word
   with a token below −3.0 is marked unsure (`doubts.py`). A reading that ran into the token
   limit is flagged *cut off*; one where a run of 12 words appears twice is flagged *repeats
   itself* (`projects/text.py`).
3. **Clean.** The `main` model, or the one chosen for the `clean` role, gets the raw page,
   the page note, the person's confirmed readings (newest first, at most 30) and 300
   characters from each neighbouring page's edge. It answers in JSON held to a schema
   (`clean.py`): blocks (heading, paragraph, table, list, signature, stamp, figure), the
   fixes it made with the reason for each, and doubts with candidate readings and a reason.
   An unsure OCR word that cleanup left unchanged becomes a doubt of its own. An invalid
   reply is retried once. Code then checks the reply: blocks copied from a neighbouring page
   are dropped, and so is a one-word block that only repeats the end of the word before it
   ("луги." after "послуги.") and isn't a word of the page; lines left out are put back,
   Latin and Cyrillic look-alike letters are restored, a doubt in the wrong block is moved,
   and a doubt whose only candidate is its own text (nothing to decide) is dropped. Each
   drop is listed as a fix. When a cut-off reading ends in a loop,
   the loop is cut before cleanup and listed as a fix that suggests re-running the page.
4. **Assemble.** Code only, no model (`assemble.py`). A short line (under 100 characters) at
   the top or bottom of three pages or more is a running header or footer and is dropped,
   along with bare page numbers. A word hyphenated across a page break is joined, a
   paragraph that runs onto the next page is joined, and a table that continues is merged,
   without its repeated header row. A page note saying "don't merge" (or "do not merge",
   "never merge", "не об'єднувати") keeps that page's first block apart.

The model is leased a page at a time and given back between pages, so a chat can get the GPU
after the current page. Once the chat has been idle for two minutes the job takes it back.
On a card where the OCR model and the cleanup model don't fit together that still means one
swap per phase, not two per page ([Models and memory](models.md#leases-and-swapping)).

## Working on pages

The OCR tab has the page list on the left (30%) and the page on the right (70%), in four
views: Split, Scan, Text and Raw. A page in the list shows its flags: repeats itself, cut
off, handwritten, has a note.

- **Re-run** reads a page again, with its usual model or another OCR model (one not
  downloaded is shown greyed out). In a paused session only that page is read.
- **Clean again** cleans a page again without reading it.
- **Page notes** are free text, up to 5000 characters, written in the OCR tab or through the
  chat (`ocr.note`). A note goes into that page's cleanup prompt; saving one on a page that's
  already cleaned offers *Clean this page again with this note*. The system prompt tells the
  chat that notes come first when it builds documents and exports.

## Review

What cleanup and OCR weren't sure of becomes a question. The question bar shows how many are
open on the page and in the document, and moves to the next page with questions. Each
question card shows the reading, the suggestion (the first candidate that differs from the
raw text), the reason, and whether it came from cleanup or OCR. Its answers:

- **Accept** the suggestion, or click any other candidate to accept that one. There is no
  Accept when no candidate differs from the reading: it would change nothing.
- **Keep** the reading as it is, when it was right: recorded as kept, not as an edit.
- **Edit** it to what the page says.
- **Remove**, offered only when the question is all of its block (a fragment cleanup made up):
  the block stays in `document.json`, struck out in the review so the answer can be taken
  back, and is left out of `document.md`, the Document tab and the exports.
- **Later** moves on without answering: to the page's next open question, and after its last
  to the next page with questions. On the document's only open question it says so.

With focus in the review, **Enter** accepts the suggestion (or keeps the reading when there is
none), **K** keeps, **E** edits, **Tab** is Later, and **Escape** cancels an edit. A resolved
question is shown underlined with the original as a tooltip, and its chip (*kept*, *removed*,
or the reading it became) reopens it.

Answers are stored apart from the Document (`ocr_decisions`) and applied every time it is
assembled, so they survive cleaning a page again. An answer whose text no longer appears,
because the page was cleaned differently, is shown as *unplaced*. Confirmed readings (not
removals) also go into later cleanups of the same session.

## The Document

A finished session publishes a folder at the project's top level, named after the source
file (made unique when that name is taken):

```
lease-1998-scan/
├── document.json          the Document: the source of truth
├── document.md            generated from it, for the chat to read
└── pages/
    ├── p001.png           the page image
    ├── p001.ocr.md        the reading in use
    ├── p001.<model>.ocr.md   each model's reading
    ├── p001.<model>.json  its tokens and log-probabilities, timings, cut-off
    └── p001.clean.json    the page's cleanup
```

Every answer, reopened question or new assembly publishes a new version with the same block
ids, so earlier citations still point at the right place. Only the latest version is kept.

### document.json

Schema `lectern.document/1` (`documents/document.py`):

```json
{
  "schema": "lectern.document/1",
  "title": "lease-1998-scan",
  "version": 7,
  "source": {"artifact": "lease-1998-scan.pdf", "pages": 12},
  "pages": [
    {"n": 3, "image": "pages/p003.png", "size": [2480, 3508],
     "note": "Keep the payment table as it is.", "blocks": ["b3.1", "b3.2"], "failed": null}
  ],
  "blocks": [
    {"id": "b3.2", "type": "paragraph", "page": 3, "pages": null, "bbox": null,
     "level": null, "text": "Оплата здійснюється не пізніше {d3.1} числа …", "rows": null,
     "label": null,
     "doubts": [{"id": "d3.1", "raw": "1?", "status": "open", "candidates": ["10", "15"],
                 "reason": "…", "source": "clean", "value": null, "resolved_by": null}]}
  ]
}
```

- Block ids are `b<page>.<n>`, doubt ids `d<page>.<n>`; a doubt sits in its block's text as
  `{d3.1}`. A block that continues onto the next page lists every page in `pages`.
- A table's `rows` start with the header row. Headings have a `level` from 1 to 3; figures
  and stamps have a `label`.
- A page that couldn't be cleaned has the reason in `failed`.
- `bbox` is always null: there is no layout model, so figures have no position and nothing
  is cut out of the scan.

### document.md

What the chat reads. Every block starts (or, for a heading, ends) with an anchor it can cite:

```markdown
---
document: lease-1998-scan · version 7
source: lease-1998-scan.pdf · 12 pages
open_questions: 1
---
> Note for page 3 (from you): Keep the payment table as it is.

## 3. Орендна плата {#b3.1 p=3}

{#b3.2 p=3} Оплата здійснюється не пізніше ⟦1? | open | 10 · 15⟧ числа …

{#b3.3 p=3→4} …

{#b3.4 p=3 figure} [Image 1: stamp · no cut-out yet]
```

An open doubt is `⟦raw | open | candidates⟧`; a resolved one is just its value. Tables and
lists have their anchor on a line of its own (`{#b3.5 p=3 table}`), and a page that wasn't
cleaned reads `{#p3 p=3} [Page 3 wasn't cleaned: …]`. For search and reading, document.md is
cut into one piece per block and one per note (`#noteN`), so the chat loads only what it
needs (`document.outline`, `project.search`, `document.read`: see [The chat](agent.md)).

## Exports

Each format is a module in `src/lectern/modules/`, and each takes a Document:

| Module | Format | Built with | Capabilities |
|---|---|---|---|
| `export_md` | `md` | own code | tables |
| `export_docx` | `docx` | python-docx | tables, comments |
| `export_odt` | `odt` | odfdo | tables, comments |

Options (`documents/export.py`):

- `open_doubts`: `comment` (the default where the format has comments) or `inline`, which
  prints `⟦raw → a · b⟧` in the text. Questions whose mark isn't printed follow their block as
  "Not shown above: …".
- `notes`: `omit` (the default) or `comment`. DOCX and ODT attach a page's note as a comment
  on its first block; Markdown prints it as a quote.

A figure is always printed as `[Image N: label, page P]`. Before an export, Lectern works out
what the format would lose: the chat then asks with a card, and the Export buttons answer with
*Export anyway*. The exported file is always new, `<title>.<ext>` at the project's top level.

Other files export too: Markdown (headings, paragraphs, lists, pipe tables), plain text, a
PDF's text layer and DOCX are turned into a Document first (`documents/from_text.py`). For a
PDF with a text layer, the chat warns that tables come out as plain lines, and that an OCR
session with `keep_layout` keeps them.

Export buttons are on a finished OCR session, in the Document tab and under a chat reply. The
chat lists the installed formats with `export.formats` before offering one, and `ocr.start`
can take `then_export`: the export runs when the session finishes, and is held, with the chat
told, while questions are open or a page failed.

Every exporter passes the same test document (`tests/documents/fixtures.py`): a table across
two pages, open and resolved questions, notes, and a figure.
