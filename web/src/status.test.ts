import "./i18n";

import i18next from "i18next";
import { describe, expect, it } from "vitest";

import type { RuntimeState } from "./api/types";
import { ocrStepText, type OcrStepInput } from "./status";

const t = i18next.t.bind(i18next);
const base: OcrStepInput = {
  status: "running", phase: null, pagesTotal: 3, read: 0, cleaned: 0, openQuestions: 0, reason: null,
};
const loading = (role: "main" | "ocr") =>
  ({ activity: { kind: "loading", role, model_id: "m" } }) as unknown as RuntimeState;
const step = (over: Partial<OcrStepInput>, runtime?: RuntimeState, waiting = false) =>
  ocrStepText({ ...base, ...over }, waiting, runtime, t);

describe("ocrStepText", () => {
  it("walks through a session's steps, saying when a model loads in between", () => {
    expect(step({ status: "queued" })).toBe("waiting to start");
    expect(step({ phase: "rasterise" })).toBe("preparing the pages");
    expect(step({ phase: "recognise" }, loading("ocr"))).toBe("loading the OCR model…");
    expect(step({ phase: "recognise", read: 1 })).toBe("reading page 2 of 3");
    expect(step({ phase: "clean", read: 3 }, loading("main"))).toBe(
      "loading the chat model to clean the text…",
    );
    expect(step({ phase: "clean", read: 3, cleaned: 2 })).toBe("cleaning page 3 of 3");
    expect(step({ phase: "assemble" })).toBe("putting the document together");
  });

  it("doesn't count past the last page", () => {
    expect(step({ phase: "recognise", read: 3 })).toBe("reading page 3 of 3");
  });

  it("ignores the chat's own model loading while pages are read", () => {
    expect(step({ phase: "recognise" }, loading("main"))).toBe("reading page 1 of 3");
  });

  it("says done with pages and open questions, or why it stopped", () => {
    expect(step({ status: "done", pagesTotal: 1 })).toBe("done · 1 page");
    expect(step({ status: "done", openQuestions: 2 })).toBe("done · 3 pages · 2 open questions");
    expect(step({ status: "paused" })).toBe("paused");
    expect(step({ status: "paused", reason: "the chat needed the memory" })).toBe(
      "paused · the chat needed the memory",
    );
    expect(step({ status: "failed", reason: "no model" })).toBe("stopped · no model");
  });

  it("leaves a wait for the chat to HoldNote", () => {
    expect(step({ phase: "recognise" }, undefined, true)).toBeNull();
    expect(step({ status: "done" }, undefined, true)).toBe("done · 3 pages"); // finished wins
  });
});
