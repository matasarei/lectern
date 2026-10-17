import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routeTree } from "../router";

const LEASE = { id: 1, name: "Lease 1998", folder: "/p/Lease 1998", created_at: null };
const NOTES = { id: 2, name: "Notes", folder: "/p/Notes", created_at: null };
let listed: unknown[];
let removes: { url: string; body: unknown }[];
let refusal: [number, string] | null; // what DELETE answers instead of removing

beforeEach(() => {
  listed = [LEASE, NOTES];
  removes = [];
  refusal = null;
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      if (init.method === "DELETE") {
        removes.push({ url, body: JSON.parse(String(init.body)) });
        if (refusal) return new Response(JSON.stringify({ detail: refusal[1] }), { status: refusal[0] });
        listed = listed.filter((p) => `/api/projects/${(p as { id: number }).id}` !== url);
        return new Response(JSON.stringify({ removed: 1 }));
      }
      if (url === "/api/projects") return new Response(JSON.stringify(listed));
      return new Response("{}");
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderProjects() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/projects"] }) });
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

async function openRemove(name: string) {
  fireEvent.click(await screen.findByRole("button", { name: `Remove ${name}` }));
  const dialog = screen.getByRole("dialog", { name: `Remove ${name}?` });
  return {
    dialog,
    field: within(dialog).getByLabelText(`To confirm, type the project's name: ${name}`),
    remove: within(dialog).getByRole("button", { name: "Remove" }) as HTMLButtonElement,
  };
}

describe("Removing a project", () => {
  it("has a × on every project", async () => {
    renderProjects();
    expect(await screen.findByRole("button", { name: "Remove Lease 1998" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Remove Notes" })).toBeTruthy();
  });

  it("stays off until the name is typed exactly, case and spaces too", async () => {
    renderProjects();
    const { dialog, field, remove } = await openRemove("Lease 1998");
    expect(within(dialog).getByText(/moves to the trash folder beside your projects folder/)).toBeTruthy();

    for (const typed of ["", "lease 1998", "Lease 1998 ", " Lease 1998", "Lease"]) {
      fireEvent.change(field, { target: { value: typed } });
      expect(remove.disabled).toBe(true);
    }
    fireEvent.submit(field.closest("form")!); // Enter in the field doesn't get round it
    expect(removes).toEqual([]);

    fireEvent.change(field, { target: { value: "Lease 1998" } });
    expect(remove.disabled).toBe(false);
  });

  it("sends the name as typed, and the list loses the project", async () => {
    renderProjects();
    const { field, remove } = await openRemove("Lease 1998");
    fireEvent.change(field, { target: { value: "Lease 1998" } });
    fireEvent.click(remove);

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(removes).toEqual([{ url: "/api/projects/1", body: { name: "Lease 1998" } }]);
    await waitFor(() => expect(screen.queryByText("Lease 1998")).toBeNull());
    expect(screen.getByText("Notes")).toBeTruthy();
  });

  it("says why when the server refuses, and keeps the dialog open", async () => {
    refusal = [422, "Type the project's name exactly to remove it."];
    renderProjects();
    const { dialog, field, remove } = await openRemove("Notes");
    fireEvent.change(field, { target: { value: "Notes" } });
    fireEvent.click(remove);

    expect(await within(dialog).findByText("Type the project's name exactly to remove it.")).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getAllByText("Notes").length).toBeGreaterThan(0);
  });

  it("closes on Cancel and on Escape without removing anything", async () => {
    renderProjects();
    let { dialog } = await openRemove("Notes");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    ({ dialog } = await openRemove("Notes"));
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(removes).toEqual([]);
  });
});
