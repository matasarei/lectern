import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { followEvents } from "../api/client";
import type { DownloadState } from "../api/types";

/** Live download progress from /api/models/events, by model id. Also refreshes the
 * model and first-run queries when a download ends, so the rest of the page catches up. */
export function useDownloadEvents(): Record<string, DownloadState> {
  const queryClient = useQueryClient();
  const [live, setLive] = useState<Record<string, DownloadState>>({});

  useEffect(() => {
    const controller = new AbortController();
    followEvents<DownloadState & { type: string }>(
      "/api/models/events",
      (event) => {
        if (event.type !== "download") return;
        setLive((prev) => ({ ...prev, [event.model_id]: event }));
        if (event.status !== "downloading") {
          void queryClient.invalidateQueries({ queryKey: ["models"] });
          void queryClient.invalidateQueries({ queryKey: ["first-run"] });
        }
      },
      controller.signal,
      (again) => {
        // Reopened: a download may have ended, or been interrupted, while it was closed
        if (!again) return;
        void queryClient.invalidateQueries({ queryKey: ["models"] });
        void queryClient.invalidateQueries({ queryKey: ["first-run"] });
      },
    ).catch(() => {
      // aborted on unmount, or refused; the queries still show the last state
    });
    return () => controller.abort();
  }, [queryClient]);

  return live;
}
