import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiFetch } from "../api/client";
import type { OcrSession, ProjectItems, SessionSummary, Tab } from "../api/types";
import { useRuntime } from "../hooks/useRuntime";
import { ocrPath, ocrProgress, useOcrEvents } from "./ocr-api";
import { OcrSteps, type OcrStepsInput, stepStartedAt } from "./ocr-steps";

/** Under the chat's ocr.start line: the OCR session it began, as steps (Reading, Cleaning,
 * Your questions, then what waits for it). The name opens the session's tab. No Start now
 * here: the wait for this chat's reply is a moment; the OCR tab has it for longer waits. */
export function OcrLine({
  projectId,
  sessionId,
  then,
  onOpen,
}: {
  projectId: number;
  sessionId: number;
  then: string[]; // what waits for it, in words: "export to DOCX"
  onOpen: (tab: Tab) => void;
}) {
  const items = useQuery({
    queryKey: ["items", projectId],
    queryFn: () => apiFetch<ProjectItems>(`/api/projects/${projectId}/items`),
  });
  const summary = items.data?.sessions.find((s) => s.id === sessionId);
  if (!summary?.ocr) return null; // not listed yet, or gone
  // Only a session still at work follows its events: a transcript with many finished ones
  // has nothing to hear from them, and needn't hold a socket open for each.
  const finished = summary.ocr.status === "done" || summary.ocr.status === "failed";
  return finished ? (
    <Line summary={summary} onOpen={onOpen} input={finishedInput(summary, then)} />
  ) : (
    <LiveLine projectId={projectId} summary={summary} then={then} onOpen={onOpen} />
  );
}

function finishedInput(summary: SessionSummary, then: string[]): OcrStepsInput {
  const ocr = summary.ocr!;
  return {
    status: ocr.status,
    phase: null,
    pagesTotal: ocr.pages,
    read: ocr.pages,
    cleaned: ocr.pages,
    openQuestions: ocr.open_doubts,
    reason: null,
    failedPages: ocr.failed_pages,
    waitingForChat: false,
    exports: then,
    stepStartedAt: null,
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
  const live = useOcrEvents(projectId, summary.id);
  const session = useQuery({
    queryKey: ["ocr", projectId, summary.id],
    queryFn: () => apiFetch<OcrSession>(ocrPath(projectId, summary.id)),
  });
  const data = session.data;
  const input: OcrStepsInput = data
    ? {
        status: data.status,
        phase: live.phase,
        pagesTotal: data.pages_total,
        ...ocrProgress(data),
        openQuestions: data.document?.open_doubts ?? 0,
        reason: data.error ?? data.paused_reason,
        failedPages: data.pages
          .filter((p) => p.status === "failed" || p.clean_status === "failed")
          .map((p) => p.number),
        waitingForChat: live.waitingForChat,
        exports: then,
        stepStartedAt: stepStartedAt(data.pages),
      }
    : { ...finishedInput(summary, then), read: 0, cleaned: 0 }; // until the session is read
  return <Line summary={summary} onOpen={onOpen} input={input} live />;
}

function Line({
  summary,
  input,
  onOpen,
  live = false,
}: {
  summary: SessionSummary;
  input: OcrStepsInput;
  onOpen: (tab: Tab) => void;
  live?: boolean; // a running session's line changes, and is announced; a finished one won't
}) {
  const { t } = useTranslation();
  const runtime = useRuntime();
  return (
    <div className="grid gap-1 text-sm" data-testid="ocr-line" role={live ? "status" : undefined}>
      <button
        className="w-fit text-left text-accent hover:underline"
        onClick={() => onOpen({ kind: "session", id: summary.id })}
      >
        {t("chat.ocrLine", { title: summary.title })}
      </button>
      <OcrSteps
        input={input}
        runtime={runtime.data}
        onQuestions={() => onOpen({ kind: "session", id: summary.id, questions: true })}
      />
    </div>
  );
}
