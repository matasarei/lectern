import type { TFunction } from "i18next";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { RuntimeState } from "./api/types";
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
      <button className="btn-secondary px-2 py-0.5 text-xs" disabled={starting} onClick={onStartNow}>
        {t("status.startNow")}
      </button>
    </span>
  );
}
