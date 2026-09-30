/**
 * OCR sessions on the server: starting one, and following its job. The session is cached
 * under ["ocr", projectId, sessionId]; each event from the job is applied to that copy, so
 * the tab updates page by page without reading the whole session again.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { apiPost, readEvents } from "../api/client";
import type { OcrEvent, OcrPage, OcrPhase, OcrSession } from "../api/types";

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
      return { phase: event.phase, waitingForChat: false };
    case "waiting":
      return { ...live, waitingForChat: true };
    case "page":
      return event.status === "recognising" ? live : { ...live, waitingForChat: false };
    case "session":
      return event.status === "running" ? live : { phase: null, waitingForChat: false };
    default:
      return live;
  }
}

/** Follows an OCR session's events, keeping the cached session and the live state current. */
export function useOcrEvents(projectId: number, sessionId: number): OcrLive {
  const queryClient = useQueryClient();
  const [live, setLive] = useState<OcrLive>({ phase: null, waitingForChat: false });

  useEffect(() => {
    const controller = new AbortController();
    const key = ["ocr", projectId, sessionId];
    readEvents<OcrEvent>(
      `${ocrPath(projectId, sessionId)}/events`,
      (event) => {
        if (event.type === "replayed") return;
        setLive((prev) => applyToLive(prev, event));
        const cached = queryClient.getQueryData<OcrSession>(key);
        if (cached) queryClient.setQueryData(key, applyOcrEvent(cached, event));
        else void queryClient.invalidateQueries({ queryKey: key });
        if (event.type === "document") void queryClient.invalidateQueries({ queryKey: key });
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
    ).catch(() => {
      // aborted on unmount, or the server went away; the stored session still shows
    });
    return () => controller.abort();
  }, [projectId, sessionId, queryClient]);

  return live;
}
