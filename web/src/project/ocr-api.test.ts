import { describe, expect, it } from "vitest";

import type { OcrPage, OcrSession } from "../api/types";
import { applyOcrEvent, applyToLive } from "./ocr-api";

export const page = (number: number, over: Partial<OcrPage> = {}): OcrPage => ({
  number, key: `p${String(number).padStart(3, "0")}`, status: "pending", model_id: null,
  lease_key: null, handwritten: false, note: "", seconds: null, cut_off: false, looped: false,
  error: null, width: 2480, height: 3508, has_hint: false, ...over,
});

export const session = (over: Partial<OcrSession> = {}): OcrSession => ({
  id: 2, kind: "ocr", title: "lease-scan.pdf", folder: "lease-scan", status: "running",
  paused_reason: null, error: null, pages_total: 2, source_artifact_id: 5, updated_at: "",
  pages: [page(1), page(2)], ...over,
});

describe("applyOcrEvent", () => {
  it("replaces a page by its number", () => {
    const done = page(2, { status: "done", model_id: "paddleocr-vl-1.6", seconds: 1.5 });
    const next = applyOcrEvent(session(), { seq: 5, type: "page", ...done });
    expect(next.pages[1]).toEqual(done);
    expect(next.pages[0].status).toBe("pending");
  });

  it("adds pages it didn't know, in order", () => {
    const next = applyOcrEvent(session({ pages: [], pages_total: 0 }), { seq: 1, type: "page", ...page(2) });
    const after = applyOcrEvent(next, { seq: 2, type: "page", ...page(1) });
    expect(after.pages.map((p) => p.number)).toEqual([1, 2]);
    expect(after.pages_total).toBe(2);
  });

  it("takes the session's status and reason", () => {
    const next = applyOcrEvent(session(), {
      seq: 9, type: "session", status: "paused", reason: "Lectern restarted", error: null,
    });
    expect([next.status, next.paused_reason]).toEqual(["paused", "Lectern restarted"]);
  });
});

describe("applyToLive", () => {
  const idle = { phase: null, waitingForChat: false } as const;

  it("follows the phase, and waiting for the chat until the next page", () => {
    let live = applyToLive(idle, { seq: 1, type: "phase", phase: "recognise" });
    live = applyToLive(live, { seq: 2, type: "waiting", reason: "chat" });
    expect(live).toEqual({ phase: "recognise", waitingForChat: true });
    live = applyToLive(live, { seq: 3, type: "page", ...page(3, { status: "recognising" }) });
    expect(live.waitingForChat).toBe(true); // still waiting until the page is read
    live = applyToLive(live, { seq: 4, type: "page", ...page(3, { status: "done" }) });
    expect(live.waitingForChat).toBe(false);
  });

  it("clears when the job stops", () => {
    const live = applyToLive({ phase: "recognise", waitingForChat: true }, {
      seq: 5, type: "session", status: "paused", reason: "Paused", error: null,
    });
    expect(live).toEqual(idle);
  });
});
