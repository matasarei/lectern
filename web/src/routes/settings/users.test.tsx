import "../../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetSession, setSession } from "../../api/session";
import { UsersPage } from "./users";

vi.mock("@tanstack/react-router", () => ({
  Link: ({ to, children }: { to: string; children: ReactNode }) => <a href={to}>{children}</a>,
}));

const PASSWORD = "we walked to the lake in may";
type Person = { login: string; role: "admin" | "member"; disabled: boolean };
type Sent = { method: string; url: string; body: unknown };

/** The server's accounts, changed by what the page sends, as the server would. */
let people: Person[];
let sent: Sent[];
let refusal: [number, string] | null;

beforeEach(() => {
  people = [
    { login: "yevhen", role: "admin", disabled: false },
    { login: "olena", role: "member", disabled: false },
    { login: "taras", role: "member", disabled: true },
  ];
  sent = [];
  refusal = null;
  window.sessionStorage.setItem("lectern.token", "t");
  forgetSession();
  setSession({ accounts: true, user: { login: "yevhen", role: "admin" } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      if (method === "GET") return new Response(JSON.stringify(people));
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      sent.push({ method, url, body });
      if (refusal) return new Response(JSON.stringify({ detail: refusal[1] }), { status: refusal[0] });
      const login = decodeURIComponent(url.split("/")[3] ?? "");
      if (url === "/api/users") {
        const added: Person = {
          login: String(body.login).toLowerCase(),
          role: body.admin ? "admin" : "member",
          disabled: false,
        };
        people = [...people, added];
        return new Response(JSON.stringify(added), { status: 201 });
      }
      if (url.endsWith("/disabled")) {
        people = people.map((p) => (p.login === login ? { ...p, disabled: Boolean(body.disabled) } : p));
        return new Response(JSON.stringify(people.find((p) => p.login === login)));
      }
      return new Response(JSON.stringify({ login }));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <UsersPage />
    </QueryClientProvider>,
  );
}

const row = async (login: string) =>
  within((await screen.findByText(login)).closest("li") as HTMLElement);

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("Users", () => {
  it("lists everyone who can sign in, with what they are", async () => {
    renderPage();

    expect((await row("yevhen")).getByText(/Admin/)).toBeTruthy();
    expect((await row("olena")).getByText(/Member/)).toBeTruthy();
    expect((await row("taras")).getByText(/Disabled/)).toBeTruthy();
  });

  it("offers nothing against one's own account: that is Change password's", async () => {
    renderPage();

    const mine = await row("yevhen");
    expect(mine.getByText(/you/)).toBeTruthy();
    expect(mine.queryAllByRole("button")).toEqual([]);
  });

  it("adds a person, as a member unless told otherwise", async () => {
    renderPage();
    await row("olena");

    type("Name", "Marko");
    type("Password", PASSWORD);
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect((await row("marko")).getByText(/Member/)).toBeTruthy();
    expect(sent).toEqual([
      { method: "POST", url: "/api/users", body: { login: "Marko", password: PASSWORD, admin: false } },
    ]);
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
  });

  it("adds an admin when that is ticked", async () => {
    renderPage();
    await row("olena");

    type("Name", "marko");
    type("Password", PASSWORD);
    fireEvent.click(screen.getByLabelText(/Admin/));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect((await row("marko")).getByText(/Admin/)).toBeTruthy();
    expect(sent[0].body).toMatchObject({ admin: true });
  });

  it.each([
    [409, "The name 'olena' is already in use."],
    [400, "A password needs at least 15 characters."],
  ])("says why a person wasn't added (%i), and keeps what was typed", async (status, detail) => {
    refusal = [status, detail];
    renderPage();
    await row("olena");

    type("Name", "olena");
    type("Password", "short");
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect((await screen.findByRole("alert")).textContent).toBe(detail);
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("olena");
  });

  it("sends nothing without a name and a password", async () => {
    renderPage();
    await row("olena");

    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(sent).toEqual([]);
  });

  it("gives someone a new password, and says they were signed out", async () => {
    renderPage();
    const hers = await row("olena");

    fireEvent.click(hers.getByRole("button", { name: "New password for olena" }));
    fireEvent.change(hers.getByLabelText("New password for olena"), { target: { value: PASSWORD } });
    fireEvent.click(hers.getByRole("button", { name: "Save" }));

    expect(await hers.findByText(/signed out everywhere/)).toBeTruthy();
    expect(sent).toEqual([
      { method: "POST", url: "/api/users/olena/password", body: { password: PASSWORD } },
    ]);
    expect(hers.queryByRole("textbox")).toBeNull(); // the field is gone, the button is back
  });

  it("says why a new password wasn't taken, and leaves it to be changed", async () => {
    refusal = [400, "A password needs at least 15 characters."];
    renderPage();
    const hers = await row("olena");

    fireEvent.click(hers.getByRole("button", { name: "New password for olena" }));
    fireEvent.change(hers.getByLabelText("New password for olena"), { target: { value: "short" } });
    fireEvent.click(hers.getByRole("button", { name: "Save" }));

    expect((await screen.findByRole("alert")).textContent).toMatch(/at least 15 characters/);
    expect((hers.getByLabelText("New password for olena") as HTMLInputElement).value).toBe("short");
  });

  it("drops a new password that was begun and not wanted", async () => {
    renderPage();
    const hers = await row("olena");

    fireEvent.click(hers.getByRole("button", { name: "New password for olena" }));
    fireEvent.click(hers.getByRole("button", { name: "Cancel" }));

    expect(hers.queryByRole("textbox")).toBeNull(); // the field is gone, the button is back
    expect(sent).toEqual([]);
  });

  it("disables an account, and enables it again", async () => {
    renderPage();
    const hers = await row("olena");

    fireEvent.click(hers.getByRole("button", { name: "Disable olena" }));
    expect(await hers.findByText(/Disabled/)).toBeTruthy();

    fireEvent.click(hers.getByRole("button", { name: "Enable olena" }));
    await waitFor(() => expect(hers.queryByText(/Disabled/)).toBeNull());

    expect(sent).toEqual([
      { method: "POST", url: "/api/users/olena/disabled", body: { disabled: true } },
      { method: "POST", url: "/api/users/olena/disabled", body: { disabled: false } },
    ]);
  });

  it("says why an account couldn't be disabled", async () => {
    refusal = [409, "The only admin can't be disabled."];
    renderPage();
    const hers = await row("olena");

    fireEvent.click(hers.getByRole("button", { name: "Disable olena" }));

    expect((await screen.findByRole("alert")).textContent).toBe("The only admin can't be disabled.");
  });

  it("says so when the list can't be read", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "Only an admin can do that." }), { status: 403 })),
    );
    renderPage();

    expect((await screen.findByRole("alert")).textContent).toBe("Only an admin can do that.");
  });
});

describe("Users, for someone who can't manage them", () => {
  it("tells a member it is the admin's, and asks the server nothing", () => {
    setSession({ accounts: true, user: { login: "olena", role: "member" } });
    renderPage();

    expect(screen.getByText(/Only an admin can add people/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add" })).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("says how accounts are turned on, where nobody signs in yet", () => {
    setSession({ accounts: false, user: { login: null, role: "admin" } });
    renderPage();

    expect(screen.getByText(/lectern user init/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add" })).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });
});
