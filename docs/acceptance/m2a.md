# M2a acceptance: the OCR engine and the model-scoring test

This covers the first half of the blueprint's M2: page images, recognition through the `ocr` and `ocr.hand` models, and a test that scores candidate OCR models on your own pages. The OCR session and its tab are M2b.

## How to run it

```sh
uv sync
# Download the OCR models first: Settings → Models & memory, or lectern serve and the models API.
uv run lectern dev ocr-check ~/path/to/scans --draft paddleocr-vl-1.6   # draft references
# correct reference/<page>.txt by hand, then:
uv run lectern dev ocr-check ~/path/to/scans
```

The folder holds the scans (PDFs and images), `reference/<page>.txt`, and an optional `tags.yaml` listing the kind of each page. A page is named after its file: `invoice` for an image, `lease-p003` for page 3 of `lease.pdf`. The check writes only under `<folder>/.ocr-check/`, plus `reference/` with `--draft`, which never overwrites. It refuses to run while `lectern serve` is up, because a server would share the GPU.

**The scores:**
- **Words missed:** reference words not in the output.
- **Words extra:** output words not in the reference, which is how loops and made-up text show up.

  Both are counted in any order, because each model picks its own reading order for tables and side-by-side blocks. CER is shown beside them, and it does count order.
- **Looped:** pages where some run of 12 words appears twice.

## Results

### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 · llama.cpp b11236

Run on 2026-09-30 with the `comfortable` tier's runtime. Models: PaddleOCR-VL 1.6 (F16), Qwen3-VL 4B and 8B Instruct (Q4_K_M), Chandra OCR 2 (Q4_K_M). Each model ran alone, in one phase, with no swaps.

#### A generated page of known text

The real-model test (`pytest -m gpu tests/modules/ocr`) uses an A4 page at 300 dpi with 12 pt Ukrainian and Latin text.

| Model | CER | Prompt tokens (at `max_side`) | Output tokens | Time |
|---|---|---|---|---|
| PaddleOCR-VL 1.6 | 1.5% | 1,273 (2048 px) | 233 | 1.7 s |
| Qwen3-VL 4B | 0.0% | 1,167 (1280 px) | 230 | 3.6 s |
| Qwen3-VL 8B | 1.2% | 1,167 (1280 px) | 224 | 5.4 s |
| Chandra OCR 2 | 0.0% | 1,782 (1600 px) | 627 | 8.8 s |

- **Clean Cyrillic and Latin text:** all four read it well.
- **Context:** every prompt used under half of the 4k context, and nothing was cut off. The catalog's `max_side` values stay as they are.

#### Four sample pages

The pages:
- three invoice templates with a table, form fields and signatures (one at low resolution, with a rotated sidebar and a script font);
- one scanned scanner test sheet: small text, rotated text, colour charts, and letters cropped at the page edge.

All four are Latin-script. The pages and their corrected text are kept outside the repository.

| Model | Words missed | Words extra | CER | Looped | s/page | Memory |
|---|---|---|---|---|---|---|
| PaddleOCR-VL 1.6 | 11.3% | 2.4% | 29.1% | 0 | 1.4 | 1.1 GB |
| Qwen3-VL 4B | 13.9% | 29.8% | 56.3% | 1 | 4.0 | 3.9 GB |
| Qwen3-VL 8B | 4.0% | 3.5% | 18.4% | 0 | 6.6 | 6.3 GB |
| Chandra OCR 2 | 4.0% | 12.9% | 20.8% | 0 | 13.6 | 3.7 GB |

What the numbers are made of:
- **PaddleOCR-VL:**
  - It is the fastest and smallest, and reads the invoices' main text almost completely.
  - Its misses are text running vertically in a sidebar, and the small print inside a logo.
  - It reads tables column by column, which is why its CER is high although the words are right.
- **Qwen3-VL 4B:**
  - It read the scanner sheet correctly, then repeated it three times (98% extra words on that page).
  - It also dropped most of a page set in widely spaced capitals (46% missed).
- **Qwen3-VL 8B:** the most accurate overall, with no loops. It needs 6.3 GB.
- **Chandra OCR 2:**
  - It reads as well as the 8B, but describes images in words (logos, signatures), which counts as extra.
  - It is the slowest, at 13.6 s a page.
  - Its licence restricts commercial use, so it can't be a default.

### Defaults: unchanged, and why

The tier defaults in `models.yaml` stay as they are: PaddleOCR-VL for `ocr`, Qwen3-VL 4B or 8B for `ocr.hand`. These four pages are Latin-script, printed and mostly clean. They can't show how the models handle what your documents mostly are: Cyrillic text, handwriting and poor scans. Changing a default on them would be a guess.

Two findings carry into M2b regardless:
- **Loops happen.** Qwen3-VL 4B, the minimum tier's handwriting model, went round a page three times. The session has to notice a looping page and flag it for re-running, instead of keeping the repeated text.
- **Some models reason first.** Chandra's reply was empty until `recognise` asked it not to reason (`enable_thinking: false`). Any new OCR model should be checked for this.

## Not done yet
- **Your real pages:** Cyrillic print, handwriting and poor scans. The same command scores them once they're in the folder with corrected references. That run decides the defaults.
- **The 4 GB and 8 GB PCs:** not measured. The timings above are an M4 Max with every model on the GPU.
