import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OcrDocument, OcrSession, Tab } from "../api/types";
import { installFakeSockets } from "../test/fake-socket";
import { OcrTab } from "./ocr";
import { block, doubt, documentOf, page, session } from "./ocr-fixtures";

let current: OcrSession;
const firstDoc = (): OcrDocument =>
  documentOf([
    block("b1.1", "Рахунок № {d1.1} від 1998 р.", { doubts: [doubt("d1.1", "1?", { candidates: ["1", "7"] })] }),
  ]);
let currentDoc: OcrDocument;
let posts: { url: string; body: unknown }[] = [];
let push: (event: object) => void = () => {};

// The runtime the page reads for "what it's waiting for"; its own event stream is empty.
let runtimeState: Record<string, unknown> = {};

beforeEach(() => {
  currentDoc = firstDoc();
  runtimeState = { residents: [], activity: null, hold: null };
  posts = [];
  current = session({
    pages: [
      page(1, { status: "done", model_id: "paddleocr-vl-1.6", seconds: 1.4 }),
      page(2, { status: "recognising" }),
    ],
  });
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal("URL", { ...URL, createObjectURL: () => "blob:page", revokeObjectURL: () => {} });
  // The event streams: the runtime's stays silent, the OCR session's sends what a test pushes.
  installFakeSockets((socket) => {
    socket.open();
    if (socket.path !== "/api/runtime/events") push = (event) => socket.push(event);
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/runtime") return new Response(JSON.stringify(runtimeState));
      if (url === "/api/exporters") {
        return new Response(JSON.stringify([{ format: "odt", extension: ".odt", capabilities: [] }]));
      }
      if (url === "/api/projects/1/export") {
        posts.push({ url, body: JSON.parse(String(init?.body)) });
        return new Response(JSON.stringify({ id: 50, name: "Lease.odt" }), { status: 201 });
      }
      if (init?.method === "POST" || init?.method === "PATCH") {
        posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null });
        return new Response(JSON.stringify({}));
      }
      if (url === "/api/projects/1/ocr/models") {
        return new Response(JSON.stringify([
          { id: "paddleocr-vl-1.6", title: "PaddleOCR-VL 1.6", role: "ocr", downloaded: true },
          { id: "qwen3-vl-8b", title: "Qwen3-VL 8B Instruct", role: "ocr.hand", downloaded: true },
          { id: "chandra-ocr-2", title: "Chandra OCR 2", role: "ocr.hand", downloaded: false },
        ]));
      }
      if (url.endsWith("/image")) return new Response(new Blob(["png"], { type: "image/png" }));
      if (url.endsWith("/document")) return new Response(JSON.stringify(currentDoc));
      if (url.endsWith("/pages/1/text")) {
        return new Response(JSON.stringify({
          number: 1, model_id: "paddleocr-vl-1.6", text: "Рахунок № 1",
          cleaned: "{#b1.1 p=1} Рахунок № ⟦1? | open | 1 · 7⟧ від 1998 р.",
        }));
      }
      if (url.endsWith("/text")) return new Response(JSON.stringify({ number: 2, model_id: null, text: null }));
      return new Response(JSON.stringify(current));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderTab(onOpen: (tab: Tab) => void = () => {}, initialPage?: number, toQuestions = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <OcrTab projectId={1} sessionId={2} onOpen={onOpen} initialPage={initialPage} toQuestions={toQuestions} />
    </QueryClientProvider>,
  );
}

describe("OCR tab", () => {
  it("lists the pages and shows the first beside its text", async () => {
    renderTab();
    const list = await screen.findByRole("list", { name: "Pages" });
    expect(within(list).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Page 1read",
      "Page 2reading…",
    ]);
    expect(await screen.findByText("Рахунок № 1")).toBeTruthy();
    expect(await screen.findByAltText("Preview of Page 1")).toBeTruthy();
    expect(screen.getByText("Read 1/2")).toBeTruthy();
    expect(screen.getByText("read by paddleocr-vl-1.6 in 1.4 s")).toBeTruthy();
  });

  it("updates a page from the job's events, with its flags", async () => {
    renderTab();
    await screen.findByRole("list", { name: "Pages" });
    await act(async () => {
      push({ seq: 1, type: "phase", phase: "recognise" });
      push({ seq: 2, type: "waiting", reason: "chat" });
    });
    // No countdown in the event: it waits for the chat's reply (the wording changed with it).
    expect(await screen.findByText("Starts when the chat's reply is finished")).toBeTruthy();
    await act(async () => {
      push({ seq: 3, type: "page", ...page(2, { status: "done", model_id: "paddleocr-vl-1.6", looped: true }) });
    });
    await waitFor(() => expect(screen.getByText("read · repeats itself")).toBeTruthy());
    expect(screen.queryByText("Starts when the chat's reply is finished")).toBeNull();
    expect(screen.getByText("Read 2/2")).toBeTruthy();
  });

  it("spins while it reads, not while it waits for the chat's reply", async () => {
    renderTab();
    await screen.findByRole("list", { name: "Pages" });
    const spinner = () => screen.queryByTestId("ocr-working");
    await act(async () => {
      push({ seq: 1, type: "phase", phase: "recognise" });
    });
    await waitFor(() => expect(spinner()).not.toBeNull());
    await act(async () => {
      push({ seq: 2, type: "waiting", reason: "chat", resume_in: 100 });
    });
    await screen.findByRole("status"); // the countdown says it waits…
    expect(spinner()).toBeNull(); // …and nothing spins beside the step
    await act(async () => {
      push({ seq: 3, type: "page", ...page(2, { status: "recognising" }) });
    });
    await waitFor(() => expect(spinner()).not.toBeNull()); // the wait is over: at work again
  });

  it("counts down the wait for the chat, and Start now asks for it", async () => {
    renderTab();
    await screen.findByRole("list", { name: "Pages" });
    await act(async () => {
      push({ seq: 1, type: "phase", phase: "recognise" });
      push({ seq: 2, type: "waiting", reason: "chat", resume_in: 100 });
    });

    expect((await screen.findByRole("status")).textContent).toMatch(
      /Starts in 1:(40|39), when the chat has been quiet for 2 minutes/,
    );
    fireEvent.click(screen.getByRole("button", { name: "Start now" }));
    await waitFor(() => expect(posts.map((p) => p.url)).toEqual(["/api/projects/1/ocr/2/start-now"]));
  });

  it("says when a model is loading between the phases, and Start now stands out", async () => {
    runtimeState = { ...runtimeState, activity: { kind: "loading", role: "main", model_id: "qwen3.5-4b" } };
    renderTab();
    await screen.findByRole("list", { name: "Pages" });
    await act(async () => {
      push({ seq: 1, type: "phase", phase: "clean" });
    });
    await waitFor(() =>
      expect(screen.getByTestId("ocr-steps").textContent).toContain(
        "Cleaning· loading the chat model to clean the text…",
      ),
    );

    await act(async () => {
      push({ seq: 2, type: "phase", phase: "recognise" });
      push({ seq: 3, type: "waiting", reason: "chat", resume_in: 100 });
    });
    expect(screen.getByTestId("ocr-steps").textContent).not.toContain("loading the chat model"); // not while reading
    expect(screen.getByRole("button", { name: "Start now" }).className).toContain("btn-primary");
  });

  it("pauses a running job and resumes a paused one", async () => {
    renderTab();
    fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
    await waitFor(() => expect(posts.map((p) => p.url)).toEqual(["/api/projects/1/ocr/2/pause"]));
    current = { ...current, status: "paused", paused_reason: "Lectern restarted" }; // as stored
    await act(async () => {
      push({ seq: 4, type: "session", status: "paused", reason: "Lectern restarted", error: null });
    });
    expect(await screen.findByText("(Lectern restarted)")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(posts.at(-1)?.url).toBe("/api/projects/1/ocr/2/resume"));
  });

  it("switches between the scan, the text and both", async () => {
    renderTab();
    await screen.findByText("Рахунок № 1");
    fireEvent.click(screen.getByRole("button", { name: "Text" }));
    expect(screen.queryByAltText("Preview of Page 1")).toBeNull();
    expect(screen.getByText("Рахунок № 1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Scan" }));
    expect(await screen.findByAltText("Preview of Page 1")).toBeTruthy();
    expect(screen.queryByText("Рахунок № 1")).toBeNull();
  });

  it("says a page hasn't been read yet", async () => {
    renderTab();
    fireEvent.click(within(await screen.findByRole("list", { name: "Pages" })).getByText("Page 2"));
    expect(await screen.findByText("Not read yet.")).toBeTruthy();
  });

  it("opens at the page it's given (from a question in the Document tab)", async () => {
    renderTab(() => {}, 2);
    const list = await screen.findByRole("list", { name: "Pages" });
    expect(within(list).getByText("Page 2").closest("button")?.getAttribute("aria-current")).toBe("true");
  });

  it("opens at the first open question when asked to (the chat's \"N open questions\")", async () => {
    current = session({
      status: "done",
      document: { version: 2, artifact_id: 40, open_doubts: 1 },
      pages: [page(1, { status: "done", clean_status: "done" }), page(2, { status: "done", clean_status: "done" })],
    });
    currentDoc = documentOf([
      block("b1.1", "Рахунок № {d1.1}", { doubts: [doubt("d1.1", "1?", { status: "resolved", value: "7" })] }),
      block("b2.1", "Сума {d2.1} грн", { doubts: [doubt("d2.1", "2?0", { candidates: ["250"] })] }),
    ]);
    renderTab(() => {}, undefined, true);

    const card = await screen.findByTestId("question-card"); // page 2's, selected
    expect(within(card).getByText("2?0 → 250")).toBeTruthy();
    const list = screen.getByRole("list", { name: "Pages" });
    const shown = (n: number) => within(list).getByText(`Page ${n}`).closest("button")?.getAttribute("aria-current");
    expect(shown(2)).toBe("true");

    // The last answer: the Document comes back with nothing open, and the page stays put.
    currentDoc = documentOf([
      block("b1.1", "Рахунок № {d1.1}", { doubts: [doubt("d1.1", "1?", { status: "resolved", value: "7" })] }),
      block("b2.1", "Сума {d2.1} грн", { doubts: [doubt("d2.1", "2?0", { status: "resolved", value: "250" })] }),
    ]);
    fireEvent.click(within(card).getByRole("button", { name: "Accept 250" }));
    await waitFor(() => expect(screen.queryByTestId("question-card")).toBeNull());
    expect(shown(2)).toBe("true");
  });

  it("goes to the first open question from its own steps bar", async () => {
    current = session({
      status: "done",
      document: { version: 2, artifact_id: 40, open_doubts: 1 },
      pages: [page(1, { status: "done", clean_status: "done" }), page(2, { status: "done", clean_status: "done" })],
    });
    currentDoc = documentOf([
      block("b1.1", "Рахунок № 7", {}),
      block("b2.1", "Сума {d2.1} грн", { doubts: [doubt("d2.1", "2?0", { candidates: ["250"] })] }),
    ]);
    renderTab();
    fireEvent.click(await screen.findByRole("button", { name: "1 open question" }));

    expect(await screen.findByTestId("question-card")).toBeTruthy();
    const list = screen.getByRole("list", { name: "Pages" });
    expect(within(list).getByText("Page 2").closest("button")?.getAttribute("aria-current")).toBe("true");
  });

  it("saves a page's note and marks it handwritten", async () => {
    renderTab();
    const note = await screen.findByRole("textbox", { name: "Note for this page" });
    fireEvent.change(note, { target: { value: "Печатка орендодавця." } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Handwritten/ }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts).toEqual([
      { url: "/api/projects/1/ocr/2/pages/1", body: { note: "Печатка орендодавця." } },
      { url: "/api/projects/1/ocr/2/pages/1", body: { handwritten: true } },
    ]);
  });

  it("offers to clean a cleaned page again once its note is saved, and only then", async () => {
    current = session({ pages: [page(1, { status: "done", clean_status: "done" }), page(2)] });
    renderTab();
    const note = await screen.findByRole("textbox", { name: "Note for this page" });
    expect(screen.queryByRole("button", { name: "Clean this page again with this note" })).toBeNull();
    fireEvent.change(note, { target: { value: "Не об'єднуй таблицю." } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));
    fireEvent.click(await screen.findByRole("button", { name: "Clean this page again with this note" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toEqual({ url: "/api/projects/1/ocr/2/pages/1/clean", body: null });
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Clean this page again with this note" })).toBeNull(),
    );
  });

  it("doesn't offer to clean a page that isn't cleaned yet", async () => {
    renderTab(); // page 1 is read, not cleaned
    const note = await screen.findByRole("textbox", { name: "Note for this page" });
    fireEvent.change(note, { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(screen.queryByRole("button", { name: "Clean this page again with this note" })).toBeNull();
  });

  it("reads a page again with a downloaded model, and offers no model that isn't", async () => {
    renderTab();
    const choose = await screen.findByRole("combobox", { name: "Model to read the page again with" });
    await within(choose).findByText("Qwen3-VL 8B Instruct");
    const missing = within(choose).getByText("Chandra OCR 2 (not downloaded)") as HTMLOptionElement;
    expect(missing.disabled).toBe(true);
    fireEvent.change(choose, { target: { value: "qwen3-vl-8b" } });
    fireEvent.click(screen.getByRole("button", { name: "Re-run page" }));
    await waitFor(() =>
      expect(posts).toEqual([{ url: "/api/projects/1/ocr/2/pages/1/rerun", body: { model_id: "qwen3-vl-8b" } }]),
    );
  });

  it("doesn't offer a re-run while the page is being read", async () => {
    renderTab();
    fireEvent.click(within(await screen.findByRole("list", { name: "Pages" })).getByText("Page 2"));
    expect((await screen.findByRole("button", { name: "Re-run page" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("lists every page once they're made, not only the ones read so far", async () => {
    current = session({ status: "running", pages: [], pages_total: 0 });
    renderTab();
    expect(await screen.findByText("The pages are being made from the file…")).toBeTruthy();
    current = session({ status: "running", pages: [page(1), page(2), page(3)], pages_total: 3 });
    await act(async () => {
      push({ seq: 1, type: "phase", phase: "recognise" });
      push({ seq: 2, type: "page", ...page(1, { status: "recognising" }) });
    });
    const list = await screen.findByRole("list", { name: "Pages" });
    await waitFor(() => expect(within(list).getAllByRole("button")).toHaveLength(3));
    expect(screen.getByText("Read 0/3")).toBeTruthy();
  });

  it("reads a page not read yet on its own when the session is paused", async () => {
    current = session({ status: "paused", paused_reason: "Paused", pages: [page(1, { status: "done" }), page(2)] });
    renderTab();
    fireEvent.click(within(await screen.findByRole("list", { name: "Pages" })).getByText("Page 2"));
    const rerun = (await screen.findByRole("button", { name: "Re-run page" })) as HTMLButtonElement;
    expect(rerun.disabled).toBe(false);
    fireEvent.click(rerun);
    await waitFor(() => expect(posts.at(-1)?.url).toBe("/api/projects/1/ocr/2/pages/2/rerun"));
  });

  it("shows the cleaned page with its open questions marked, and the raw text beside", async () => {
    current = session({
      status: "done",
      document: { version: 2, artifact_id: 40, open_doubts: 3 },
      pages: [page(1, { status: "done", clean_status: "done" }), page(2, { status: "done" })],
    });
    const opened: Tab[] = [];
    renderTab((tab) => opened.push(tab));

    const mark = await screen.findByText("1?", { selector: "mark" });
    expect(mark.dataset.doubt).toBe("d1.1");
    expect(screen.getByText("Clean 1/2")).toBeTruthy();
    // Done reading and cleaning, but not final: said plainly, and the steps stop at the questions
    expect(screen.getByText(/^Not finished: 3 open questions\./)).toBeTruthy();
    expect(screen.getByTestId("ocr-steps").textContent).toContain("Your questions· 3 open questions");
    expect(screen.getByTestId("ocr-steps").textContent).not.toContain("✓Done");
    fireEvent.click(screen.getByRole("button", { name: "Raw OCR" }));
    expect(await screen.findByText("Рахунок № 1")).toBeTruthy();
    expect(screen.queryByText("1?", { selector: "mark" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open the document (version 2)" }));
    expect(opened).toEqual([{ kind: "document", id: 2 }]); // the Document tab, not the bare file
  });

  it("exports a finished session's document from its header, and opens the file", async () => {
    current = session({
      status: "done",
      document: { version: 2, artifact_id: 40, open_doubts: 0 },
      pages: [page(1, { status: "done", clean_status: "done" })],
    });
    const opened: Tab[] = [];
    renderTab((tab) => opened.push(tab));
    fireEvent.click(await screen.findByRole("button", { name: "ODT" }));
    await waitFor(() => expect(opened).toEqual([{ kind: "artifact", id: 50 }]));
    expect(posts.at(-1)).toEqual({
      url: "/api/projects/1/export", body: { artifact_id: 40, format: "odt", accept_loss: false },
    });
    expect(screen.queryByText(/^as it is/)).toBeNull(); // nothing open: nothing to say
  });

  it("says an export before the questions are answered is as it is, with them marked", async () => {
    current = session({
      status: "done",
      document: { version: 2, artifact_id: 40, open_doubts: 3 },
      pages: [page(1, { status: "done", clean_status: "done" })],
    });
    renderTab();
    const odt = await screen.findByRole("button", { name: "ODT" });
    expect((odt as HTMLButtonElement).disabled).toBe(false); // still there to use
    expect(within(screen.getByTestId("export")).getByText("as it is: its 3 open questions are marked")).toBeTruthy();
  });

  it("offers no export while the session is still at work", async () => {
    renderTab();
    await screen.findByRole("list", { name: "Pages" });
    expect(screen.queryByTestId("export")).toBeNull();
  });

  it("says when a page isn't cleaned yet, or why its cleanup failed", async () => {
    current = session({
      pages: [page(1, { status: "done", clean_status: "failed", clean_error: "the reply wasn't valid" })],
    });
    renderTab();
    expect(await screen.findByText("This page wasn't cleaned: the reply wasn't valid")).toBeTruthy();
    expect(screen.getByText("read · not cleaned")).toBeTruthy();
    cleanup();

    current = session({ pages: [page(1, { status: "done", clean_status: "pending" })] });
    renderTab();
    expect(
      await screen.findByText("Not cleaned yet: this is the raw OCR text. The cleaned text and its questions come next."),
    ).toBeTruthy();
    expect(screen.getByText("Рахунок № 1")).toBeTruthy();
  });
});
