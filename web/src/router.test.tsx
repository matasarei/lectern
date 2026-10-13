import "./i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetSession, keepInStep } from "./api/session";
import { routeTree } from "./router";
import { installFakeSockets } from "./test/fake-socket";

const OLENA = { login: "olena", role: "member" };
const TARAS = { login: "taras", role: "admin" };

/** The server: who it says the browser is, who the form signs in, and how far setup has got
 * ("unknown": it can't say). */
let who: { accounts: boolean; user: object | null; token: string | null } | null;
let signsIn: object;
let setup: "hardware" | "done" | "unknown";
let asked: string[];

beforeEach(() => {
  who = { accounts: true, user: null, token: null };
  signsIn = OLENA;
  setup = "done";
  asked = [];
  window.sessionStorage.clear();
  forgetSession();
  installFakeSockets();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      asked.push(`${init.method ?? "GET"} ${url}`);
      const refused = (detail: string) => new Response(JSON.stringify({ detail }), { status: 401 });
      if (url === "/api/login" && init.method === "POST") {
        who = { accounts: true, user: signsIn, token: "the-logins-token" };
        return new Response(JSON.stringify({ user: signsIn, token: who.token }));
      }
      if (url === "/api/login") {
        return who ? new Response(JSON.stringify(who)) : refused("Missing or wrong token");
      }
      if (who?.accounts && !who.user) return refused("Not signed in");
      if (url === "/api/first-run" && setup === "unknown") {
        return new Response(JSON.stringify({ detail: "The database is busy." }), { status: 500 });
      }
      if (url === "/api/first-run") return new Response(JSON.stringify({ step: setup }));
      return new Response(JSON.stringify([]));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function signIn(name: string) {
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: name } });
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: "we walked to the lake in may" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

/** The app as the browser opens it at `path`. */
function open(path: string) {
  const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  keepInStep(queries);
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  render(
    <QueryClientProvider client={queries}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

describe("The bare address, where people sign in", () => {
  it("asks someone who isn't signed in to sign in, and asks the server for nothing else", async () => {
    open("/");

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(asked).toEqual(["GET /api/login"]);
  });

  it("leads on to the projects once they have", async () => {
    const router = open("/");
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "olena" } });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "we walked to the lake in may" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeTruthy();
    expect(router.state.location.pathname).toBe("/projects");
    expect(screen.getByText("Signed in as olena")).toBeTruthy();
  });

  it("takes a browser that is signed in straight to the projects, in a new tab too", async () => {
    who = { accounts: true, user: OLENA, token: "the-logins-token" };

    open("/");

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeTruthy();
    expect(window.sessionStorage.getItem("lectern.token")).toBe("the-logins-token");
  });

  it("shows the form at the address that was asked for, and that page after signing in", async () => {
    const router = open("/settings/password");
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "olena" } });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "we walked to the lake in may" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Change your password" })).toBeTruthy();
    expect(router.state.location.pathname).toBe("/settings/password");
  });

  it("tells a member that setup isn't done, and lets them in when it is", async () => {
    who = { accounts: true, user: OLENA, token: "the-logins-token" };
    setup = "hardware";
    open("/");
    expect(await screen.findByRole("heading", { name: "Lectern isn't set up yet" })).toBeTruthy();

    setup = "done";
    fireEvent.click(screen.getByRole("link", { name: "Check again" }));

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeTruthy();
  });
});

describe("The projects, opened by their own address before setup is done", () => {
  beforeEach(() => {
    setup = "hardware";
  });

  it("lead an admin who signs in there to setup, as the bare address would", async () => {
    signsIn = TARAS;
    const router = open("/projects");
    await screen.findByLabelText("Name");
    signIn("taras");

    await waitFor(() => expect(router.state.location.pathname).toBe("/first-run"));
    expect(screen.queryByRole("heading", { name: "Projects" })).toBeNull();
  });

  it("lead an admin already signed in to setup too", async () => {
    who = { accounts: true, user: TARAS, token: "the-logins-token" };

    const router = open("/projects");

    await waitFor(() => expect(router.state.location.pathname).toBe("/first-run"));
  });

  it("are shown to an admin once setup is done", async () => {
    setup = "done";
    signsIn = TARAS;
    const router = open("/projects");
    await screen.findByLabelText("Name");
    signIn("taras");

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeTruthy();
    expect(router.state.location.pathname).toBe("/projects");
  });

  it("are shown to an admin when the server can't say how far setup is", async () => {
    setup = "unknown";
    who = { accounts: true, user: TARAS, token: "the-logins-token" };

    const router = open("/projects");

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeTruthy();
    expect(router.state.location.pathname).toBe("/projects");
  });

  it("are shown to a member, whose setup it isn't, without asking how far it is", async () => {
    const router = open("/projects");
    await screen.findByLabelText("Name");
    signIn("olena");

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeTruthy();
    expect(router.state.location.pathname).toBe("/projects");
    expect(asked).not.toContain("GET /api/first-run");
  });

  it("are shown where nobody signs in, as before accounts", async () => {
    who = { accounts: false, user: { login: null, role: "admin" }, token: null };
    window.sessionStorage.setItem("lectern.token", "from-the-link");

    const router = open("/projects");

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeTruthy();
    expect(router.state.location.pathname).toBe("/projects");
    expect(asked).not.toContain("GET /api/first-run");
  });
});

describe("The bare address, where nobody signs in", () => {
  beforeEach(() => {
    who = { accounts: false, user: { login: null, role: "admin" }, token: null };
  });

  it("leads whoever opened the computer's link to the projects, as before accounts", async () => {
    window.sessionStorage.setItem("lectern.token", "from-the-link");

    open("/");

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeTruthy();
    expect(screen.queryByText(/Signed in as/)).toBeNull();
  });

  it("says where the link is to someone without it", async () => {
    who = null; // no token: the server refuses even to say who this is

    open("/");

    expect(await screen.findByText(/Open Lectern from the link/)).toBeTruthy();
    expect(asked).toEqual(["GET /api/login"]);
  });
});
