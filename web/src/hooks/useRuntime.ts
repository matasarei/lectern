import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { apiFetch, readEvents } from "../api/client";
import type { RuntimeState } from "../api/types";

/** The runtime state, refreshed whenever the runtime reports an event (load, unload, lease,
 * release, crash). */
export function useRuntime() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["runtime"],
    queryFn: () => apiFetch<RuntimeState>("/api/runtime"),
  });

  useEffect(() => {
    const controller = new AbortController();
    readEvents<{ type: string }>(
      "/api/runtime/events",
      (event) => {
        if (event.type === "runtime") void queryClient.invalidateQueries({ queryKey: ["runtime"] });
      },
      controller.signal,
    ).catch(() => {
      // aborted on unmount, or the server went away
    });
    return () => controller.abort();
  }, [queryClient]);

  return query;
}
