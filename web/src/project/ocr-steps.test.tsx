import "../i18n";

import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { afterEach, describe, expect, it } from "vitest";

import type { RuntimeState } from "../api/types";
import { OcrSteps, ocrSteps, type OcrStepsInput } from "./ocr-steps";

const t = i18next.t.bind(i18next);
const base: OcrStepsInput = {
  status: "running", phase: null, pagesTotal: 3, read: 0, cleaned: 0, openQuestions: 0, reason: null,
  failedPages: [], waitingForChat: false, exports: ["export to DOCX"],
};
const at = (over: Partial<OcrStepsInput>, runtime?: RuntimeState) => {
  const { steps, detail } = ocrSteps({ ...base, ...over }, runtime, t);
  return [steps.map((s) => `${s.key}:${s.state}`).join(" "), detail];
};
const loading = (role: "main" | "ocr") =>
  ({ activity: { kind: "loading", role, model_id: "m" } }) as unknown as RuntimeState;

afterEach(cleanup);

describe("ocrSteps", () => {
  it("reads, then cleans, saying what it waits for", () => {
    expect(at({ waitingForChat: true })).toEqual([
      "read:current clean:later questions:later export:later", "starts after this reply",
    ]);
    expect(at({ phase: "recognise" }, loading("ocr"))[1]).toBe("loading the OCR model…");
    expect(at({ phase: "recognise", read: 1 })[1]).toBe("reading page 2 of 3");
    expect(at({ phase: "clean", read: 3 }, loading("main"))).toEqual([
      "read:done clean:current questions:later export:later",
      "loading the chat model to clean the text…",
    ]);
    expect(at({ phase: "assemble", read: 3, cleaned: 3 })[0]).toBe(
      "read:done clean:current questions:later export:later",
    );
  });

  it("stays at Your questions while the Document isn't final, never at the export or Done", () => {
    expect(at({ status: "done", openQuestions: 2 })).toEqual([
      "read:done clean:done questions:current export:later", "2 open questions",
    ]);
    expect(at({ status: "done", openQuestions: 1, failedPages: [2] })[1]).toBe(
      "1 open question · page 2 couldn't be cleaned",
    );
    expect(at({ status: "done", failedPages: [2, 3], exports: [] })).toEqual([
      "read:done clean:done questions:current done:later", "pages 2, 3 couldn't be cleaned",
    ]);
  });

  it("reaches the export once clear, or Done with nothing to export", () => {
    expect(at({ status: "done" })).toEqual([
      "read:done clean:done questions:done export:current", "running now",
    ]);
    expect(at({ status: "done", exports: [] })).toEqual([
      "read:done clean:done questions:done done:done", "",
    ]);
  });

  it("works out where a running job is from its pages, before any phase event", () => {
    expect(at({ read: 1 })[1]).toBe("reading page 2 of 3");
    expect(at({ read: 3, cleaned: 1 })).toEqual([
      "read:done clean:current questions:later export:later", "cleaning page 2 of 3",
    ]);
    expect(at({ status: "queued" })[1]).toBe("waiting to start");
  });

  it("says why it stopped where it stopped", () => {
    expect(at({ status: "paused", reason: "Paused" })[1]).toBe("paused · Paused");
    expect(at({ status: "failed", reason: "no model", phase: "recognise" })).toEqual([
      "read:current clean:later questions:later export:later", "stopped · no model",
    ]);
  });
});

describe("OcrSteps", () => {
  it("names the export step and marks the current one", () => {
    render(<OcrSteps input={{ ...base, status: "done", openQuestions: 2 }} runtime={undefined} />);
    const bar = screen.getByTestId("ocr-steps");
    expect(bar.textContent).toBe("✓Reading→✓Cleaning→Your questions· 2 open questions→Export to DOCX");
    expect(screen.getByText("Your questions").closest("li")?.getAttribute("aria-current")).toBe("step");
  });
});
