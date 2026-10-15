import type { TFunction } from "i18next";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { OcrPage, RuntimeState } from "../api/types";
import { formatElapsed } from "../format";
import { ocrStepText, type OcrStepInput } from "../status";

/** Where an OCR session is, as the whole: what's done, what's going on, what's still to come. */
export interface OcrStepsInput extends OcrStepInput {
  failedPages: number[]; // read or cleanup failed: the Document isn't final
  waitingForChat: boolean;
  exports: string[]; // what waits for the Document to be clear: "export to DOCX"
  stepStartedAt: string | null; // when the page at work began its reading or cleanup
}

/** When the step at work began: the page being read or cleaned, as the server recorded it
 * (so a reload doesn't start the count again). Null when no page is at either. */
export function stepStartedAt(pages: OcrPage[]): string | null {
  const busy = pages.find((p) => p.status === "recognising" || p.clean_status === "cleaning");
  return busy?.step_started_at ?? null;
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
    // At work, waiting, paused or stopped: the phase it's in, with today's wording. Before
    // any phase event (the page opened mid-job), the page counts say where it is.
    const phase = s.phase ?? (s.status === "running" ? phaseFromCounts(s) : null);
    current = phase === "clean" || phase === "assemble" ? "clean" : "read";
    if (!phase && s.read >= s.pagesTotal && s.pagesTotal > 0) current = "clean"; // paused while cleaning
    detail = s.waitingForChat
      ? t("ocrSteps.afterReply")
      : (ocrStepText({ ...s, phase }, false, runtime, t) ?? "");
  }
  const at = keys.indexOf(current);
  const steps = keys.map((key, i): Step => ({
    key,
    state: i < at || (clear && last === "done") ? "done" : i === at ? "current" : "later",
  }));
  return { steps, detail };
}

function phaseFromCounts(s: OcrStepsInput): OcrStepsInput["phase"] {
  if (s.pagesTotal === 0) return "rasterise";
  if (s.read < s.pagesTotal) return "recognise";
  return s.cleaned < s.pagesTotal ? "clean" : "assemble";
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
  held = false,
}: {
  input: OcrStepsInput;
  runtime: RuntimeState | undefined;
  held?: boolean; // waiting for the chat, said elsewhere (the OCR tab's countdown), not in the text
}) {
  const { t } = useTranslation();
  const { steps, detail } = ocrSteps(input, runtime, t);
  // At work: running, and not waiting for the chat's reply. A model loading counts, since
  // something is happening; queued, paused, done and failed don't.
  const working = input.status === "running" && !input.waitingForChat && !held;
  const elapsed = useElapsed(working ? input.stepStartedAt : null);
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
          {step.state === "current" && elapsed !== null && (
            <span
              className="font-normal tabular-nums text-muted"
              title={t("ocrSteps.elapsed", { time: formatElapsed(elapsed) })}
              data-testid="ocr-elapsed"
            >
              · {formatElapsed(elapsed)}
            </span>
          )}
          {step.state === "current" && working && (
            <span className="spinner ml-1" aria-hidden data-testid="ocr-working" />
          )}
        </li>
      ))}
    </ol>
  );
}

/** Whole seconds since `since`, counted again each second; null without a start. */
function useElapsed(since: string | null): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!since) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [since]);
  if (!since) return null;
  const started = Date.parse(since);
  // Until the first tick, `now` can be from before a step that has just begun: 0:00 then.
  return Number.isNaN(started) ? null : Math.max(0, Math.floor((now - started) / 1000));
}
