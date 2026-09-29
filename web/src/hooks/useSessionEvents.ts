import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useReducer, useRef } from "react";

import { readEvents } from "../api/client";
import type { ChatEvent, Tab } from "../api/types";

/** What the chat shows about the turn in progress, on top of the stored transcript. */
export interface LiveTurn {
  running: boolean;
  text: string; // streamed since the last stored assistant message
  model: { model_id: string; ctx: number } | null;
  context: { left_out: number; released: number; stubbed_files: string[] } | null;
  cards: { confirmation_id: number; name: string; text: string; approved: boolean | null }[];
  stats: { ttft: number | null; tps: number | null } | null;
  error: string | null;
  stopped: boolean;
  lastSeq: number;
}

export const IDLE: LiveTurn = {
  running: false,
  text: "",
  model: null,
  context: null,
  cards: [],
  stats: null,
  error: null,
  stopped: false,
  lastSeq: 0,
};

export function reduce(state: LiveTurn, event: ChatEvent): LiveTurn {
  if (event.seq <= state.lastSeq) return state; // already seen (a replay after reconnecting)
  const next = { ...state, lastSeq: event.seq };
  switch (event.type) {
    case "turn":
      return event.status === "running"
        ? { ...IDLE, running: true, model: state.model, lastSeq: event.seq }
        : { ...next, running: false, text: "" };
    case "model":
      return { ...next, model: { model_id: event.model_id, ctx: event.ctx } };
    case "context":
      return { ...next, context: event };
    case "delta":
      return { ...next, text: state.text + event.text };
    case "assistant":
      return { ...next, text: "" };
    case "confirm":
      return {
        ...next,
        cards: [
          ...state.cards,
          { confirmation_id: event.confirmation_id, name: event.name, text: event.text, approved: null },
        ],
      };
    case "confirmed":
      return {
        ...next,
        cards: state.cards.map((c) =>
          c.confirmation_id === event.confirmation_id ? { ...c, approved: event.approved } : c,
        ),
      };
    case "done":
      return { ...next, stats: { ttft: event.ttft, tps: event.tps } };
    case "error":
      return { ...next, error: event.detail };
    case "stopped":
      return { ...next, stopped: true };
    default:
      return next;
  }
}

/** Follows a chat's event stream. Refreshes the transcript whenever something is stored,
 * and the project's lists when a turn ends (titles, new files). */
export function useSessionEvents(
  projectId: number,
  sessionId: number,
  onOpen: (tab: Tab) => void,
): LiveTurn {
  const queryClient = useQueryClient();
  const [live, dispatch] = useReducer(reduce, IDLE);
  // The latest onOpen: the page makes a new one on every render (it closes over the tabs),
  // and the stream mustn't restart for that.
  const openRef = useRef(onOpen);
  useEffect(() => {
    openRef.current = onOpen;
  }, [onOpen]);

  useEffect(() => {
    const controller = new AbortController();
    const chatKey = ["chat", projectId, sessionId];
    let seen = 0;
    readEvents<ChatEvent>(
      `/api/projects/${projectId}/sessions/${sessionId}/events`,
      (event) => {
        const fresh = event.seq > seen;
        seen = Math.max(seen, event.seq);
        dispatch(event);
        if (!fresh) return;
        if (event.type === "assistant" || event.type === "tool_result") {
          void queryClient.invalidateQueries({ queryKey: chatKey });
        }
        if (event.type === "tool_result" && event.action?.type === "open_tab") {
          openRef.current(event.action.tab);
        }
        if (event.type === "turn") {
          void queryClient.invalidateQueries({ queryKey: chatKey });
          if (event.status !== "running") {
            void queryClient.invalidateQueries({ queryKey: ["items", projectId] });
            void queryClient.invalidateQueries({ queryKey: ["project", projectId] });
          }
        }
      },
      controller.signal,
    ).catch(() => {
      // aborted on unmount, or the server went away; the transcript still shows what's stored
    });
    return () => controller.abort();
  }, [projectId, sessionId, queryClient]);

  return live;
}
