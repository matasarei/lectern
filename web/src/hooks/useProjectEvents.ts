import { useEffect, useRef } from "react";

import { followEvents } from "../api/client";
import type { ProjectEvent, Tab } from "../api/types";
import { isChatLive } from "./useSessionEvents";

/** The tabs this project's chats open. A chat whose own stream is live opens its tabs itself
 * (useSessionEvents); this adds the tab of any other chat (a DOCX exported after the last
 * answer in the OCR tab), without taking the person away from the tab they're on. One stream
 * per page, whatever the number of chats. */
export function useProjectEvents(projectId: number, onOpen: (tab: Tab, focus?: boolean) => void): void {
  // The latest onOpen: the page makes a new one on every render, and the stream mustn't
  // restart for that.
  const openRef = useRef(onOpen);
  useEffect(() => {
    openRef.current = onOpen;
  }, [onOpen]);

  useEffect(() => {
    const controller = new AbortController();
    followEvents<ProjectEvent>(
      `/api/projects/${projectId}/events`,
      (event) => {
        if (event.type !== "open_tab") return;
        // Decided by the chat's stream, not by which tab is active: a chat tab still loading,
        // or one just switched to, isn't acting on events yet.
        if (isChatLive(projectId, event.session_id)) return;
        openRef.current(event.tab, false);
      },
      controller.signal,
    ).catch(() => {
      // aborted on unmount, or refused: a showing chat still opens its tabs
    });
    return () => controller.abort();
  }, [projectId]);
}
