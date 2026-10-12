import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  apiFetch,
  apiPost,
  apiRaw,
  EventError,
  followEvents,
  getToken,
  initToken,
  onNotSignedIn,
  openSocket,
  RECONNECT_DELAYS_MS,
  setToken,
} from "./client";
import { type FakeSocket, installFakeSockets } from "../test/fake-socket";

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

describe("a request the server answers 401", () => {
  const refuses = (detail: string) =>
    vi.fn(async () => new Response(JSON.stringify({ detail }), { status: 401 }));

  afterEach(() => onNotSignedIn(() => {}));

  it("is said to whoever keeps track of the sign-in", async () => {
    const told = vi.fn();
    onNotSignedIn(told);
    vi.stubGlobal("fetch", refuses("Not signed in"));

    await expect(apiFetch("/api/projects")).rejects.toEqual(new ApiError(401, "Not signed in"));
    await expect(apiRaw("/api/projects/1/files/a.txt")).rejects.toBeInstanceOf(ApiError);

    expect(told).toHaveBeenCalledTimes(2);
  });

  it("isn't, when it is the sign-in itself that was refused", async () => {
    const told = vi.fn();
    onNotSignedIn(told);
    vi.stubGlobal("fetch", refuses("Sign-in failed"));

    await expect(apiPost("/api/login", { login: "olena", password: "x" })).rejects.toBeInstanceOf(
      ApiError,
    );
    await expect(apiFetch("/api/login")).rejects.toBeInstanceOf(ApiError);

    expect(told).not.toHaveBeenCalled();
  });
});

describe("setToken", () => {
  it("keeps the token for this tab, and forgets it", () => {
    setToken("abc");
    expect(getToken()).toBe("abc");
    setToken(null);
    expect(getToken()).toBeNull();
  });
});

describe("followEvents", () => {
  const PATH = "/api/x/events";

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // A socket that never opened makes the page ask over HTTP whether it is still let in.
    // Unless a test says otherwise, nothing answers: the server is down.
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
  });
  afterEach(() => vi.useRealTimers());

  it("opens a socket with the token, and hands on each frame as an event", async () => {
    window.sessionStorage.setItem("lectern.token", "t0k");
    const sockets = installFakeSockets((socket) => {
      socket.open();
      socket.push({ a: 1 }, { a: 2 });
    });
    const events: unknown[] = [];
    const controller = new AbortController();

    void followEvents(PATH, (e) => events.push(e), controller.signal);
    await vi.waitFor(() => expect(events).toEqual([{ a: 1 }, { a: 2 }]));

    expect(sockets.all).toHaveLength(1);
    expect(sockets.all[0].url).toBe(`ws://${window.location.host}${PATH}`);
    expect(sockets.all[0].protocols).toEqual(["lectern.v1", "lectern.token.t0k"]);
    controller.abort();
  });

  it("opens the socket again after it closes, and says so each time it opens", async () => {
    // The server stops (1001: going away) after one event, and is back for the next socket.
    const sockets = installFakeSockets((socket, index) => {
      socket.open();
      socket.push({ seq: 1 });
      if (index === 0) socket.drop(1001);
    });
    const events: unknown[] = [];
    const opens: boolean[] = [];
    const controller = new AbortController();

    void followEvents(PATH, (e) => events.push(e), controller.signal, (again) => opens.push(again));
    await vi.waitFor(() => expect(events).toHaveLength(1));
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[0]);
    await vi.waitFor(() => expect(events).toHaveLength(2));

    expect(events).toEqual([{ seq: 1 }, { seq: 1 }]); // the restarted server numbers from 1 again
    expect(opens).toEqual([false, true]); // only the reopen is told to catch up
    expect(sockets.all).toHaveLength(2);
    controller.abort();
  });

  it("keeps trying while the server can't be reached, waiting longer each time", async () => {
    const sockets = installFakeSockets((socket, index) => {
      if (index < 2) return socket.drop(); // restarting: nothing answers
      socket.open();
      socket.push({ seq: 1 });
    });
    const events: unknown[] = [];
    const controller = new AbortController();

    void followEvents(PATH, (e) => events.push(e), controller.signal);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[0]);
    expect(sockets.all).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[1] - 1);
    expect(sockets.all).toHaveLength(2); // the second wait is longer
    await vi.advanceTimersByTimeAsync(1);
    await vi.waitFor(() => expect(events).toEqual([{ seq: 1 }]));
    controller.abort();
  });

  it("retries the server's code for a 5xx, but stops on one for a 4xx, which a retry wouldn't change", async () => {
    const sockets = installFakeSockets((socket, index) => {
      socket.open();
      if (index === 0) socket.drop(4503, "Starting.");
      else socket.drop(4404, "No such session.");
    });
    const controller = new AbortController();

    const refused = expect(followEvents(PATH, () => {}, controller.signal)).rejects.toMatchObject({
      status: 404,
      detail: "No such session.",
    });
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[0]);

    await refused;
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS.at(-1)! * 2);
    expect(sockets.all).toHaveLength(2);
  });

  it.each([401, 403])(
    "gives up a socket refused before it opened when the server answers %i over HTTP",
    async (status) => {
      // A wrong token: the browser says only that the socket closed (1006), as when the
      // server is down. Asked over HTTP, the server says which it is.
      const sockets = installFakeSockets((socket) => socket.drop());
      const asked = vi.fn(async () =>
        new Response(JSON.stringify({ detail: "Missing or wrong token" }), { status }),
      );
      vi.stubGlobal("fetch", asked);
      window.sessionStorage.setItem("lectern.token", "stale");

      await expect(followEvents(PATH, () => {}, new AbortController().signal)).rejects.toMatchObject({
        status,
        detail: "Missing or wrong token",
      });

      await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS.at(-1)! * 2);
      expect(sockets.all).toHaveLength(1); // not tried again: nothing would change
      expect(asked).toHaveBeenCalledTimes(1);
      const [url, init] = asked.mock.calls[0] as unknown as [string, RequestInit];
      expect(url).toBe("/api/runtime");
      expect(new Headers(init.headers).get("X-Lectern-Token")).toBe("stale");
    },
  );

  it("opens the socket again, with the new token, when the login was replaced under this tab", async () => {
    // Signed in again in another tab of this browser (or a password changed there): this
    // tab's token is the old login's. Whoever keeps track of the sign-in is told of the 401,
    // asks who the browser is now, and has the new token by the time it answers.
    window.sessionStorage.setItem("lectern.token", "old");
    const sockets = installFakeSockets((socket) =>
      socket.protocols.includes("lectern.token.new") ? socket.open() : socket.drop(),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "Not signed in" }), { status: 401 })),
    );
    onNotSignedIn(async () => setToken("new"));
    const connected = vi.fn();
    const controller = new AbortController();

    const following = followEvents(PATH, () => {}, controller.signal, connected);
    await vi.waitFor(() => expect(connected).toHaveBeenCalledWith(false));

    expect(sockets.all).toHaveLength(2); // at once: no wait before the second
    controller.abort();
    await following;
    onNotSignedIn(() => {});
  });

  it("gives up a refused socket when asking again brought no new token: the login has ended", async () => {
    window.sessionStorage.setItem("lectern.token", "old");
    const sockets = installFakeSockets((socket) => socket.drop());
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "Not signed in" }), { status: 401 })),
    );
    const asked = vi.fn(async () => {});
    onNotSignedIn(asked);

    await expect(followEvents(PATH, () => {}, new AbortController().signal)).rejects.toMatchObject({
      status: 401,
    });

    expect(asked).toHaveBeenCalledTimes(1);
    expect(sockets.all).toHaveLength(1);
    onNotSignedIn(() => {});
  });

  it("goes on trying a socket that never opens while the server lets the page in, or can't be asked", async () => {
    const sockets = installFakeSockets((socket) => socket.drop());
    const answers = [() => Promise.reject(new TypeError("Failed to fetch")), () => new Response("{}")];
    const asked = vi.fn(async () => answers[Math.min(asked.mock.calls.length - 1, 1)]());
    vi.stubGlobal("fetch", asked);
    const controller = new AbortController();

    void followEvents(PATH, () => {}, controller.signal);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[0] + RECONNECT_DELAYS_MS[1]);

    expect(sockets.all).toHaveLength(3);
    controller.abort();
  });

  it("doesn't ask over HTTP about a socket that had opened", async () => {
    const sockets = installFakeSockets((socket, index) => {
      socket.open();
      if (index === 0) socket.drop(1001); // the server restarts
    });
    const controller = new AbortController();

    void followEvents(PATH, () => {}, controller.signal);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[0]);
    await vi.waitFor(() => expect(sockets.all).toHaveLength(2));

    expect(fetch).not.toHaveBeenCalled();
    controller.abort();
  });

  it.each([
    ["its handler throws", (socket: FakeSocket) => socket.push({ seq: 1 }), () => { throw new TypeError("a bug in the handler"); }],
    ["its JSON doesn't parse", (socket: FakeSocket) => socket.pushText("{not json"), () => {}],
  ])("stops, rather than reconnecting into the same event, when %s", async (_why, send, onEvent) => {
    const sockets = installFakeSockets((socket) => {
      socket.open();
      send(socket);
    });
    const controller = new AbortController();

    const stopped = expect(followEvents(PATH, onEvent, controller.signal)).rejects.toBeInstanceOf(EventError);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS.at(-1)! * 2);

    await stopped;
    expect(sockets.all).toHaveLength(1);
    expect(sockets.all[0].closedByPage).toBe(true);
  });

  it("leaves no abort listener behind from the sockets and waits it finished", async () => {
    const sockets = installFakeSockets((socket, index) => (index < 3 ? socket.drop() : socket.open()));
    const controller = new AbortController();
    const added = vi.spyOn(controller.signal, "addEventListener");
    const removed = vi.spyOn(controller.signal, "removeEventListener");

    void followEvents(PATH, () => {}, controller.signal);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS[0] + RECONNECT_DELAYS_MS[1] + RECONNECT_DELAYS_MS[2]);
    await vi.waitFor(() => expect(sockets.all).toHaveLength(4));

    const aborts = (calls: unknown[][]) => calls.filter(([type]) => type === "abort").length;
    // Four sockets and three waits listened; only the socket still open is still listening.
    expect(aborts(added.mock.calls)).toBe(7);
    expect(aborts(removed.mock.calls)).toBe(6);
    controller.abort();
  });

  it("closes its socket when aborted, and opens no other", async () => {
    const sockets = installFakeSockets();
    const controller = new AbortController();

    const following = followEvents(PATH, () => {}, controller.signal);
    await vi.waitFor(() => expect(sockets.all[0]?.readyState).toBe(1));
    controller.abort();

    await expect(following).resolves.toBeUndefined();
    expect(sockets.all[0].closedByPage).toBe(true);
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS.at(-1)! * 2);
    expect(sockets.all).toHaveLength(1);
  });

  it("stops when aborted while waiting to reconnect", async () => {
    const sockets = installFakeSockets((socket) => socket.drop());
    const controller = new AbortController();

    const following = followEvents(PATH, () => {}, controller.signal);
    await vi.waitFor(() => expect(sockets.all).toHaveLength(1));
    controller.abort();

    await expect(following).resolves.toBeUndefined();
    await vi.advanceTimersByTimeAsync(RECONNECT_DELAYS_MS.at(-1)! * 2);
    expect(sockets.all).toHaveLength(1);
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
