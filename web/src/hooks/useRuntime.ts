import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { apiFetch, followEvents } from "../api/client";
import type { RuntimeState } from "../api/types";

/** Events arriving within this window cause one refetch: a long job sends a lease and a
 * release for every page. */
export const REFRESH_DEBOUNCE_MS = 250;

/** One runtime event stream for the whole page, however many components read the runtime
 * (the header, the chat, the OCR tab, Models & memory): over HTTP/1.1 a browser allows six
 * connections per host, and a stream per reader would use them up. It opens with the first
 * reader and closes with the last. */
const shared = {
  readers: new Map<QueryClient, number>(),
  controller: null as AbortController | null,
  pending: undefined as ReturnType<typeof setTimeout> | undefined,
};

function subscribe(queryClient: QueryClient): () => void {
  shared.readers.set(queryClient, (shared.readers.get(queryClient) ?? 0) + 1);
  if (shared.controller === null) {
    const controller = new AbortController();
    shared.controller = controller;
    followEvents<{ type: string }>(
      "/api/runtime/events",
      (event) => {
        if (event.type !== "runtime" || shared.pending !== undefined) return;
        shared.pending = setTimeout(() => {
          shared.pending = undefined;
          for (const client of shared.readers.keys()) {
            void client.invalidateQueries({ queryKey: ["runtime"] });
          }
        }, REFRESH_DEBOUNCE_MS);
      },
      controller.signal,
      (again) => {
        // Reopened: whatever changed while it was closed (a restart unloads everything)
        if (!again) return;
        for (const client of shared.readers.keys()) {
          void client.invalidateQueries({ queryKey: ["runtime"] });
        }
      },
    ).catch(() => {
      // aborted with the last reader, or refused
    });
  }
  return () => {
    const left = (shared.readers.get(queryClient) ?? 1) - 1;
    if (left > 0) shared.readers.set(queryClient, left);
    else shared.readers.delete(queryClient);
    if (shared.readers.size === 0) {
      shared.controller?.abort();
      shared.controller = null;
      if (shared.pending !== undefined) clearTimeout(shared.pending);
      shared.pending = undefined;
    }
  };
}

/** The runtime state, refreshed when the runtime reports events (load, unload, lease,
 * release, crash, turns). */
export function useRuntime() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["runtime"],
    queryFn: () => apiFetch<RuntimeState>("/api/runtime"),
  });

  useEffect(() => subscribe(queryClient), [queryClient]);

  return query;
}
