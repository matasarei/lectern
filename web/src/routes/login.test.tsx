import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiFetch, getToken, setToken } from "../api/client";
import { forgetSession, getSession, keepInStep, setSession } from "../api/session";
import { AccountBar, PasswordPage, SignedIn } from "./login";

const router = vi.hoisted(() => ({ invalidate: vi.fn(), navigate: vi.fn() }));

vi.mock("@tanstack/react-router", () => ({
  useRouter: () => ({ invalidate: router.invalidate }),
  useNavigate: () => router.navigate,
  Link: ({ to, children }: { to: string; children: ReactNode }) => <a href={to}>{children}</a>,
}));

const OLENA = { login: "olena", role: "member" as const };
const WRONG =
  "Sign-in failed: the name or the password is wrong, or there were too many tries. " +
  "Wait a minute before trying again.";

type Sent = { method: string; url: string; body: unknown; token: string | null };
let sent: Sent[] = [];
/** What the server answers, by "METHOD path": a body, or [status, detail] for a refusal. */
let answers: Record<string, unknown> = {};
let queries: QueryClient;
let stop: () => void;

beforeEach(() => {
  sent = [];
  answers = {};
  router.invalidate.mockReset();
  router.navigate.mockReset();
  window.sessionStorage.clear();
  forgetSession();
  queries = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  stop = keepInStep(queries);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      sent.push({
        method,
        url,
        body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
        token: new Headers(init.headers).get("X-Lectern-Token"),
      });
      const answer = answers[`${method} ${url}`];
      if (answer === undefined) throw new TypeError("Failed to fetch");
      if (Array.isArray(answer)) {
        const [status, detail] = answer as [number, string];
        return new Response(JSON.stringify({ detail }), { status });
      }
      return new Response(JSON.stringify(answer));
    }),
  );
});

afterEach(() => {
  stop();
  cleanup();
  vi.unstubAllGlobals();
});

function show(page: ReactNode) {
  return render(<QueryClientProvider client={queries}>{page}</QueryClientProvider>);
}

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("Who gets in", () => {
  it("is everybody with the link's token, where nobody signs in", () => {
    setToken("from-the-link");
    show(<SignedIn>the page</SignedIn>);

    expect(screen.getByText("the page")).toBeTruthy();
  });

  it("is nobody without it, and the page says where the link is", () => {
    show(<SignedIn>the page</SignedIn>);

    expect(screen.queryByText("the page")).toBeNull();
    expect(screen.getByText(/Open Lectern from the link/)).toBeTruthy();
  });

  it("is whoever signed in, where people do", () => {
    setSession({ accounts: true, user: OLENA });
    show(<SignedIn>the page</SignedIn>);

    expect(screen.getByText("the page")).toBeTruthy();
  });

  it("is asked to sign in otherwise, even with a token left from an old login", () => {
    setToken("an-old-logins");
    setSession({ accounts: true, user: null });
    show(<SignedIn>the page</SignedIn>);

    expect(screen.queryByText("the page")).toBeNull();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeTruthy();
  });
});

describe("The sign-in form", () => {
  beforeEach(() => {
    setSession({ accounts: true, user: null });
  });

  it("signs the person in and shows them the page they came for", async () => {
    answers["POST /api/login"] = { user: OLENA, token: "the-logins-token" };
    queries.setQueryData(["projects"], [{ id: 7, name: "somebody else's" }]);
    show(<SignedIn>the page</SignedIn>);

    type("Name", "Olena");
    type("Password", "we walked to the lake in may");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("the page")).toBeTruthy();
    expect(sent).toEqual([
      {
        method: "POST",
        url: "/api/login",
        body: { login: "Olena", password: "we walked to the lake in may" },
        token: null,
      },
    ]);
    expect(getToken()).toBe("the-logins-token");
    expect(getSession()).toEqual({ accounts: true, user: OLENA });
    expect(queries.getQueryData(["projects"])).toBeUndefined(); // nothing of whoever was here
    expect(router.invalidate).toHaveBeenCalled(); // "/" then goes where it leads
  });

  it("says what the server said about a wrong password, and keeps the name", async () => {
    answers["POST /api/login"] = [401, WRONG];
    show(<SignedIn>the page</SignedIn>);

    type("Name", "olena");
    type("Password", "not her password at all");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect((await screen.findByRole("alert")).textContent).toBe(WRONG);
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("olena");
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
    expect(screen.queryByText("the page")).toBeNull();
    expect(getToken()).toBeNull();
  });

  it("says so when the server doesn't answer", async () => {
    show(<SignedIn>the page</SignedIn>);

    type("Name", "olena");
    type("Password", "we walked to the lake in may");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect((await screen.findByRole("alert")).textContent).toMatch(/Lectern didn't answer/);
  });

  it("asks for both before it sends anything", () => {
    show(<SignedIn>the page</SignedIn>);

    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(sent).toEqual([]);
    expect((screen.getByLabelText("Password") as HTMLInputElement).type).toBe("password");
  });

  it("is where a 401 in the middle of things leads", async () => {
    setSession({ accounts: true, user: OLENA });
    setToken("t1");
    answers["GET /api/projects"] = [401, "Not signed in"];
    answers["GET /api/login"] = { accounts: true, user: null, token: null };
    show(<SignedIn>the page</SignedIn>);
    expect(screen.getByText("the page")).toBeTruthy();

    await apiFetch("/api/projects").catch(() => {});

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(screen.queryByText("the page")).toBeNull();
  });
});

describe("The account line", () => {
  it("isn't there where nobody signs in", () => {
    setToken("from-the-link");
    setSession({ accounts: false, user: { login: null, role: "admin" } });
    const { container } = show(<AccountBar />);

    expect(container.textContent).toBe("");
  });

  it("names who is signed in, and signs them out", async () => {
    setSession({ accounts: true, user: OLENA });
    setToken("t1");
    answers["POST /api/logout"] = { signed_out: true };
    queries.setQueryData(["projects"], [{ id: 1 }]);
    show(<AccountBar />);
    expect(screen.getByText(/Signed in as olena/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Change password" }).getAttribute("href")).toBe(
      "/settings/password",
    );

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(getSession()).toEqual({ accounts: true, user: null }));
    expect(sent[0]).toMatchObject({ method: "POST", url: "/api/logout", token: "t1" });
    expect(getToken()).toBeNull();
    expect(queries.getQueryData(["projects"])).toBeUndefined();
    expect(router.navigate).toHaveBeenCalledWith({ to: "/" });
  });

  it("stays signed in, and says so, when the server can't be told", async () => {
    setSession({ accounts: true, user: OLENA });
    setToken("t1");
    show(<AccountBar />);

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    // The cookie is the server's to remove: with it left, a reload would sign her back in.
    expect((await screen.findByRole("alert")).textContent).toMatch(/still signed in/);
    expect(getSession().user).toEqual(OLENA);
    expect(getToken()).toBe("t1");
  });

  it("says the page was opened with the computer's link, which has no password", () => {
    setSession({ accounts: true, user: { login: null, role: "admin" } });
    show(<AccountBar />);

    expect(screen.getByText(/this computer's link/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Change password" })).toBeNull();
  });
});

describe("Changing one's password", () => {
  const OLD = "we walked to the lake in may";
  const NEW = "and came back before the rain";

  beforeEach(() => {
    setSession({ accounts: true, user: OLENA });
    setToken("t1");
  });

  function fill(current: string, fresh: string, again: string) {
    type("Current password", current);
    type("New password", fresh);
    type("New password, again", again);
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
  }

  it("sends both, and keeps this browser signed in with its new token", async () => {
    answers["POST /api/password"] = { token: "t2" };
    queries.setQueryData(["projects"], [{ id: 1 }]);
    show(<PasswordPage />);

    fill(OLD, NEW, NEW);

    expect(await screen.findByText(/Your password is changed/)).toBeTruthy();
    expect(sent).toEqual([
      { method: "POST", url: "/api/password", body: { current: OLD, new: NEW }, token: "t1" },
    ]);
    expect(getToken()).toBe("t2");
    expect(getSession().user).toEqual(OLENA);
    expect(queries.getQueryData(["projects"])).toEqual([{ id: 1 }]); // still hers
    expect((screen.getByLabelText("Current password") as HTMLInputElement).value).toBe("");
  });

  it("doesn't send two new passwords that differ", () => {
    show(<PasswordPage />);

    fill(OLD, NEW, `${NEW}.`);

    expect(screen.getByRole("alert").textContent).toMatch(/aren't the same/);
    expect(sent).toEqual([]);
  });

  it.each([
    [403, "The current password is wrong."],
    [400, "A password needs at least 15 characters."],
  ])("says why the server refused (%i)", async (status, detail) => {
    answers["POST /api/password"] = [status, detail];
    show(<PasswordPage />);

    fill(OLD, NEW, NEW);

    expect((await screen.findByRole("alert")).textContent).toBe(detail);
    expect(getToken()).toBe("t1");
  });
});
