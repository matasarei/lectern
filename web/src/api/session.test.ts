import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiFetch, getToken, setToken } from "./client";
import {
  askAgain,
  followOtherTabs,
  forgetSession,
  getSession,
  keepInStep,
  setSession,
  signedIn,
  signedOut,
  whoAmI,
} from "./session";

const OLENA = { login: "olena", role: "member" as const };
const ok = (body: unknown) => new Response(JSON.stringify(body));
const refused = (detail: string) => new Response(JSON.stringify({ detail }), { status: 401 });

/** The server: what `GET /api/login` answers now, and 401 for everything else when told to. */
let login: () => Response;
let signedOutThere = false;
let asked = 0;

beforeEach(() => {
  window.sessionStorage.clear();
  forgetSession();
  signedOutThere = false;
  asked = 0;
  login = () => ok({ accounts: false, user: { login: null, role: "admin" }, token: null });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/login") {
        asked += 1;
        return login();
      }
      return signedOutThere ? refused("Not signed in") : ok([]);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("who the page is", () => {
  it("is asked once, at the start", async () => {
    setToken("from-the-link");

    expect(await whoAmI()).toEqual({ accounts: false, user: { login: null, role: "admin" } });
    await whoAmI();

    expect(asked).toBe(1);
    expect(getToken()).toBe("from-the-link"); // the answer had none: the link's stays
  });

  it("a new tab picks its token up: the cookie says who, the answer brings the token", async () => {
    login = () => ok({ accounts: true, user: OLENA, token: "this-logins-token" });

    expect(await whoAmI()).toEqual({ accounts: true, user: OLENA });

    expect(getToken()).toBe("this-logins-token");
    expect(getSession().user).toEqual(OLENA);
  });

  it("is nobody where people sign in and this browser hasn't", async () => {
    login = () => ok({ accounts: true, user: null, token: null });

    expect(await whoAmI()).toEqual({ accounts: true, user: null });
  });

  it("is nobody, as before accounts, when the link's token is missing or wrong", async () => {
    login = () => refused("Missing or wrong token");

    expect(await whoAmI()).toEqual({ accounts: false, user: null });
  });

  it("is asked again at the next page if the server couldn't be reached", async () => {
    login = () => {
      throw new TypeError("Failed to fetch");
    };
    expect(await whoAmI()).toEqual({ accounts: false, user: null });

    login = () => ok({ accounts: true, user: OLENA, token: "t" });
    expect((await whoAmI()).user).toEqual(OLENA);
  });
});

describe("a 401 in the middle of things", () => {
  beforeEach(async () => {
    login = () => ok({ accounts: true, user: OLENA, token: "t1" });
    await whoAmI();
  });

  it("returns the page to the sign-in when the login has ended", async () => {
    const queries = new QueryClient();
    const stop = keepInStep(queries);
    queries.setQueryData(["projects"], [{ id: 1 }]);
    signedOutThere = true;
    login = () => ok({ accounts: true, user: null, token: null });

    await expect(apiFetch("/api/projects")).rejects.toMatchObject({ status: 401 });
    await vi.waitFor(() => expect(getSession()).toEqual({ accounts: true, user: null }));

    expect(queries.getQueryData(["projects"])).toBeUndefined();
    stop();
  });

  it("asks once for a burst of them", async () => {
    signedOutThere = true;
    login = () => ok({ accounts: true, user: null, token: null });
    const before = asked;

    await Promise.allSettled([apiFetch("/api/a"), apiFetch("/api/b"), apiFetch("/api/c")]);
    await vi.waitFor(() => expect(getSession().user).toBeNull());

    expect(asked).toBe(before + 1);
  });

  it("takes the new token when this browser signed in again in another tab", async () => {
    login = () => ok({ accounts: true, user: OLENA, token: "t2" });

    expect(await askAgain()).toEqual({ accounts: true, user: OLENA });

    expect(getToken()).toBe("t2");
  });

  it("keeps the person signed in through a moment without the server", async () => {
    login = () => {
      throw new TypeError("Failed to fetch");
    };

    expect((await askAgain()).user).toEqual(OLENA);
  });
});

describe("what the page has fetched", () => {
  it("goes when the person does: the next one mustn't see it", async () => {
    setSession({ accounts: true, user: OLENA });
    const queries = new QueryClient();
    const stop = keepInStep(queries);
    queries.setQueryData(["projects"], [{ id: 1, name: "Olena's" }]);

    signedOut();

    expect(queries.getQueryData(["projects"])).toBeUndefined();
    expect(getToken()).toBeNull();
    stop();
  });

  it("is fetched again when the token changed under it", async () => {
    setSession({ accounts: true, user: OLENA });
    setToken("t1");
    const queries = new QueryClient();
    const stop = keepInStep(queries);
    queries.setQueryData(["projects"], [{ id: 1 }]);
    login = () => ok({ accounts: true, user: OLENA, token: "t2" });

    await askAgain();

    expect(queries.getQueryData(["projects"])).toBeUndefined();
    stop();
  });

  it("stays when nothing changed", async () => {
    login = () => ok({ accounts: true, user: OLENA, token: "t1" });
    await whoAmI();
    const queries = new QueryClient();
    const stop = keepInStep(queries);
    queries.setQueryData(["projects"], [{ id: 1 }]);

    await askAgain();

    expect(queries.getQueryData(["projects"])).toEqual([{ id: 1 }]);
    stop();
  });

  it("is kept in step from the sign-in on", () => {
    signedIn({ user: OLENA, token: "t9" });

    expect(getSession()).toEqual({ accounts: true, user: OLENA });
    expect(getToken()).toBe("t9");
  });
});

/** The tabs of one browser: what one says, the others on that channel hear (never itself). */
class FakeChannel {
  static open: FakeChannel[] = [];
  private heard: ((event: { data: unknown }) => void)[] = [];

  constructor(readonly name: string) {
    FakeChannel.open.push(this);
  }

  postMessage(data: unknown) {
    for (const other of FakeChannel.open) {
      if (other !== this && other.name === this.name) other.heard.forEach((hear) => hear({ data }));
    }
  }

  addEventListener(_type: "message", hear: (event: { data: unknown }) => void) {
    this.heard.push(hear);
  }

  close() {
    FakeChannel.open = FakeChannel.open.filter((channel) => channel !== this);
  }
}

describe("the other tabs of this browser", () => {
  beforeEach(() => {
    FakeChannel.open = [];
    vi.stubGlobal("BroadcastChannel", FakeChannel);
  });

  it("ask again who they are when one of them signs in or out", async () => {
    const stop = followOtherTabs();
    await whoAmI(); // this tab: nobody signs in here, as far as it knows
    login = () => ok({ accounts: true, user: OLENA, token: "this-logins-token" });

    new FakeChannel("lectern.session").postMessage("changed"); // another tab signed in

    await vi.waitFor(() => expect(getSession()).toEqual({ accounts: true, user: OLENA }));
    expect(getToken()).toBe("this-logins-token");
    stop();
  });

  it("are told when this one signs in and when it signs out, and it doesn't ask itself", () => {
    const stop = followOtherTabs();
    const heard: unknown[] = [];
    new FakeChannel("lectern.session").addEventListener("message", (event) => heard.push(event.data));

    signedIn({ user: OLENA, token: "this-logins-token" });
    expect(heard).toHaveLength(1);
    signedOut();
    expect(heard).toHaveLength(2);

    expect(heard.join()).not.toContain("this-logins-token"); // only that something changed
    expect(asked).toBe(0);
    stop();
  });

  it("are told when this one learns from the server that its login has ended", async () => {
    // Signed out, given a new password or disabled somewhere else: this tab finds out at a
    // refused request or a closed socket, and asks. A tab with nothing to ask wouldn't.
    const stop = followOtherTabs();
    login = () => ok({ accounts: true, user: OLENA, token: "this-logins-token" });
    await whoAmI();
    const heard: unknown[] = [];
    new FakeChannel("lectern.session").addEventListener("message", (event) => heard.push(event.data));
    login = () => ok({ accounts: true, user: null, token: null });

    await askAgain();
    expect(getSession().user).toBeNull();
    expect(heard).toEqual(["changed"]);

    await askAgain(); // still nobody: nothing new to tell
    expect(heard).toEqual(["changed"]);
    stop();
  });

  it("pass it on, once, when they were told and find their own login has ended", async () => {
    const stop = followOtherTabs();
    login = () => ok({ accounts: true, user: OLENA, token: "this-logins-token" });
    await whoAmI();
    const other = new FakeChannel("lectern.session");
    const heard: unknown[] = [];
    other.addEventListener("message", (event) => heard.push(event.data));
    login = () => ok({ accounts: true, user: null, token: null });

    other.postMessage("changed"); // that tab's login ended, and so has this one's
    await vi.waitFor(() => expect(getSession().user).toBeNull());
    expect(heard).toEqual(["changed"]);

    other.postMessage("changed"); // a third tab passing it on: nobody here already
    await vi.waitFor(() => expect(asked).toBe(3));
    expect(heard).toEqual(["changed"]);
    stop();
  });

  it("aren't told what the server says when nobody's login has ended", async () => {
    const stop = followOtherTabs();
    const heard: unknown[] = [];
    new FakeChannel("lectern.session").addEventListener("message", (event) => heard.push(event.data));
    login = () => ok({ accounts: true, user: null, token: null });
    await whoAmI(); // a tab opened with nobody signed in: nobody stopped being anybody

    login = () => ok({ accounts: true, user: OLENA, token: "this-logins-token" });
    await askAgain(); // signed in elsewhere: the tab that did it has said so

    login = () => ok({ accounts: true, user: OLENA, token: "another-logins-token" });
    await askAgain(); // the login replaced under this tab: the same

    expect(getToken()).toBe("another-logins-token");
    expect(heard).toEqual([]);
    stop();
  });

  it("aren't listened to, or told, once stopped", async () => {
    followOtherTabs()();
    const other = new FakeChannel("lectern.session");
    const heard: unknown[] = [];
    other.addEventListener("message", (event) => heard.push(event.data));

    other.postMessage("changed");
    signedIn({ user: OLENA, token: "this-logins-token" });
    await Promise.resolve();

    expect(asked).toBe(0);
    expect(heard).toEqual([]);
  });

  it("make no difference in a browser that has no such channel", () => {
    vi.stubGlobal("BroadcastChannel", undefined);
    const stop = followOtherTabs();

    signedIn({ user: OLENA, token: "this-logins-token" });

    expect(getSession().user).toEqual(OLENA);
    stop();
  });
});
