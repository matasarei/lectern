import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Chat, ChatMessage, Tab } from "../api/types";
import { ChatTab, shortArgs, writtenScript } from "./chat";

const message = (id: number, over: Partial<ChatMessage>): ChatMessage => ({
  id, role: "user", content: "", tool_calls: null, tool_call_id: null, tool_name: null,
  attachments: [], created_at: "", ...over,
});

let chat: Chat;
let posts: { url: string; body: unknown }[] = [];
let push: (event: object) => void = () => {};

// The runtime the page reads for "what it's waiting for"; its own event stream is empty.
let runtimeState: Record<string, unknown> = {};

beforeEach(() => {
  runtimeState = { residents: [], activity: null, hold: null };
  posts = [];
  chat = {
    id: 1, kind: "chat", title: "Payment terms", state: "idle", pending: [], pinned: [], followups: [],
    summary: { upto: 0, version: 0, messages: 0 },
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
      if (url === "/api/runtime/events") return new Response(new ReadableStream({ start: (c) => c.close() }));
      if (url === "/api/runtime") return new Response(JSON.stringify(runtimeState));
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
      if (url === "/api/projects/1/items") {
        return new Response(JSON.stringify({
          sessions: [],
          artifacts: [
            { id: 11, name: "scripts/penalty.py" },
            { id: 21, name: "lease-1998.docx" },
            { id: 22, name: "notes.md" },
          ],
        }));
      }
      if (url === "/api/projects/1/artifacts/11/content") {
        return new Response("print('penalty', 62.5)\n");
      }
      if (init?.method === "DELETE") {
        posts.push({ url, body: "DELETE" });
        return new Response(JSON.stringify({ id: "p1", text: "x" }));
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

  it("says what the reply waits for: the engine downloading, then the model loading", async () => {
    runtimeState = {
      residents: [], hold: null,
      activity: { kind: "engine", variant: "linux-cuda12", done: 120 * 10 ** 6, total: 420 * 10 ** 6 },
    };
    renderChat();
    await screen.findByText("Yes: lease.md.");
    await events({ type: "turn", status: "running", message_id: 5, text: "hi", attachments: [] });

    expect((await screen.findByRole("status")).textContent).toBe(
      "Downloading the llama.cpp engine · 120 MB of 420 MB",
    );
  });

  it("says when the reply waits for an OCR page to finish", async () => {
    runtimeState = {
      activity: null, hold: null,
      residents: [{ role: "ocr", model_id: "paddleocr-vl-1.6", leases: 1, bytes: 1, ctx: 4096,
                    kv_type: "f16", on_gpu: true, status: "loaded", pid: 1 }],
    };
    renderChat();
    await screen.findByText("Yes: lease.md.");
    await events({ type: "turn", status: "running", message_id: 5, text: "hi", attachments: [] });

    expect((await screen.findByRole("status")).textContent).toBe(
      "Waiting for the OCR job to finish its page…",
    );
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

describe("Tools at work", () => {
  it("shows a download bar, then a build line, under the tool that's working", async () => {
    chat.messages.push(
      message(5, { content: "What's the penalty?" }),
      message(6, {
        role: "assistant",
        tool_calls: [{ id: "w1", type: "function", function: { name: "code_write", arguments: '{"name":"penalty"}' } }],
      }),
    );
    renderChat();
    await screen.findByText("Yes: lease.md.");
    await events(
      { type: "turn", status: "running", message_id: 5, text: "x", attachments: [] },
      { type: "progress", call_id: "w1", label: "Downloading Qwen2.5-Coder 1.5B", done: 550, total: 1100 },
    );
    expect(screen.getByTestId("progress").textContent).toBe("Downloading Qwen2.5-Coder 1.5B: 50%");

    await act(async () => {
      push({ seq: 3, type: "progress", call_id: "w1", label: "Building the script sandbox", line: "Step 2/3 : RUN pip install" });
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(screen.getByTestId("progress").textContent).toBe("Building the script sandboxStep 2/3 : RUN pip install");
  });

  it("shows the script code.write saved", async () => {
    chat.messages.push(
      message(5, {
        role: "assistant",
        tool_calls: [{ id: "w1", type: "function", function: { name: "code_write", arguments: '{"name":"penalty"}' } }],
      }),
      message(6, {
        role: "tool", tool_call_id: "w1", tool_name: "code.write",
        content: "Wrote scripts/penalty.py (1 lines) with coder; its test run passed in 0.3 s.",
      }),
    );
    renderChat();
    const block = await screen.findByTestId("script");
    expect(block.textContent).toContain("scripts/penalty.py");
    await waitFor(() => expect(block.textContent).toContain("print('penalty', 62.5)"));
  });
});

describe("writtenScript", () => {
  it("finds the script's path in code.write's result", () => {
    expect(writtenScript("Wrote scripts/penalty (2).py (4 lines) with x")).toBe("scripts/penalty (2).py");
    expect(writtenScript("Error: couldn't write a working script")).toBeNull();
  });
});

describe("Pinned items, the summary and the context", () => {
  it("shows what's pinned and unpins on ×", async () => {
    chat.pinned = [{ id: "p1", kind: "instruction", text: "Answer in Ukrainian" }];
    renderChat();
    const strip = await screen.findByTestId("pinned");
    expect(strip.textContent).toContain("instruction Answer in Ukrainian");
    fireEvent.click(screen.getByLabelText("Unpin: Answer in Ukrainian"));
    await waitFor(() => expect(posts).toEqual([{ url: "/api/projects/1/sessions/1/pins/p1", body: "DELETE" }]));
  });

  it("shows the follow-ups waiting for an OCR session, and says when one runs", async () => {
    chat.followups = [{ id: "f1", label: "after OCR · lease-scan.pdf: export to ODT" }];
    renderChat();
    const strip = await screen.findByTestId("followups");
    expect(strip.textContent).toBe("Waitingafter OCR · lease-scan.pdf: export to ODT");
    await events({ type: "turn", status: "running", followup: "after OCR · lease-scan.pdf: export to ODT" });
    expect(screen.getByTestId("followup-turn").textContent).toBe(
      "Follow-up: after OCR · lease-scan.pdf: export to ODT",
    );
  });

  it("marks where the summary ends", async () => {
    chat.summary = { upto: 2, version: 1, messages: 2 };
    renderChat();
    await screen.findByText("Yes: lease.md.");
    const notes = screen.getAllByRole("note");
    expect(notes[0].textContent).toBe("2 earlier messages are summarised for the model. They're all still here.");
  });

  it("shows how the context is used, and when it was compacted", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");
    await events(
      { type: "turn", status: "running", message_id: 5, text: "x", attachments: [] },
      { type: "usage", budget: 6656, parts: { system: 400, tools: 1300, pinned: 20, files: 0, summary: 600, turns: 2780 } },
      { type: "compacting" },
    );
    expect(screen.getByTestId("usage").textContent).toBe("Context 5.1k of 6.7k · pinned 20 · summary 600 · recent 2.8k");
    expect(screen.getByTestId("compaction").textContent).toBe("Tidying up the context…");
    await act(async () => {
      push({ seq: 4, type: "compacted", summarised: 12, before: 5100, after: 3000, budget: 6656 });
      await new Promise((r) => setTimeout(r, 10));
    });
    expect(screen.getByTestId("compaction").textContent).toBe("Folded 12 older messages into the summary.");
  });
});


describe("Adding files already in the project", () => {
  const box = () => screen.getByRole("textbox", { name: "Message" });

  it("@ opens the project's files, filters as you type, and Enter attaches one", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");

    fireEvent.change(box(), { target: { value: "read @lea", selectionStart: 9 } });
    const list = await screen.findByRole("listbox", { name: "Project files" });
    const options = await within(list).findAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["lease-1998.docx"]);

    fireEvent.keyDown(box(), { key: "Enter" });

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByRole("button", { name: "Remove lease-1998.docx" })).toBeTruthy();
    expect((box() as HTMLTextAreaElement).value).toBe("read "); // the @lea is gone
    expect(posts).toEqual([]); // Enter picked; it didn't send
  });

  it("with no matching file, Enter sends the message (\"@\" is just text then)", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");

    fireEvent.change(box(), { target: { value: "ping @team", selectionStart: 10 } });
    await screen.findByText("No file matches");
    fireEvent.keyDown(box(), { key: "Enter" });

    await waitFor(() =>
      expect(posts).toContainEqual({
        url: "/api/projects/1/sessions/1/messages",
        body: { text: "ping @team", attachments: [] },
      }),
    );
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("sending with the button closes a list left open", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");

    fireEvent.change(box(), { target: { value: "see @lea", selectionStart: 8 } });
    await screen.findAllByRole("option");
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(posts.map((p) => p.url)).toContain("/api/projects/1/sessions/1/messages"));
    await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
  });

  it("arrows move through the list and Escape closes it", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");

    fireEvent.change(box(), { target: { value: "@", selectionStart: 1 } });
    await screen.findAllByRole("option"); // the project's three files
    fireEvent.keyDown(box(), { key: "ArrowDown" });
    expect(screen.getByRole("option", { selected: true }).textContent).toBe("lease-1998.docx");
    // A screen reader follows the active option: the box names the list and the option.
    const listId = screen.getByRole("listbox").id;
    expect(box().getAttribute("aria-controls")).toBe(listId);
    expect(box().getAttribute("aria-activedescendant")).toBe(
      screen.getByRole("option", { selected: true }).id,
    );
    fireEvent.keyDown(box(), { key: "Escape" });

    expect(screen.queryByRole("listbox")).toBeNull();
    expect(box().getAttribute("aria-activedescendant")).toBeNull();
    expect((box() as HTMLTextAreaElement).value).toBe("@");
  });

  it("From project lists the files to pick from, and the message is sent with it", async () => {
    renderChat();
    await screen.findByText("Yes: lease.md.");

    fireEvent.click(screen.getByRole("button", { name: "From project" }));
    fireEvent.change(await screen.findByRole("searchbox", { name: "Find a file" }), {
      target: { value: "notes" },
    });
    fireEvent.mouseDown(await screen.findByRole("option", { name: "notes.md" }));
    fireEvent.change(box(), { target: { value: "what's next?", selectionStart: 12 } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(posts).toContainEqual({
        url: "/api/projects/1/sessions/1/messages",
        body: { text: "what's next?", attachments: [22] },
      }),
    );
  });
});
