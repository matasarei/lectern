import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { type FakeSockets, installFakeSockets } from "../test/fake-socket";
import { useRuntime } from "./useRuntime";

let sockets: FakeSockets;

beforeEach(() => {
  window.sessionStorage.setItem("lectern.token", "t");
  sockets = installFakeSockets(); // each stays open
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ residents: [], activity: null, hold: null }))),
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
  // One socket and one listener on the server, not one per reader (the header, the chat,
  // the OCR tab…).
  const client = new QueryClient();
  const page = render(
    <QueryClientProvider client={client}>
      <Reader />
      <Reader />
      <Reader />
    </QueryClientProvider>,
  );
  await vi.waitFor(() => expect(sockets.to("/api/runtime/events")).toHaveLength(1));
  expect(sockets.all).toHaveLength(1);

  page.unmount();
  await vi.waitFor(() => expect(sockets.all[0].closedByPage).toBe(true)); // with the last reader
});
