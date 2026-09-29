import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { apiFetch, readEvents } from "../api/client";
import type { RuntimeState } from "../api/types";

/** Events arriving within this window cause one refetch: a long job sends a lease and a
 * release for every page. */
export const REFRESH_DEBOUNCE_MS = 250;

/** The runtime state, refreshed when the runtime reports events (load, unload, lease,
 * release, crash). */
export function useRuntime() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["runtime"],
    queryFn: () => apiFetch<RuntimeState>("/api/runtime"),
  });

  useEffect(() => {
    const controller = new AbortController();
    let pending: ReturnType<typeof setTimeout> | undefined;
    readEvents<{ type: string }>(
      "/api/runtime/events",
      (event) => {
        if (event.type !== "runtime" || pending !== undefined) return;
        pending = setTimeout(() => {
          pending = undefined;
          void queryClient.invalidateQueries({ queryKey: ["runtime"] });
        }, REFRESH_DEBOUNCE_MS);
      },
      controller.signal,
    ).catch(() => {
      // aborted on unmount, or the server went away
    });
    return () => {
      controller.abort();
      if (pending !== undefined) clearTimeout(pending);
    };
  }, [queryClient]);

  return query;
}
