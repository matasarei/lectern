/** Test data for OCR sessions, shared by the OCR tests. A plain module: importing a test
 * file would run its tests again in the importer. */

import type { OcrPage, OcrSession } from "../api/types";

export const page = (number: number, over: Partial<OcrPage> = {}): OcrPage => ({
  number, key: `p${String(number).padStart(3, "0")}`, status: "pending", model_id: null,
  lease_key: null, handwritten: false, note: "", seconds: null, cut_off: false, looped: false,
  error: null, width: 2480, height: 3508, has_hint: false, clean_status: "pending",
  clean_error: null, ...over,
});

export const session = (over: Partial<OcrSession> = {}): OcrSession => ({
  id: 2, kind: "ocr", title: "lease-scan.pdf", folder: "lease-scan", status: "running",
  paused_reason: null, error: null, pages_total: 2, source_artifact_id: 5, document: null,
  updated_at: "",
  pages: [page(1), page(2)], ...over,
});
