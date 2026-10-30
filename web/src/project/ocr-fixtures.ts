/** Test data for OCR sessions, shared by the OCR tests. A plain module: importing a test
 * file would run its tests again in the importer. */

import type { DocBlock, DocDoubt, OcrDocument, OcrPage, OcrSession } from "../api/types";

export const page = (number: number, over: Partial<OcrPage> = {}): OcrPage => ({
  number, key: `p${String(number).padStart(3, "0")}`, status: "pending", model_id: null,
  lease_key: null, handwritten: false, note: "", seconds: null, cut_off: false, looped: false,
  error: null, width: 2480, height: 3508, has_hint: false, clean_status: "pending",
  clean_error: null, step_started_at: null, ...over,
});

export const session = (over: Partial<OcrSession> = {}): OcrSession => ({
  id: 2, kind: "ocr", title: "lease-scan.pdf", folder: "lease-scan", status: "running",
  paused_reason: null, error: null, pages_total: 2, source_artifact_id: 5, chat_id: null, document: null,
  updated_at: "",
  pages: [page(1), page(2)], ...over,
});

export const doubt = (id: string, raw: string, over: Partial<DocDoubt> = {}): DocDoubt => ({
  id, raw, status: "open", candidates: [], reason: "", source: "clean", value: null,
  resolved_by: null, ...over,
});

export const block = (id: string, text: string, over: Partial<DocBlock> = {}): DocBlock => ({
  id, type: "paragraph", page: Number(id.slice(1).split(".")[0]), pages: null, level: null,
  text, rows: null, label: null, doubts: [], ...over,
});

/** Pages 1-3: page 1 has two questions, page 2 none, page 3 one (already answered: none). */
export const documentOf = (blocks: DocBlock[], pages = 3): OcrDocument => ({
  schema: "lectern.document/1", title: "lease-scan", version: 1,
  source: { artifact: "lease-scan.pdf", pages },
  pages: Array.from({ length: pages }, (_, i) => ({
    n: i + 1, image: `pages/p00${i + 1}.png`, size: [2480, 3508] as [number, number], note: "",
    blocks: blocks.filter((b) => b.page === i + 1 || b.pages?.includes(i + 1)).map((b) => b.id),
    failed: null,
  })),
  blocks, unplaced: [],
});
