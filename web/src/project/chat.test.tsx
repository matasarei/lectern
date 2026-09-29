import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Chat, ChatMessage, Tab } from "../api/types";
import { ChatTab, shortArgs } from "./chat";

const message = (id: number, over: Partial<ChatMessage>): ChatMessage => ({
  id, role: "user", content: "", tool_calls: null, tool_call_id: null, tool_name: null,
  attachments: [], created_at: "", ...over,
});

let chat: Chat;
let posts: { url: string; body: unknown }[] = [];
let push: (event: object) => void = () => {};

beforeEach(() => {
  posts = [];
  chat = {
    id: 1, kind: "chat", title: "Payment terms", state: "idle", pending: [],
    messages: [
      message(1, { content: "Is there a lease file?", attachments: [{ id: 3, name: "notes.md" }] }),
      message(2, {
        role: "assistant",
        tool_calls: [{ id: "c1", type: "function", function: { name: "project_find", arguments: '{"query":"lease"}' } }],
      }),
      message(3, { role: "tool", tool_call_id: "c1", tool_name: "project.find", content: "lease.md · file · 24 B\nmore" }),
      message(4, { role: "assistant", content: "Yes: lease.md." }),
    ],
  };
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/events")) {
        const encoder = new TextEncoder();
        return new Response(
          new ReadableStream({
            start(controller) {
              push = (event) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
            },
          }),
        );
      }
      if (init?.method === "POST" || init?.method === "PUT") {
        posts.push({ url, body: init.body instanceof File ? init.body : JSON.parse(String(init.body ?? "null")) });
        if (url.includes("/files?")) return new Response(JSON.stringify({ id: 9, name: "terms.md" }));
        return new Response(JSON.stringify({ message_id: 5 }));
      }
      return new Response(JSON.stringify(chat));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderChat(onOpen: (tab: Tab) => void = () => {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ChatTab projectId={1} sessionId={1} onOpen={onOpen} />
    </QueryClientProvider>,
  );
}

async function events(...list: object[]) {
  await act(async () => {
    list.forEach((event, i) => push({ seq: i + 1, ...event }));
    await new Promise((r) => setTimeout(r, 10));
  });
}

describe("Chat", () => {
  it("shows the transcript with tool calls as collapsible lines", async () => {
    renderChat();
    expect(await screen.findByText("Yes: lease.md.")).toBeTruthy();
    expect(screen.getByText("Is there a lease file?")).toBeTruthy();
    expect(screen.getByText("Attached: notes.md")).toBeTruthy();
    const line = screen.getByTestId("tool-line");
    expect(within(line).getByText('project.find("lease") → lease.md · file · 24 B')).toBeTruthy();
    expect(line.hasAttribute("open")).toBe(false);
    expect(line.textContent).toContain("more"); // the full result, inside the fold
  });

  it("streams a reply and offers Stop while it runs", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");
    await events(
      { type: "turn", status: "running", message_id: 5, text: "hi", attachments: [] },
      { type: "model", model_id: "qwen3.5-4b", ctx: 8192 },
      { type: "delta", text: "Hel" },
      { type: "delta", text: "lo" },
    );

    expect(screen.getByTestId("streaming").textContent).toBe("Hello");
    expect(screen.getByTestId("model-line").textContent).toBe("main · qwen3.5-4b · 8,192 tokens of context");
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(posts.map((p) => p.url)).toEqual(["/api/projects/1/sessions/1/stop"]));
  });

  it("asks before deleting, and sends the answer", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");
    await events(
      { type: "turn", status: "running", message_id: 5, text: "delete", attachments: [] },
      { type: "confirm", confirmation_id: 7, call_id: "c9", name: "artifact.trash", text: "Move old.md to the trash?" },
    );

    const card = screen.getByRole("alertdialog", { name: "Allow this?" });
    expect(within(card).getByText("Move old.md to the trash?")).toBeTruthy();
    fireEvent.click(within(card).getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ url: "/api/projects/1/sessions/1/confirmations/7", body: { approve: true } });

    await act(async () => {
      push({ seq: 3, type: "confirmed", confirmation_id: 7, approved: true });
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(within(card).getByText("Allowed")).toBeTruthy();
  });

  it("marks where the model's view starts when older messages were left out", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");
    await events(
      { type: "turn", status: "running", message_id: 5, text: "x", attachments: [] },
      { type: "context", left_out: 2, released: 0, stubbed_files: [] },
    );
    const note = screen.getByRole("note");
    expect(note.textContent).toBe("2 older messages aren't in what the model sees now. They're still here.");
  });

  it("ignores events it has already seen when a stream replays them", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");
    await events(
      { type: "turn", status: "running", message_id: 5, text: "x", attachments: [] },
      { type: "delta", text: "Dear " },
    );
    await act(async () => {
      push({ seq: 2, type: "delta", text: "Dear " }); // replayed
      push({ seq: 3, type: "delta", text: "tenant" });
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(screen.getByTestId("streaming").textContent).toBe("Dear tenant");
  });

  it("sends a message with attached files", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");
    const file = new File(["# Terms"], "terms.md");
    fireEvent.change(screen.getByTestId("attach-picker"), { target: { files: [file] } });
    expect(await screen.findByLabelText("Remove terms.md")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Summarise it" } });
    fireEvent.keyDown(screen.getByLabelText("Message"), { key: "Enter" });

    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[0].url).toBe("/api/projects/1/files?name=terms.md");
    expect(posts[1]).toEqual({
      url: "/api/projects/1/sessions/1/messages",
      body: { text: "Summarise it", attachments: [9] },
    });
  });

  it("opens a tab when a tool asks for it, but not again when the turn is replayed", async () => {
    const openTab = {
      type: "tool_result", call_id: "c2", text: "Opened", error: false, refs: [],
      action: { type: "open_tab", tab: { kind: "artifact", id: 3 } },
    };
    const opened: Tab[] = [];
    renderChat((tab) => opened.push(tab));
    await screen.findByText("Yes: lease.md.");
    await act(async () => {
      push({ seq: 1, ...openTab }); // replayed: it already happened
      push({ seq: 0, type: "replayed" });
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(opened).toEqual([]);

    await act(async () => {
      push({ seq: 2, ...openTab, call_id: "c3" }); // live
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(opened).toEqual([{ kind: "artifact", id: 3 }]);
  });
});

describe("shortArgs", () => {
  it("shows one argument as its value and several as name=value", () => {
    expect(shortArgs('{"query":"lease"}')).toBe('"lease"');
    expect(shortArgs('{"name":"a.md","refs":["#c1"]}')).toBe('name="a.md", refs=["#c1"]');
    expect(shortArgs("{not json")).toBe("{not json");
    expect(shortArgs("")).toBe("");
  });
});
