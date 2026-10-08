import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routeTree } from "../router";

const GIB = 1024 ** 3;
let savedTabs: unknown;
let puts: unknown[] = [];
let sessionsFail = false; // POST /sessions answers 500
let pushChat: (event: object) => void = () => {}; // the open chat's event stream

const RESPONSES: Record<string, unknown> = {
  "/api/projects/1": { id: 1, name: "Lease archive 1998", folder: "/p", created_at: null },
  "/api/projects/1/items": {
    sessions: [
      { id: 1, kind: "chat", title: "Payment terms", messages: 4, ocr: null, created_at: "", updated_at: "" },
      {
        id: 2, kind: "ocr", title: "lease-scan.pdf", messages: 0,
        ocr: { pages: 12, status: "done", folder: "lease-scan", document_version: 0, open_doubts: 0, figures: 0, failed_pages: [], clear: true },
        created_at: "", updated_at: "",
      },
    ],
    artifacts: [
      {
        id: 7, name: "lease.md", kind: "imported", mime: "text/markdown", size: 10, text: "indexed",
        text_note: null, from: { kind: "import", id: null, note: null, title: null },
        created_at: "", modified_at: "",
      },
    ],
  },
  "/api/projects/1/ocr/2": {
    id: 2, kind: "ocr", title: "lease-scan.pdf", folder: "lease-scan", status: "done", paused_reason: null,
    error: null, pages_total: 0, source_artifact_id: 7, updated_at: "", pages: [],
  },
  "/api/projects/1/sessions/1": {
    id: 1, kind: "chat", title: "Payment terms", state: "idle", pending: [], pinned: [], followups: [],
    summary: { upto: 0, version: 0, messages: 0 }, messages: [],
  },
  "/api/runtime": {
    tier: "recommended", budget: 7.4 * GIB, unified: false, used: 0, swaps: 0, waiting: 0,
    residents: [],
  },
};

beforeEach(() => {
  puts = [];
  sessionsFail = false;
  savedTabs = { open: [{ kind: "session", id: 1 }], active: { kind: "session", id: 1 } };
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/projects/1/sessions/1/events") {
        const encoder = new TextEncoder();
        return new Response(
          new ReadableStream({
            start(controller) {
              pushChat = (event) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
            },
          }),
        );
      }
      if (url.endsWith("/events")) return new Response(new ReadableStream({ start: (c) => c.close() }));
      if (url === "/api/projects/1/tabs") {
        if (init?.method === "PUT") {
          savedTabs = JSON.parse(String(init.body));
          puts.push(savedTabs);
        }
        return new Response(JSON.stringify(savedTabs));
      }
      if (url === "/api/projects/1/sessions" && init?.method === "POST") {
        if (sessionsFail) return new Response(JSON.stringify({ detail: "The disk is full." }), { status: 500 });
        return new Response(JSON.stringify({ id: 9, kind: "chat", title: "New chat", messages: 0 }));
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

describe("The tab bar for assistive tech", () => {
  it("holds only tabs: the + sits beside it, and the × buttons stay out of the way", async () => {
    renderAt("/projects/1");
    const bar = await screen.findByRole("tablist");
    expect(within(bar).queryAllByRole("button")).toEqual([]); // role queries skip aria-hidden
    expect(within(bar).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Overview", "Payment terms"]);
    expect(screen.getByLabelText("Close Payment terms").getAttribute("tabindex")).toBe("-1");
    expect(within(bar).queryByRole("button", { name: "New chat" })).toBeNull();
  });

  it("closes a tab with Delete, but not Overview", async () => {
    renderAt("/projects/1");
    const overview = await screen.findByRole("tab", { name: "Overview" });
    fireEvent.keyDown(overview, { key: "Delete" });
    const chat = screen.getByRole("tab", { name: "Payment terms" });
    expect(chat.getAttribute("aria-keyshortcuts")).toBe("Delete");
    expect(overview.hasAttribute("aria-keyshortcuts")).toBe(false);
    fireEvent.keyDown(chat, { key: "Delete" });
    await waitFor(() => expect(puts.at(-1)).toEqual({ open: [], active: null }));
    expect(puts).toHaveLength(1); // Delete on Overview did nothing
  });

  it("moves focus to the next tab after Delete, or the previous one when it was the last", async () => {
    savedTabs = {
      open: [{ kind: "session", id: 1 }, { kind: "artifact", id: 7 }],
      active: null,
    };
    renderAt("/projects/1");
    const chat = await screen.findByRole("tab", { name: "Payment terms" });
    chat.focus();
    fireEvent.keyDown(chat, { key: "Delete" });
    await waitFor(() => expect(screen.queryByRole("tab", { name: "Payment terms" })).toBeNull());
    expect(document.activeElement?.textContent).toBe("lease.md");

    fireEvent.keyDown(document.activeElement!, { key: "Delete" });
    await waitFor(() => expect(screen.queryByRole("tab", { name: "lease.md" })).toBeNull());
    expect(document.activeElement?.textContent).toBe("Overview");
  });
});

describe("The + in the tab bar", () => {
  it("creates a chat and opens it as the active tab", async () => {
    renderAt("/projects/1");
    fireEvent.click(await screen.findByRole("button", { name: "New chat" }));

    await waitFor(() => expect(puts.at(-1)).toEqual({
      open: [{ kind: "session", id: 1 }, { kind: "session", id: 9 }],
      active: { kind: "session", id: 9 },
    }));
    const posted = vi.mocked(fetch).mock.calls.filter(
      ([url, init]) => url === "/api/projects/1/sessions" && init?.method === "POST",
    );
    expect(posted).toHaveLength(1);
  });

  it("says why when the chat can't be created, and leaves the tabs as they were", async () => {
    sessionsFail = true;
    savedTabs = { open: [{ kind: "session", id: 1 }], active: null }; // on Overview: no chat to load
    renderAt("/projects/1");
    const bar = await screen.findByRole("navigation"); // Overview has a "New chat" of its own
    fireEvent.click(within(bar).getByRole("button", { name: "New chat" }));

    expect(await screen.findByText("The disk is full.")).toBeTruthy();
    expect(puts).toEqual([]);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Overview", "Payment terms"]);
    expect(within(bar).getByRole("button", { name: "New chat" }).hasAttribute("disabled")).toBe(false);
  });
});

describe("A tab the chat opens", () => {
  it("is added in the background when the tool says focus: false, and the items are read again", async () => {
    renderAt("/projects/1");
    expect(await screen.findByText("What are we working on?")).toBeTruthy(); // the chat is showing
    const itemReads = () =>
      vi.mocked(fetch).mock.calls.filter(([url]) => url === "/api/projects/1/items").length;
    const before = itemReads();
    await act(async () => {
      pushChat({ seq: 0, type: "replayed" });
      pushChat({
        seq: 1, type: "tool_result", call_id: "c1", text: "Started", error: false, refs: [],
        action: { type: "open_tab", tab: { kind: "session", id: 2 }, focus: false },
      });
      await new Promise((r) => setTimeout(r, 10));
    });

    await waitFor(() => expect(puts.at(-1)).toEqual({
      open: [{ kind: "session", id: 1 }, { kind: "session", id: 2 }],
      active: { kind: "session", id: 1 },
    }));
    expect(screen.getByRole("tab", { name: "Payment terms" }).getAttribute("aria-selected")).toBe("true");
    await waitFor(() => expect(itemReads()).toBeGreaterThan(before));
  });
});

describe("An OCR session's tab", () => {
  it("is named after its file and shows the OCR view, not a chat", async () => {
    savedTabs = { open: [{ kind: "session", id: 2 }], active: { kind: "session", id: 2 } };
    renderAt("/projects/1");

    expect(await screen.findByRole("tab", { name: "OCR · lease-scan.pdf" })).toBeTruthy();
    expect(await screen.findByText("Done")).toBeTruthy(); // the OCR status strip
    expect(screen.queryByRole("textbox")).toBeNull(); // no chat box
  });
});

describe("An OCR session's Document tab", () => {
  it("is named after the output folder and shows the Document view", async () => {
    savedTabs = { open: [{ kind: "document", id: 2 }], active: { kind: "document", id: 2 } };
    renderAt("/projects/1");

    expect(await screen.findByRole("tab", { name: "Document · lease-scan" })).toBeTruthy();
    expect(await screen.findByText("This session has no Document yet.")).toBeTruthy();
  });
});

describe("A tab for something the page hasn't heard of yet", () => {
  it("reads the project's items again and shows a new OCR session as one, not as a chat", async () => {
    savedTabs = { open: [{ kind: "session", id: 9 }], active: { kind: "session", id: 9 } };
    const items = RESPONSES["/api/projects/1/items"] as { sessions: object[] };
    const before = items.sessions;
    let reads = 0;
    const real = fetch as unknown as (url: string, init?: RequestInit) => Promise<Response>;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/projects/1/items" && ++reads > 1) {
        // The second read knows the session ocr.start just made.
        items.sessions = [...before, { id: 9, kind: "ocr", title: "scan.pdf", messages: 0,
          ocr: { pages: 1, status: "running", folder: "scan", document_version: 0, open_doubts: 0,
                 figures: 0 }, created_at: "", updated_at: "" }];
      }
      return real(url, init);
    }));
    try {
      renderAt("/projects/1");
      expect(await screen.findByRole("tab", { name: "OCR · scan.pdf" })).toBeTruthy();
      expect(reads).toBeGreaterThan(1);
      expect(screen.queryByRole("textbox")).toBeNull(); // never shown as a chat
    } finally {
      items.sessions = before;
    }
  });
});

describe("A tab for something that's gone", () => {
  it("says so once the items are read again, instead of loading forever", async () => {
    savedTabs = { open: [{ kind: "artifact", id: 99 }], active: { kind: "artifact", id: 99 } };
    renderAt("/projects/1");
    expect(await screen.findByText("This file is no longer in the project.")).toBeTruthy();
    expect(screen.queryByText("Loading…")).toBeNull();
  });
});
