import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { useRuntime } from "./useRuntime";

let streams = 0;
let aborted = 0;

beforeEach(() => {
  streams = 0;
  aborted = 0;
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/runtime/events") {
        streams += 1;
        init?.signal?.addEventListener("abort", () => (aborted += 1));
        return new Response(new ReadableStream({ start: () => {} })); // stays open
      }
      return new Response(JSON.stringify({ residents: [], activity: null, hold: null }));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function Reader() {
  useRuntime();
  return null;
}

it("opens one runtime event stream however many components read the runtime", async () => {
  // Over HTTP/1.1 a browser allows six connections per host: one stream per reader (the
  // header, the chat, the OCR tab…) would use them up.
  const client = new QueryClient();
  const page = render(
    <QueryClientProvider client={client}>
      <Reader />
      <Reader />
      <Reader />
    </QueryClientProvider>,
  );
  await vi.waitFor(() => expect(streams).toBe(1));

  page.unmount();
  await vi.waitFor(() => expect(aborted).toBe(1)); // closed with the last reader
});
