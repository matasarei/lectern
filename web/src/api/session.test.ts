import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiFetch, getToken, setToken } from "./client";
import {
  askAgain,
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
