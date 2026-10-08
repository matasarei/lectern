import type { TFunction } from "i18next";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { OcrPhase, OcrStatus, RuntimeState } from "./api/types";
import { formatBytes } from "./format";

/** Seconds left of `seconds` counted from `since` (ms), ticking every second; null stays null. */
export function useCountdown(seconds: number | null | undefined, since: number): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (seconds == null) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [seconds]);
  if (seconds == null) return null;
  return Math.max(0, Math.round(seconds - (now - since) / 1000));
}

/** 100 → "1:40". */
export function formatCountdown(seconds: number): string {
  const m = Math.floor(seconds / 60);
  return `${m}:${String(seconds % 60).padStart(2, "0")}`;
}

/** What the chat waits for before its model answers: the engine downloading, the model
 * loading, an OCR page finishing; else nothing more specific than "the model". */
export function modelWaitText(runtime: RuntimeState | undefined, t: TFunction): string {
  const activity = runtime?.activity;
  if (activity?.kind === "engine") {
    return t("status.engine", { done: formatBytes(activity.done), total: formatBytes(activity.total) });
  }
  if (activity?.kind === "loading") return t("status.loading", { model: activity.model_id });
  if (runtime?.residents?.some((r) => r.role !== "main" && r.leases > 0)) return t("status.jobPage");
  return t("chat.waitingModel");
}

/** A job waiting for the chat: the countdown, or "after the reply", with Start now. */
export function HoldNote({
  seconds,
  since,
  onStartNow,
  starting,
}: {
  seconds: number | null;
  since: number;
  onStartNow: () => void;
  starting: boolean;
}) {
  const { t } = useTranslation();
  const left = useCountdown(seconds, since);
  return (
    <span className="flex items-center gap-2 text-muted" role="status">
      {left === null ? t("status.afterReply") : t("status.startsIn", { time: formatCountdown(left) })}
      <button className="btn-primary px-2 py-0.5 text-xs" disabled={starting} onClick={onStartNow}>
        {t("status.startNow")}
      </button>
    </span>
  );
}

/** Where an OCR session is, for the chat's line and the OCR tab. */
export interface OcrStepInput {
  status: OcrStatus;
  phase: OcrPhase | null; // from its events, while it runs
  pagesTotal: number;
  read: number;
  cleaned: number;
  openQuestions: number;
  reason: string | null; // why it paused, or what stopped it
}

/** The model an OCR phase is loading, in words; null when none is. Reading loads the OCR
 * model, cleaning the chat's: the swaps that are the silent gaps between them. */
export function ocrLoadingText(
  phase: OcrPhase | null,
  runtime: RuntimeState | undefined,
  t: TFunction,
): string | null {
  const loading = runtime?.activity?.kind === "loading" ? runtime.activity.role : null;
  if (phase === "recognise" && (loading === "ocr" || loading === "ocr.hand")) {
    return t("ocrStep.loadingOcr");
  }
  if (phase === "clean" && loading === "main") return t("ocrStep.loadingMain");
  return null;
}

/** The step an OCR session is at, in words: the model loading (the gaps between reading and
 * cleaning are swaps), the page it's on, done. Null while it waits for the chat: the caller
 * shows HoldNote there, with its countdown and Start now. */
export function ocrStepText(
  s: OcrStepInput,
  waitingForChat: boolean,
  runtime: RuntimeState | undefined,
  t: TFunction,
): string | null {
  if (s.status === "done") {
    const pages = t("ocrStep.pages", { count: s.pagesTotal });
    return s.openQuestions > 0
      ? `${t("ocrStep.done")} · ${pages} · ${t("ocrStep.questions", { count: s.openQuestions })}`
      : `${t("ocrStep.done")} · ${pages}`;
  }
  if (s.status === "failed") return t("ocrStep.failed", { reason: s.reason ?? "" });
  if (s.status === "paused") {
    return s.reason ? t("ocrStep.pausedBecause", { reason: s.reason }) : t("ocrStep.paused");
  }
  if (waitingForChat) return null;
  const loading = ocrLoadingText(s.phase, runtime, t);
  if (loading) return loading;
  const page = (done: number) => Math.min(done + 1, Math.max(s.pagesTotal, 1));
  switch (s.phase) {
    case "rasterise":
      return t("ocrStep.rasterise");
    case "recognise":
      return t("ocrStep.reading", { page: page(s.read), total: s.pagesTotal });
    case "clean":
      return t("ocrStep.cleaning", { page: page(s.cleaned), total: s.pagesTotal });
    case "assemble":
      return t("ocrStep.assemble");
    default:
      return t("ocrStep.queued");
  }
}
