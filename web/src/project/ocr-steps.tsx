import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import type { RuntimeState } from "../api/types";
import { ocrStepText, type OcrStepInput } from "../status";

/** Where an OCR session is, as the whole: what's done, what's going on, what's still to come. */
export interface OcrStepsInput extends OcrStepInput {
  failedPages: number[]; // read or cleanup failed: the Document isn't final
  waitingForChat: boolean;
  exports: string[]; // what waits for the Document to be clear: "export to DOCX"
}

const TICK = "✓"; // a done step's mark, not words

type StepKey = "read" | "clean" | "questions" | "export" | "done";
export interface Step {
  key: StepKey;
  state: "done" | "current" | "later";
}

/** The steps — Reading, Cleaning, Your questions, then the export (or Done) — which one is
 * current, and the detail for it. It never reaches the last step while a question is open or
 * a page isn't cleaned: the Document isn't final until then. */
export function ocrSteps(
  s: OcrStepsInput,
  runtime: RuntimeState | undefined,
  t: TFunction,
): { steps: Step[]; detail: string } {
  const last: StepKey = s.exports.length > 0 ? "export" : "done";
  const keys: StepKey[] = ["read", "clean", "questions", last];
  const finished = s.status === "done";
  const clear = finished && s.openQuestions === 0 && s.failedPages.length === 0;
  let current: StepKey;
  let detail: string;
  if (clear) {
    current = last;
    detail = last === "export" ? t("ocrSteps.exporting") : "";
  } else if (finished) {
    current = "questions";
    detail = questionsDetail(s, t);
  } else {
    // At work, waiting, paused or stopped: the phase it's in, with today's wording
    current = s.phase === "clean" || s.phase === "assemble" ? "clean" : "read";
    if (!s.phase && s.read >= s.pagesTotal && s.pagesTotal > 0) current = "clean"; // paused while cleaning
    detail = s.waitingForChat
      ? t("ocrSteps.afterReply")
      : (ocrStepText(s, false, runtime, t) ?? "");
  }
  const at = keys.indexOf(current);
  const steps = keys.map((key, i): Step => ({
    key,
    state: i < at || (clear && last === "done") ? "done" : i === at ? "current" : "later",
  }));
  return { steps, detail };
}

function questionsDetail(s: OcrStepsInput, t: TFunction): string {
  const parts: string[] = [];
  if (s.openQuestions > 0) parts.push(t("ocrSteps.open", { count: s.openQuestions }));
  if (s.failedPages.length > 0) {
    parts.push(t("ocrSteps.notCleaned", { count: s.failedPages.length, pages: s.failedPages.join(", ") }));
  }
  return parts.join(" · ");
}

/** "export to DOCX" → "Export to DOCX", a step's name. */
function capitalised(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The step bar: in the chat's reply card and in the OCR tab. */
export function OcrSteps({
  input,
  runtime,
}: {
  input: OcrStepsInput;
  runtime: RuntimeState | undefined;
}) {
  const { t } = useTranslation();
  const { steps, detail } = ocrSteps(input, runtime, t);
  return (
    <ol className="flex flex-wrap items-center gap-x-1 gap-y-0.5 text-sm" data-testid="ocr-steps">
      {steps.map((step, i) => (
        <li
          key={step.key}
          className={`flex items-center gap-1 ${
            step.state === "current" ? "font-semibold text-ink" : step.state === "done" ? "text-accent" : "text-muted"
          }`}
          aria-current={step.state === "current" ? "step" : undefined}
        >
          {i > 0 && <span className="text-muted" aria-hidden>→</span>}
          {step.state === "done" && <span aria-hidden>{TICK}</span>}
          <span>
            {step.key === "export" ? capitalised(input.exports.join(", ")) : t(`ocrSteps.${step.key}`)}
          </span>
          {step.state === "current" && detail && <span className="font-normal text-muted">· {detail}</span>}
        </li>
      ))}
    </ol>
  );
}
