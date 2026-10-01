import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OcrSession, Tab } from "../api/types";
import { OcrTab } from "./ocr";
import { page, session } from "./ocr-fixtures";

let current: OcrSession;
let posts: { url: string; body: unknown }[] = [];
let push: (event: object) => void = () => {};

beforeEach(() => {
  posts = [];
  current = session({
    pages: [
      page(1, { status: "done", model_id: "paddleocr-vl-1.6", seconds: 1.4 }),
      page(2, { status: "recognising" }),
    ],
  });
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal("URL", { ...URL, createObjectURL: () => "blob:page", revokeObjectURL: () => {} });
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

function renderTab(onOpen: (tab: Tab) => void = () => {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <OcrTab projectId={1} sessionId={2} onOpen={onOpen} />
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
    expect(await screen.findByText("Waiting for the chat to be idle")).toBeTruthy();
    await act(async () => {
      push({ seq: 3, type: "page", ...page(2, { status: "done", model_id: "paddleocr-vl-1.6", looped: true }) });
    });
    await waitFor(() => expect(screen.getByText("read · repeats itself")).toBeTruthy());
    expect(screen.queryByText("Waiting for the chat to be idle")).toBeNull();
    expect(screen.getByText("Read 2/2")).toBeTruthy();
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

    const mark = await screen.findByText("⟦1? | open | 1 · 7⟧");
    expect(mark.tagName).toBe("MARK");
    expect(screen.getByText("Clean 1/2")).toBeTruthy();
    expect(screen.getByText("3 open questions")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Raw OCR" }));
    expect(await screen.findByText("Рахунок № 1")).toBeTruthy();
    expect(screen.queryByText("⟦1? | open | 1 · 7⟧")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open the document (version 2)" }));
    expect(opened).toEqual([{ kind: "artifact", id: 40 }]);
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
    expect(await screen.findByText("Not cleaned yet: this is the raw OCR text.")).toBeTruthy();
    expect(screen.getByText("Рахунок № 1")).toBeTruthy();
  });
});
