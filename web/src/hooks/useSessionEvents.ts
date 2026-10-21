import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useReducer, useRef } from "react";

import { followEvents } from "../api/client";
import type { CardKind, ChatEvent, Tab } from "../api/types";

/** What the chat shows about the turn in progress, on top of the stored transcript. */
export interface LiveTurn {
  running: boolean;
  text: string; // streamed since the last stored assistant message
  model: { model_id: string; ctx: number } | null;
  context: { left_out: number; released: number; stubbed_files: string[] } | null;
  // call_id: the tool call each asks about, so it shows under that call's line
  cards: {
    confirmation_id: number;
    call_id: string;
    name: string;
    text: string;
    kind: CardKind;
    approved: boolean | null;
  }[];
  // The latest progress of each tool at work, by tool call.
  progress: Record<string, { label: string; done?: number; total?: number; line?: string; seconds?: number }>;
  stats: { ttft: number | null; tps: number | null } | null;
  usage: { parts: Record<string, number>; budget: number } | null;
  compaction: "working" | { summarised: number } | { failed: string } | null;
  error: string | null;
  stopped: boolean;
  followup: string | null; // this turn is a follow-up's, started when an OCR session finished
  messageId: number | null; // the person's message this turn answers; null for a follow-up's turn
  lastSeq: number;
}

export const IDLE: LiveTurn = {
  running: false,
  text: "",
  model: null,
  context: null,
  cards: [],
  progress: {},
  stats: null,
  usage: null,
  compaction: null,
  error: null,
  stopped: false,
  followup: null,
  messageId: null,
  lastSeq: 0,
};

/** The stream (re)opened: the server replays the turn so far, numbered from its own start. */
export const CONNECTED = { type: "connected" } as const;

export function reduce(state: LiveTurn, event: ChatEvent | typeof CONNECTED): LiveTurn {
  if (event.type === "connected") return IDLE; // rebuilt from the replay that follows
  if (event.type === "replayed" || event.seq <= state.lastSeq) return state; // already seen (a replay after reconnecting)
  const next = { ...state, lastSeq: event.seq };
  switch (event.type) {
    case "turn":
      return event.status === "running"
        ? {
            ...IDLE,
            running: true,
            model: state.model,
            usage: state.usage,
            followup: "followup" in event ? event.followup : null,
            messageId: "message_id" in event ? event.message_id : null,
            lastSeq: event.seq,
          }
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
          {
            confirmation_id: event.confirmation_id,
            call_id: event.call_id,
            name: event.name,
            text: event.text,
            kind: event.kind ?? "allow",
            approved: null,
          },
        ],
      };
    case "confirmed":
      return {
        ...next,
        cards: state.cards.map((c) =>
          c.confirmation_id === event.confirmation_id ? { ...c, approved: event.approved } : c,
        ),
      };
    case "progress": {
      const { label, done, total, line, seconds } = event;
      return { ...next, progress: { ...state.progress, [event.call_id]: { label, done, total, line, seconds } } };
    }
    case "tool_result": {
      // The tool is finished: its result line replaces its progress.
      const progress = { ...state.progress };
      delete progress[event.call_id];
      return { ...next, progress };
    }
    case "usage":
      return { ...next, usage: { parts: event.parts, budget: event.budget } };
    case "compacting":
      return { ...next, compaction: "working" };
    case "compacted":
      return { ...next, compaction: { summarised: event.summarised } };
    case "compaction_failed":
      return { ...next, compaction: { failed: event.detail } };
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

/** Chats whose stream is past its replay and acting on events, as "project:session", with a
 * count in case one mounts twice. The project's stream leaves their tabs to them, and opens
 * the tabs of every other chat (useProjectEvents). */
const liveChats = new Map<string, number>();

export function isChatLive(projectId: number, sessionId: number): boolean {
  return liveChats.has(`${projectId}:${sessionId}`);
}

/** Follows a chat's event stream. Refreshes the transcript whenever something is stored,
 * and the project's lists when a turn ends (titles, new files). */
export function useSessionEvents(
  projectId: number,
  sessionId: number,
  onOpen: (tab: Tab, focus?: boolean) => void,
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
    const liveKey = `${projectId}:${sessionId}`;
    let seen = 0;
    // Events before the server's "replayed" marker happened earlier (a reload, or switching
    // back to this tab): show them, but don't repeat their actions.
    let live = false;
    const unregister = () => {
      if (!live) return;
      live = false;
      const left = (liveChats.get(liveKey) ?? 1) - 1;
      if (left > 0) liveChats.set(liveKey, left);
      else liveChats.delete(liveKey);
    };
    followEvents<ChatEvent>(
      `/api/projects/${projectId}/sessions/${sessionId}/events`,
      (event) => {
        if (event.type === "replayed") {
          live = true;
          liveChats.set(liveKey, (liveChats.get(liveKey) ?? 0) + 1);
          return;
        }
        const fresh = event.seq > seen;
        seen = Math.max(seen, event.seq);
        dispatch(event);
        if (!fresh) return;
        if (event.type === "assistant" || event.type === "tool_result" || event.type === "compacted") {
          void queryClient.invalidateQueries({ queryKey: chatKey });
        }
        if (live && event.type === "tool_result" && event.action?.type === "open_tab") {
          openRef.current(event.action.tab, event.action.focus !== false);
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
      (again) => {
        // (Re)opened, after a restart perhaps: its numbering starts again, and until its
        // replay ends the project's stream opens this chat's tabs.
        unregister();
        seen = 0;
        dispatch(CONNECTED);
        if (again) void queryClient.invalidateQueries({ queryKey: chatKey }); // stored while away
      },
    ).catch(() => {
      // aborted on unmount, or refused (the chat is gone); the transcript shows what's stored
    });
    return () => {
      controller.abort();
      unregister();
    };
  }, [projectId, sessionId, queryClient]);

  return live;
}
