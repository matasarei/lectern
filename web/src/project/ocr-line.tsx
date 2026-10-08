import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost } from "../api/client";
import type { OcrSession, ProjectItems, SessionSummary, Tab } from "../api/types";
import { useRuntime } from "../hooks/useRuntime";
import { HoldNote, ocrStepText, type OcrStepInput } from "../status";
import { ocrPath, ocrProgress, useOcrEvents } from "./ocr-api";

/** Under the chat's ocr.start line: where the OCR session it began has got, and what runs
 * after it. The whole line opens the session's tab. */
export function OcrLine({
  projectId,
  sessionId,
  then,
  onOpen,
}: {
  projectId: number;
  sessionId: number;
  then: string[]; // the follow-ups' labels waiting for it
  onOpen: (tab: Tab) => void;
}) {
  const items = useQuery({
    queryKey: ["items", projectId],
    queryFn: () => apiFetch<ProjectItems>(`/api/projects/${projectId}/items`),
  });
  const summary = items.data?.sessions.find((s) => s.id === sessionId);
  if (!summary?.ocr) return null; // not listed yet, or gone
  // Only a session still at work follows its events: a transcript with many finished ones
  // mustn't open a stream each (a browser allows six connections per host).
  const finished = summary.ocr.status === "done" || summary.ocr.status === "failed";
  return finished ? (
    <Line summary={summary} then={then} onOpen={onOpen} step={finishedStep(summary)} />
  ) : (
    <LiveLine projectId={projectId} summary={summary} then={then} onOpen={onOpen} />
  );
}

function finishedStep(summary: SessionSummary): OcrStepInput {
  const ocr = summary.ocr!;
  return {
    status: ocr.status,
    phase: null,
    pagesTotal: ocr.pages,
    read: ocr.pages,
    cleaned: ocr.pages,
    openQuestions: ocr.open_doubts,
    reason: null,
  };
}

function LiveLine({
  projectId,
  summary,
  then,
  onOpen,
}: {
  projectId: number;
  summary: SessionSummary;
  then: string[];
  onOpen: (tab: Tab) => void;
}) {
  const base = ocrPath(projectId, summary.id);
  const live = useOcrEvents(projectId, summary.id);
  const session = useQuery({
    queryKey: ["ocr", projectId, summary.id],
    queryFn: () => apiFetch<OcrSession>(base),
  });
  const runtime = useRuntime();
  const startNow = useMutation({ mutationFn: () => apiPost(`${base}/start-now`) });
  const hold = runtime.data?.hold;
  const data = session.data;
  const step: OcrStepInput = data
    ? {
        status: data.status,
        phase: live.phase,
        pagesTotal: data.pages_total,
        ...ocrProgress(data),
        openQuestions: data.document?.open_doubts ?? 0,
        reason: data.error ?? data.paused_reason,
      }
    : { ...finishedStep(summary), read: 0, cleaned: 0 }; // until the session is read
  return (
    <Line
      summary={summary}
      then={then}
      onOpen={onOpen}
      step={step}
      waitingForChat={live.waitingForChat}
      hold={
        <HoldNote
          seconds={hold ? hold.resume_in : live.resumeIn}
          since={hold ? runtime.dataUpdatedAt : live.waitingSince}
          onStartNow={() => startNow.mutate()}
          starting={startNow.isPending}
        />
      }
    />
  );
}

function Line({
  summary,
  step,
  then,
  onOpen,
  waitingForChat = false,
  hold,
}: {
  summary: SessionSummary;
  step: OcrStepInput;
  then: string[];
  onOpen: (tab: Tab) => void;
  waitingForChat?: boolean;
  hold?: React.ReactNode;
}) {
  const { t } = useTranslation();
  const runtime = useRuntime();
  const text = ocrStepText(step, waitingForChat, runtime.data, t);
  // Its label reads "after OCR · <title>: export to DOCX"; the line already names the session
  const prefix = `after OCR · ${summary.title}: `;
  const next = then.map((label) => (label.startsWith(prefix) ? label.slice(prefix.length) : label));
  return (
    <div
      className="flex flex-wrap items-center gap-2 text-sm"
      data-testid="ocr-line"
      role={hold ? "status" : undefined} // a running session's line changes; a finished one won't
    >
      <button
        className="text-left text-accent hover:underline"
        onClick={() => onOpen({ kind: "session", id: summary.id })}
      >
        {t("chat.ocrLine", { title: summary.title })}
      </button>
      {text === null ? hold : <span>{text}</span>}
      {step.status !== "done" && step.status !== "failed" && next.length > 0 && (
        <span className="text-muted">{t("chat.ocrThen", { what: next.join(", ") })}</span>
      )}
    </div>
  );
}
