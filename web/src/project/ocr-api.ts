/**
 * OCR sessions on the server: starting one, and following its job. The session is cached
 * under ["ocr", projectId, sessionId]; each event from the job is applied to that copy, so
 * the tab updates page by page without reading the whole session again.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { apiFetch, apiPost, followEvents } from "../api/client";
import type { DocBlock, DocDoubt, OcrDocument, OcrEvent, OcrPage, OcrPhase, OcrSession } from "../api/types";

export function ocrPath(projectId: number, sessionId?: number): string {
  const base = `/api/projects/${projectId}/ocr`;
  return sessionId === undefined ? base : `${base}/${sessionId}`;
}

export function startOcr(projectId: number, artifactId: number): Promise<OcrSession> {
  return apiPost<OcrSession>(ocrPath(projectId), { artifact_id: artifactId });
}

/** What the tab shows beside the stored session: the phase, and why the job waits. */
export interface OcrLive {
  phase: OcrPhase | null;
  waitingForChat: boolean;
  /** How long it waits: seconds of the chat's idle window, or null until the reply ends. */
  resumeIn: number | null;
  /** When the wait was reported (ms), for the countdown. */
  waitingSince: number;
}

const NOT_WAITING = { waitingForChat: false, resumeIn: null, waitingSince: 0 };

/** How far a session has got: pages read, pages cleaned. */
export function ocrProgress(session: OcrSession): { read: number; cleaned: number } {
  return {
    read: session.pages.filter((p) => p.status === "done").length,
    cleaned: session.pages.filter((p) => p.clean_status === "done").length,
  };
}

/** An event applied to the cached session. Pure, so it's tested on its own. */
export function applyOcrEvent(session: OcrSession, event: OcrEvent): OcrSession {
  switch (event.type) {
    case "session":
      return {
        ...session,
        status: event.status,
        paused_reason: event.reason,
        error: event.error,
      };
    case "document":
      return {
        ...session,
        document: {
          version: event.version,
          artifact_id: event.artifact_id,
          open_doubts: session.document?.open_doubts ?? 0, // the reload brings the count
        },
      };
    case "page": {
      const page = pageOf(event);
      const known = session.pages.some((p) => p.number === page.number);
      const pages = known
        ? session.pages.map((p) => (p.number === page.number ? page : p))
        : [...session.pages, page].sort((a, b) => a.number - b.number);
      return { ...session, pages, pages_total: Math.max(session.pages_total, pages.length) };
    }
    default:
      return session;
  }
}

/** The page in a "page" event, without the event's own fields. */
function pageOf(event: OcrEvent & { type: "page" }): OcrPage {
  const page: Partial<OcrEvent & { type: "page" }> = { ...event };
  delete page.type;
  delete page.seq;
  return page as OcrPage;
}

export function applyToLive(live: OcrLive, event: OcrEvent): OcrLive {
  switch (event.type) {
    case "phase":
      return { ...live, phase: event.phase, ...NOT_WAITING };
    case "waiting":
      return {
        ...live,
        waitingForChat: true,
        resumeIn: event.resume_in ?? null,
        waitingSince: Date.now(),
      };
    case "page":
      // A page is marked reading or cleaning once the job has the model: the wait is over.
      return { ...live, ...NOT_WAITING };
    case "session":
      return event.status === "running" ? live : { phase: null, ...NOT_WAITING };
    default:
      return live;
  }
}

/** Follows an OCR session's events, keeping the cached session and the live state current. */
export function useOcrEvents(projectId: number, sessionId: number): OcrLive {
  const queryClient = useQueryClient();
  const [live, setLive] = useState<OcrLive>({ phase: null, ...NOT_WAITING });

  useEffect(() => {
    const controller = new AbortController();
    const key = ["ocr", projectId, sessionId];
    followEvents<OcrEvent>(
      `${ocrPath(projectId, sessionId)}/events`,
      (event) => {
        if (event.type === "replayed") return;
        setLive((prev) => applyToLive(prev, event));
        const cached = queryClient.getQueryData<OcrSession>(key);
        if (cached) queryClient.setQueryData(key, applyOcrEvent(cached, event));
        else void queryClient.invalidateQueries({ queryKey: key });
        if (event.type === "document") {
          void queryClient.invalidateQueries({ queryKey: key });
          void queryClient.invalidateQueries({ queryKey: documentKey(projectId, sessionId) });
        }
        if (event.type === "phase" && event.phase === "recognise") {
          // The page images are made: read the session once to know all its pages, not only
          // the ones whose events have come in.
          void queryClient.invalidateQueries({ queryKey: key });
        }
        if (event.type === "session" && event.status !== "running" && event.status !== "queued") {
          void queryClient.invalidateQueries({ queryKey: key }); // pages rasterised meanwhile
          void queryClient.invalidateQueries({ queryKey: ["items", projectId] });
        }
      },
      controller.signal,
      (again) => {
        // Reopened: pages may have been read, or the job paused by a restart, meanwhile
        if (again) void queryClient.invalidateQueries({ queryKey: key });
      },
    ).catch(() => {
      // aborted on unmount, or refused (the session is gone); the stored session still shows
    });
    return () => controller.abort();
  }, [projectId, sessionId, queryClient]);

  return live;
}

// --- the Document, and answering its questions (M3b) -------------------------------------

export function documentKey(projectId: number, sessionId: number) {
  return ["ocr-document", projectId, sessionId];
}

/** The session's Document, once it has one; read again on each "document" event. */
export function useOcrDocument(projectId: number, sessionId: number, version: number | null) {
  return useQuery({
    queryKey: documentKey(projectId, sessionId),
    queryFn: () => apiFetch<OcrDocument>(`${ocrPath(projectId, sessionId)}/document`),
    enabled: version !== null,
  });
}

export interface DocumentSummary {
  version: number;
  artifact_id: number;
  open_doubts: number;
}

/** Accept a candidate or Edit carry the reading; Keep takes the page's own, Remove nothing. */
export type Answer = { action: "accept" | "edit"; value: string } | { action: "keep" | "remove" };

export function answerDoubt(
  projectId: number,
  sessionId: number,
  doubtId: string,
  answer: Answer,
): Promise<DocumentSummary> {
  return apiFetch<DocumentSummary>(`${ocrPath(projectId, sessionId)}/doubts/${doubtId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(answer),
  });
}

export function reopenDoubt(projectId: number, sessionId: number, doubtId: string): Promise<DocumentSummary> {
  return apiFetch<DocumentSummary>(`${ocrPath(projectId, sessionId)}/doubts/${doubtId}`, {
    method: "DELETE",
  });
}

export function cleanPageAgain(projectId: number, sessionId: number, page: number): Promise<OcrPage> {
  return apiPost<OcrPage>(`${ocrPath(projectId, sessionId)}/pages/${page}/clean`);
}

/** The page a doubt came from: its id says so ("d3.1"), whatever block it sits in. */
export function doubtPage(doubt: DocDoubt): number {
  return Number(/^d(\d+)\./.exec(doubt.id)?.[1] ?? 0);
}

/** What Accept takes: the first candidate that isn't what the page seems to say, else
 * that ("it really says 2312/2019"). */
export function suggestion(doubt: DocDoubt): string {
  return doubt.candidates.find((c) => c.trim() && c !== doubt.raw) ?? doubt.raw;
}

/** Whether there's a suggestion other than the reading: when not, Accept would change nothing. */
export function suggests(doubt: DocDoubt): boolean {
  return suggestion(doubt) !== doubt.raw;
}

/** The doubt is all the block says: its mark once and no other word, as the server decides
 * (`covers_block`). Only such a question can be answered by removing its block. */
export function coversBlock(block: DocBlock, doubtId: string): boolean {
  const mark = `{${doubtId}}`;
  if (block.type === "table" || block.type === "figure" || block.text.split(mark).length !== 2) return false;
  return !/[\p{L}\p{N}_]/u.test(block.text.replace(mark, ""));
}

/** A block the person removed: its doubt answered with nothing. Kept in the Document so the
 * answer can be taken back, left out of what is made from it. */
export function isRemoved(block: DocBlock): boolean {
  return block.doubts.some((d) => d.status === "resolved" && d.value === "" && coversBlock(block, d.id));
}

/** A page's blocks, in order, including one that starts on an earlier page and runs on. */
export function pageBlocks(doc: OcrDocument, page: number): DocBlock[] {
  const ids = doc.pages.find((p) => p.n === page)?.blocks ?? [];
  const byId = new Map(doc.blocks.map((b) => [b.id, b]));
  return ids.flatMap((id) => byId.get(id) ?? []);
}

/** A page's questions in reading order: the doubts that came from it. */
export function pageQuestions(doc: OcrDocument, page: number): DocDoubt[] {
  return doc.blocks.flatMap((b) => b.doubts.filter((d) => doubtPage(d) === page));
}

export function openQuestions(doc: OcrDocument): DocDoubt[] {
  return doc.blocks.flatMap((b) => b.doubts.filter((d) => d.status === "open"));
}

/** The next page after `page` with an open question, wrapping round; null when none has. */
export function nextPageWithQuestions(doc: OcrDocument, page: number): number | null {
  const pages = [...new Set(openQuestions(doc).map(doubtPage))].sort((a, b) => a - b);
  if (pages.length === 0) return null;
  return pages.find((p) => p > page) ?? pages[0];
}
