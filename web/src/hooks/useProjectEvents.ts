import { useEffect, useRef } from "react";

import { readEvents } from "../api/client";
import type { ProjectEvent, Tab } from "../api/types";

/** The tabs this project's chats open. The page shows one chat at a time, and that chat's own
 * stream opens its tabs (useSessionEvents); this adds the tab of a chat that isn't showing (a
 * DOCX exported after the last answer in the OCR tab), without taking the person away from
 * the tab they're on. One stream per page, whatever the number of chats. */
export function useProjectEvents(
  projectId: number,
  showingChat: number | null,
  onOpen: (tab: Tab, focus?: boolean) => void,
): void {
  // The latest of both: the page makes a new onOpen on every render, and the stream mustn't
  // restart for that.
  const latest = useRef({ showingChat, onOpen });
  useEffect(() => {
    latest.current = { showingChat, onOpen };
  }, [showingChat, onOpen]);

  useEffect(() => {
    const controller = new AbortController();
    readEvents<ProjectEvent>(
      `/api/projects/${projectId}/events`,
      (event) => {
        if (event.type !== "open_tab") return;
        if (event.session_id === latest.current.showingChat) return; // its own stream opens it
        latest.current.onOpen(event.tab, false);
      },
      controller.signal,
    ).catch(() => {
      // aborted on unmount, or the server went away: a showing chat still opens its tabs
    });
    return () => controller.abort();
  }, [projectId]);
}
