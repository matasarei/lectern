import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routeTree } from "../router";

const GIB = 1024 ** 3;
let savedTabs: unknown;
let puts: unknown[] = [];

const RESPONSES: Record<string, unknown> = {
  "/api/projects/1": { id: 1, name: "Lease archive 1998", folder: "/p", created_at: null },
  "/api/projects/1/items": {
    sessions: [
      { id: 1, kind: "chat", title: "Payment terms", messages: 4, created_at: "", updated_at: "" },
    ],
    artifacts: [
      {
        id: 7, name: "lease.md", kind: "imported", mime: "text/markdown", size: 10, text: "indexed",
        text_note: null, from: { kind: "import", id: null, note: null, title: null },
        created_at: "", modified_at: "",
      },
    ],
  },
  "/api/runtime": {
    tier: "recommended", budget: 7.4 * GIB, unified: false, used: 0, swaps: 0, waiting: 0,
    residents: [],
  },
};

beforeEach(() => {
  puts = [];
  savedTabs = { open: [{ kind: "session", id: 1 }], active: { kind: "session", id: 1 } };
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/events")) return new Response(new ReadableStream({ start: (c) => c.close() }));
      if (url === "/api/projects/1/tabs") {
        if (init?.method === "PUT") {
          savedTabs = JSON.parse(String(init.body));
          puts.push(savedTabs);
        }
        return new Response(JSON.stringify(savedTabs));
      }
      return new Response(JSON.stringify(RESPONSES[url] ?? {}));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderAt(path: string) {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("Project tabs", () => {
  it("shows Overview first, without a close button, then the saved tabs", async () => {
    renderAt("/projects/1");

    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Overview", "Payment terms"]);
    expect(tabs[1].getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByLabelText("Close Overview")).toBeNull();
    expect(screen.getByLabelText("Close Payment terms")).toBeTruthy();
    expect(screen.getByText("Lease archive 1998")).toBeTruthy();
    expect(await screen.findByText("7.4 GB · nothing loaded")).toBeTruthy();
  });

  it("saves the tabs when they change, and restores them on the next visit", async () => {
    const first = renderAt("/projects/1");
    fireEvent.click(await screen.findByRole("tab", { name: "Overview" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({ open: [{ kind: "session", id: 1 }], active: null });

    fireEvent.click(screen.getByLabelText("Close Payment terms"));
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1]).toEqual({ open: [], active: null });
    first.unmount();

    renderAt("/projects/1");
    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Overview"]);
  });
});
