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
let pushProject: (event: object) => void = () => {}; // the project's event stream
let endChat: () => void = () => {}; // the server stops (a restart): the chat's stream ends

function stream(onStart: (push: (event: object) => void, end: () => void) => void): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        onStart(
          (event) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`)),
          () => controller.close(),
        );
      },
    }),
  );
}

const RESPONSES: Record<string, unknown> = {
  "/api/projects": [],
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
        return stream((push, end) => {
          pushChat = push;
          endChat = end;
        });
      }
      if (url === "/api/projects/1/events") return stream((push) => (pushProject = push));
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

describe("The header", () => {
  it("goes back to Projects inside the app, without reloading it", async () => {
    renderAt("/projects/1");
    const back = await screen.findByRole("link", { name: "Projects" });
    expect(back.getAttribute("href")).toBe("/projects");
    fireEvent.click(back);

    expect(await screen.findByRole("heading", { name: "Projects" })).toBeTruthy();
  });

  it("names Models & memory as the projects list does", async () => {
    renderAt("/projects/1");
    const settings = await screen.findByRole("link", { name: "Models & memory" });
    expect(settings.getAttribute("href")).toBe("/settings/models");
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

describe("A tab a chat opens while it isn't showing", () => {
  const exported = { seq: 1, type: "open_tab", session_id: 1, tab: { kind: "artifact", id: 7 } };

  it("is added from the project's stream, and the tab you're on stays active", async () => {
    savedTabs = { open: [{ kind: "session", id: 1 }, { kind: "session", id: 2 }], active: { kind: "session", id: 2 } };
    renderAt("/projects/1");
    expect(await screen.findByRole("tab", { name: "OCR · lease-scan.pdf" })).toBeTruthy();
    await act(async () => {
      pushProject({ seq: 0, type: "replayed" });
      pushProject(exported);
      await new Promise((r) => setTimeout(r, 10));
    });

    await waitFor(() => expect(puts.at(-1)).toEqual({
      open: [{ kind: "session", id: 1 }, { kind: "session", id: 2 }, { kind: "artifact", id: 7 }],
      active: { kind: "session", id: 2 },
    }));
    expect(screen.getByRole("tab", { name: "OCR · lease-scan.pdf" }).getAttribute("aria-selected")).toBe("true");
  });

  it("is added while Overview is showing, and Overview stays", async () => {
    savedTabs = { open: [{ kind: "session", id: 1 }], active: null };
    renderAt("/projects/1");
    expect(await screen.findByRole("tab", { name: "Overview" })).toBeTruthy();
    await act(async () => {
      pushProject({ seq: 0, type: "replayed" });
      pushProject(exported);
      await new Promise((r) => setTimeout(r, 10));
    });

    await waitFor(() => expect(puts.at(-1)).toEqual({
      open: [{ kind: "session", id: 1 }, { kind: "artifact", id: 7 }],
      active: null,
    }));
    expect(screen.getByRole("tab", { name: "Overview" }).getAttribute("aria-selected")).toBe("true");
  });

  it("is left to the chat's own stream once that stream is live", async () => {
    renderAt("/projects/1");
    expect(await screen.findByText("What are we working on?")).toBeTruthy(); // chat 1 is showing
    await act(async () => {
      pushChat({ seq: 0, type: "replayed" }); // the chat's stream is past its replay: live
      pushProject({ seq: 0, type: "replayed" });
      pushProject(exported);
      await new Promise((r) => setTimeout(r, 10));
    });

    expect(puts).toEqual([]); // nothing opened twice; the chat's stream handles its own tabs
  });

  it("is added when the chat is the active tab but its stream isn't live yet", async () => {
    renderAt("/projects/1");
    expect(await screen.findByText("What are we working on?")).toBeTruthy(); // showing, not live
    await act(async () => {
      pushProject({ seq: 0, type: "replayed" });
      pushProject(exported);
      await new Promise((r) => setTimeout(r, 10));
    });

    // Its own stream would replay this event as already seen, and never open it.
    await waitFor(() => expect(puts.at(-1)).toEqual({
      open: [{ kind: "session", id: 1 }, { kind: "artifact", id: 7 }],
      active: { kind: "session", id: 1 },
    }));
  });
});

describe("After the server restarts", () => {
  afterEach(() => vi.useRealTimers());

  it("the chat's stream reconnects, and acts on events numbered from 1 again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderAt("/projects/1");
    expect(await screen.findByText("What are we working on?")).toBeTruthy(); // chat 1 is showing
    await act(async () => {
      pushChat({ seq: 0, type: "replayed" });
      pushChat({ seq: 7, type: "turn", status: "done" }); // the old server had counted to 7
      endChat(); // …and stops
      await vi.advanceTimersByTimeAsync(1000); // the page waits, then opens the stream again
    });
    await act(async () => {
      pushChat({ seq: 0, type: "replayed" });
      pushChat({
        seq: 1, type: "tool_result", call_id: "c1", text: "Opened", error: false, refs: [],
        action: { type: "open_tab", tab: { kind: "artifact", id: 7 }, focus: false },
      });
      await vi.advanceTimersByTimeAsync(10);
    });

    await waitFor(() => expect(puts.at(-1)).toEqual({
      open: [{ kind: "session", id: 1 }, { kind: "artifact", id: 7 }],
      active: { kind: "session", id: 1 },
    }));
    const opened = vi.mocked(fetch).mock.calls.filter(([url]) => url === "/api/projects/1/sessions/1/events");
    expect(opened).toHaveLength(2);
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
