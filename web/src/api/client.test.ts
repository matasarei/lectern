import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  apiFetch,
  EventError,
  followEvents,
  initToken,
  openSocket,
  readEvents,
  RECONNECT_DELAYS_MS,
} from "./client";

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

describe("followEvents", () => {
  const encoder = new TextEncoder();
  // A stream that sends `events` and then ends, as the server's does when it stops.
  const ending = (...events: object[]) =>
    new Response(new ReadableStream({
      start(c) {
        for (const e of events) c.enqueue(encoder.encode(`data: ${JSON.stringify(e)}\n\n`));
        c.close();
      },
    }));
  // One that stays open, as a running server's does.
  const open = (...events: object[]) =>
    new Response(new ReadableStream({
      start(c) {
        for (const e of events) c.enqueue(encoder.encode(`data: ${JSON.stringify(e)}\n\n`));
      },
    }));

  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("opens the stream again after it ends, and says so each time it opens", async () => {
    const answers = [ending({ seq: 1 }), open({ seq: 1 })];
    const fetched = vi.fn(async () => answers.shift()!);
    vi.stubGlobal("fetch", fetched);
    const events: unknown[] = [];
    let connects = 0;
    const controller = new AbortController();

    void followEvents("/api/x/events", (e) => events.push(e), controller.signal, () => connects++);
    await vi.waitFor(() => expect(events).toHaveLength(1));
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[0]);
    await vi.waitFor(() => expect(events).toHaveLength(2));

    expect(events).toEqual([{ seq: 1 }, { seq: 1 }]); // the restarted server numbers from 1 again
    expect(connects).toBe(2);
    expect(fetched).toHaveBeenCalledTimes(2);
    controller.abort();
  });

  it("keeps trying while the server can't be reached, waiting longer each time", async () => {
    const fetched = vi.fn(async () => {
      if (fetched.mock.calls.length < 3) throw new TypeError("Failed to fetch"); // restarting
      return open({ seq: 1 });
    });
    vi.stubGlobal("fetch", fetched);
    const events: unknown[] = [];
    const controller = new AbortController();

    void followEvents("/api/x/events", (e) => events.push(e), controller.signal);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[0]);
    expect(fetched).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[1] - 1);
    expect(fetched).toHaveBeenCalledTimes(2); // the second wait is longer
    await vi.advanceTimersByTimeAsync(1);
    await vi.waitFor(() => expect(events).toEqual([{ seq: 1 }]));
    controller.abort();
  });

  it("retries a 5xx, but stops on a 4xx, which a retry wouldn't change", async () => {
    const answers = [new Response("", { status: 503 }), new Response("", { status: 401 })];
    const fetched = vi.fn(async () => answers.shift()!);
    vi.stubGlobal("fetch", fetched);
    const controller = new AbortController();

    const refused = expect(followEvents("/api/x/events", () => {}, controller.signal)).rejects.toMatchObject({
      status: 401,
    });
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[0]);

    await refused;
    expect(fetched).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["its handler throws", () => open({ seq: 1 }), () => { throw new TypeError("a bug in the handler"); }],
    ["its JSON doesn't parse", () => new Response("data: {not json\n\n"), () => {}],
  ])("stops, rather than reconnecting into the same event, when %s", async (_why, answer, onEvent) => {
    const fetched = vi.fn(async () => answer());
    vi.stubGlobal("fetch", fetched);
    const controller = new AbortController();

    const stopped = expect(followEvents("/api/x/events", onEvent, controller.signal)).rejects.toBeInstanceOf(EventError);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS.at(-1)! * 2);

    await stopped;
    expect(fetched).toHaveBeenCalledTimes(1);
  });

  it("stops when aborted while waiting to reconnect", async () => {
    const fetched = vi.fn(async () => ending());
    vi.stubGlobal("fetch", fetched);
    const controller = new AbortController();

    const following = followEvents("/api/x/events", () => {}, controller.signal);
    await vi.waitFor(() => expect(fetched).toHaveBeenCalledTimes(1));
    controller.abort();

    await expect(following).resolves.toBeUndefined();
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS.at(-1)! * 2);
    expect(fetched).toHaveBeenCalledTimes(1);
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
