import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, initToken, openSocket, readEvents } from "./client";

beforeEach(() => {
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("initToken", () => {
  it("takes the token from the fragment and removes it from the address bar", () => {
    window.history.replaceState(null, "", "/settings?x=1#token=abc123");

    expect(initToken()).toBe("abc123");
    expect(window.location.hash).toBe("");
    expect(window.location.pathname + window.location.search).toBe("/settings?x=1");
    expect(window.sessionStorage.getItem("lectern.token")).toBe("abc123");
  });

  it("keeps other fragment values", () => {
    window.history.replaceState(null, "", "/#token=abc&tab=models");

    initToken();

    expect(window.location.hash).toBe("#tab=models");
  });

  it("returns the stored token when the URL has none", () => {
    window.sessionStorage.setItem("lectern.token", "stored");
    expect(initToken()).toBe("stored");
  });

  it("returns null when there is no token anywhere", () => {
    expect(initToken()).toBeNull();
  });
});

describe("apiFetch", () => {
  it("sends the token header", async () => {
    window.sessionStorage.setItem("lectern.token", "t0k");
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/api/system");

    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(new Headers(init.headers).get("X-Lectern-Token")).toBe("t0k");
  });

  it("turns an error response into ApiError with the server's detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "Missing or wrong token" }), { status: 401 })),
    );

    await expect(apiFetch("/api/system")).rejects.toEqual(new ApiError(401, "Missing or wrong token"));
  });
});

describe("readEvents", () => {
  it("parses data lines and skips comments, across chunk boundaries", async () => {
    const chunks = [": connected\n\n", 'data: {"a":', '1}\n\ndata: {"a":2}\n\n'];
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const c of chunks) controller.enqueue(new TextEncoder().encode(c));
        controller.close();
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body)));
    const events: unknown[] = [];

    await readEvents("/api/models/events", (e) => events.push(e), new AbortController().signal);

    expect(events).toEqual([{ a: 1 }, { a: 2 }]);
  });
});

describe("openSocket", () => {
  it("offers the protocol and the token as subprotocols", () => {
    window.sessionStorage.setItem("lectern.token", "t0k");
    const sockets: Array<{ url: string; protocols: string[] }> = [];
    vi.stubGlobal(
      "WebSocket",
      class {
        constructor(url: string, protocols: string[]) {
          sockets.push({ url, protocols });
        }
      },
    );

    openSocket("/api/ws/ping");

    expect(sockets[0].url).toBe(`ws://${window.location.host}/api/ws/ping`);
    expect(sockets[0].protocols).toEqual(["lectern.v1", "lectern.token.t0k"]);
  });
});
