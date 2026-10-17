import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RECONNECT_DELAYS_MS } from "../api/client";
import { useOcrEvents } from "../project/ocr-api";
import { installFakeSockets } from "../test/fake-socket";
import { useDownloadEvents } from "./useDownloadEvents";
import { useRuntime } from "./useRuntime";

// A stream's reopen (the server restarted) is the moment to catch up on what changed while
// it was closed: each of these reads its queries again then, and only then.

beforeEach(() => {
  vi.useFakeTimers();
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}")));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** What a component on `hook` asks to read again: first while its stream is open, then once
 * the stream has closed (going away, 1001) and opened again. */
async function caughtUp(path: string, Hook: () => null): Promise<{ before: unknown[]; after: unknown[] }> {
  const sockets = installFakeSockets((socket, index) => {
    socket.open();
    if (index === 0) socket.drop(1001);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidated = vi.spyOn(client, "invalidateQueries");
  render(
    <QueryClientProvider client={client}>
      <Hook />
    </QueryClientProvider>,
  );
  await vi.waitFor(() => expect(sockets.to(path)).toHaveLength(1));
  const before = invalidated.mock.calls.map(([filters]) => filters?.queryKey);
  await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[0]);
  await vi.waitFor(() => expect(sockets.to(path)).toHaveLength(2));
  const after = invalidated.mock.calls.map(([filters]) => filters?.queryKey);
  return { before, after };
}

describe("Catching up when a stream opens again", () => {
  it("reads the runtime again", async () => {
    function Runtime() {
      useRuntime();
      return null;
    }
    const { before, after } = await caughtUp("/api/runtime/events", Runtime);
    expect(before).toEqual([]);
    expect(after).toEqual([["runtime"]]);
  });

  it("reads the models and first run again: a download may have ended meanwhile", async () => {
    function Downloads() {
      useDownloadEvents();
      return null;
    }
    const { before, after } = await caughtUp("/api/models/events", Downloads);
    expect(before).toEqual([]);
    expect(after).toEqual([["models"], ["first-run"]]);
  });

  it("reads the OCR session again: pages may have been read, or the job paused", async () => {
    function Ocr() {
      useOcrEvents(1, 2);
      return null;
    }
    const { before, after } = await caughtUp("/api/projects/1/ocr/2/events", Ocr);
    expect(before).toEqual([]);
    expect(after).toEqual([["ocr", 1, 2]]);
  });
});
