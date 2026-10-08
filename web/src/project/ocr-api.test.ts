import { describe, expect, it } from "vitest";

import {
  applyOcrEvent,
  applyToLive,
  nextPageWithQuestions,
  pageBlocks,
  pageQuestions,
  suggestion,
} from "./ocr-api";
import { block, doubt, documentOf, page, session } from "./ocr-fixtures";

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
  const idle = { phase: null, waitingForChat: false, resumeIn: null, waitingSince: 0 } as const;

  it("follows the phase, and the wait for the chat until the page's model is the job's", () => {
    let live = applyToLive(idle, { seq: 1, type: "phase", phase: "recognise" });
    live = applyToLive(live, { seq: 2, type: "waiting", reason: "chat", resume_in: 100 });
    expect([live.phase, live.waitingForChat, live.resumeIn]).toEqual(["recognise", true, 100]);
    expect(live.waitingSince).toBeGreaterThan(0); // the countdown counts from here
    // The page is marked "recognising" only once the job has the model: the wait is over.
    // (It used to be marked before the wait, and this test kept the wait shown through it.)
    live = applyToLive(live, { seq: 3, type: "page", ...page(3, { status: "recognising" }) });
    expect(live.waitingForChat).toBe(false);
  });

  it("waits for the chat's reply when there's no countdown", () => {
    const live = applyToLive(idle, { seq: 2, type: "waiting", reason: "chat", resume_in: null });
    expect([live.waitingForChat, live.resumeIn]).toEqual([true, null]);
  });

  it("clears when the job stops", () => {
    const live = applyToLive({ ...idle, phase: "recognise", waitingForChat: true }, {
      seq: 5, type: "session", status: "paused", reason: "Paused", error: null,
    });
    expect(live).toEqual(idle);
  });

  it("records a new document version from its event", () => {
    const next = applyOcrEvent(session({ document: { version: 1, artifact_id: 40, open_doubts: 2 } }), {
      seq: 7, type: "document", version: 2, artifact_id: 40,
    });
    expect(next.document).toEqual({ version: 2, artifact_id: 40, open_doubts: 2 });
  });
});

describe("the Document's questions", () => {
  const doc = documentOf([
    block("b1.1", "Оплата до {d1.1} числа, {d2.1}", {
      pages: [1, 2],
      doubts: [doubt("d1.1", "1?", { candidates: ["1?", "15"] }), doubt("d2.1", "Хр?щатик")],
    }),
    block("b2.1", "Без питань."),
    block("b3.1", "{d3.1}", { doubts: [doubt("d3.1", "2?0,00", { status: "resolved", value: "250,00" })] }),
  ]);

  it("suggests the first candidate that differs from the page, else the page's text", () => {
    expect(suggestion(doubt("d1.1", "1?", { candidates: ["1?", "15", "18"] }))).toBe("15");
    expect(suggestion(doubt("d1.1", "2312/2019", { candidates: ["2312/2019"] }))).toBe("2312/2019");
    expect(suggestion(doubt("d1.1", "4id"))).toBe("4id");
  });

  it("puts a question on the page it came from, even in a block that started earlier", () => {
    expect(pageQuestions(doc, 1).map((d) => d.id)).toEqual(["d1.1"]);
    expect(pageQuestions(doc, 2).map((d) => d.id)).toEqual(["d2.1"]);
    expect(pageBlocks(doc, 2).map((b) => b.id)).toEqual(["b1.1", "b2.1"]);
  });

  it("finds the next page with open questions, wrapping round, and none when all are answered", () => {
    expect(nextPageWithQuestions(doc, 1)).toBe(2);
    expect(nextPageWithQuestions(doc, 2)).toBe(1); // page 3's is answered
    expect(nextPageWithQuestions(documentOf([block("b1.1", "x")]), 1)).toBeNull();
  });
});
