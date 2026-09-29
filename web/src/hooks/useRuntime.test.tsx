import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { REFRESH_DEBOUNCE_MS, useRuntime } from "./useRuntime";

afterEach(() => vi.unstubAllGlobals());

it("refetches once for a burst of runtime events", async () => {
  const burst = Array.from({ length: 40 }, (_, i) =>
    `data: ${JSON.stringify({ type: "runtime", event: i % 2 ? "release" : "lease" })}\n\n`,
  ).join("");
  const runtimeFetches: number[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/runtime/events") {
        return new Response(
          new ReadableStream({
            start(c) {
              c.enqueue(new TextEncoder().encode(burst));
            },
          }),
        );
      }
      runtimeFetches.push(Date.now());
      return new Response(JSON.stringify({ residents: [], swaps: 0 }));
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );

  const { result } = renderHook(() => useRuntime(), { wrapper });

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  await new Promise((r) => setTimeout(r, REFRESH_DEBOUNCE_MS * 3));
  // The first load, plus one refetch for all 40 events.
  expect(runtimeFetches).toHaveLength(2);
});
