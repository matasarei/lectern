# M2b acceptance: the OCR session

M2b turns M2a's engine into OCR sessions: a PDF or image opened as a session, a background job that reads every page, and a tab to follow it. This file has two parts:
- **Part 1:** the engine side, which is the job, the API and sharing the GPU with the chat.
- **Part 2:** the tab in the page. It follows with the second PR.

## Part 1: the job and the API

### How to run it

```sh
uv run lectern serve
# in a project, import a PDF or image, then:
curl -X POST …/api/projects/<id>/ocr -d '{"artifact_id": <file id>}'   # the tab does this in part 2
```

To reproduce the swap check below on a large GPU, give the runtime a budget where the chat and OCR models don't fit together:

```sh
LECTERN_TIER=minimum LECTERN_BUDGET_MB=3500 uv run lectern serve
```

### Results

#### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 · llama.cpp b11236

Run on 2026-09-30 with the minimum tier's models:
- Qwen3.5-4B Q4_K_M for chat;
- PaddleOCR-VL 1.6 for `ocr`;
- Qwen3-VL 4B for `ocr.hand`.

The source was a 12-page PDF made from the four sample pages used in M2a.

**A whole batch:**
- `POST /ocr` answered 201 in 0.01 s, and the job ran in the background.
- Making the page images took about 1 s.
- PaddleOCR-VL then read the pages at 0.7 to 1.8 s each.
- All 12 pages ended `done`.
- The output folder holds `p001.png`, `p001.ocr.md`, `p001.paddleocr-vl-1.6.ocr.md` and `p001.paddleocr-vl-1.6.json` for every page.

**Chat during the batch, when both models fit** (a 4.5 GB budget):
- The chat message was answered in about 9 s.
- Both models stayed loaded, with no swaps, and OCR carried on at once.

**Chat during the batch, when they don't fit** (a 3.5 GB budget):

| Time | What happened |
|---|---|
| 7.8 s | A chat message is sent, after page 2. |
| 11.8 s | Page 3 finishes. The OCR model is unloaded and the chat model loaded (1 swap). |
| 15.5 s | The chat has answered. The OCR job's request waits, marked "waiting for the chat". |
| 135.3 s | The chat has been idle for 120 s. The chat model is unloaded and the OCR model loaded (1 swap). |
| 138.0 s | Page 4 done. The batch finishes with 2 swaps in total. |

- **The first version failed this:** the job checked for the chat itself before asking for the model. One chat message cost 4 swaps, because the job's waiting request took the GPU between two of the chat's model calls. The rule now lives in the runtime's admission, and a chat waiting for the GPU counts as active.

**Pause and Resume:**
- Pause was pressed while page 6 was being read. Page 6 finished, and the session showed "paused (Paused)".
- Resume read pages 7 to 12 without reading any page twice.

**Restart mid-batch:**
- The server was stopped with Ctrl+C after 3 pages and started again.
- The session showed "paused (Lectern restarted)", and no model was loaded, even after waiting.
- Resume finished the remaining pages, including the one that was being read when the server stopped.

**Handwritten pages and re-reading a page:**
- A page marked handwritten, with a note, was read again. It went to Qwen3-VL 4B (`ocr.hand`), and the note was kept.
- A page re-read with Qwen3-VL 8B took 7.7 s.
- Both earlier readings stay beside the new ones (`p002.paddleocr-vl-1.6.ocr.md`, `p002.qwen3-vl-8b.ocr.md`), and `p002.ocr.md` is the latest.

### Notes

- **Several sessions made from one file** get their own folders: `sample-12`, `sample-12 (2)` and so on.
- **Not run on the 4 GB and 8 GB PCs yet.** The swap check above uses a smaller budget on this Mac to stand in for them.
