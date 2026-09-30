import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Artifact, ProjectItems, Tab } from "../api/types";
import { Overview } from "./overview";

const artifact = (id: number, name: string, over: Partial<Artifact> = {}): Artifact => ({
  id, name, kind: "imported", mime: "application/pdf", size: 14_200_000, text: "none",
  text_note: "no text layer (a scan: open it as an OCR session)",
  from: { kind: "import", id: null, note: null, title: null },
  created_at: "2026-09-28T10:00:00Z", modified_at: "2026-09-28T10:00:00Z", ...over,
});

const ITEMS: ProjectItems = {
  sessions: [
    { id: 1, kind: "chat", title: "Payment terms", messages: 14, ocr: null, created_at: "", updated_at: "2026-09-29T10:00:00Z" },
  ],
  artifacts: [
    artifact(3, "lease-1998-scan.pdf"),
    artifact(4, "reply-letter.md", {
      kind: "generated", mime: "text/markdown", size: 3000, text: "indexed", text_note: null,
      from: { kind: "session", id: 1, note: null, title: "Payment terms" },
      modified_at: "2026-09-29T11:00:00Z",
    }),
  ],
};

let requests: { url: string; init?: RequestInit }[] = [];

beforeEach(() => {
  requests = [];
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requests.push({ url, init });
      if (url.startsWith("/api/projects/1/search")) {
        return new Response(JSON.stringify([
          { artifact_id: 4, name: "reply-letter.md", ref: "#c2", heading: "Penalty", snippet: "0,5% a day" },
        ]));
      }
      if (url === "/api/projects/1/ocr") return new Response(JSON.stringify({ id: 7, kind: "ocr" }));
      if (url === "/api/projects/1/sessions") {
        return new Response(JSON.stringify({ id: 2, kind: "chat", title: "New chat", messages: 0 }));
      }
      return new Response(JSON.stringify({ id: 9, name: "x" }));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderOverview(onOpen: (tab: Tab) => void = () => {}, items: ProjectItems = ITEMS) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Overview projectId={1} items={items} onOpen={onOpen} />
    </QueryClientProvider>,
  );
}

describe("Overview", () => {
  it("shows an OCR session with its pages and status, and opens it in a tab", () => {
    const opened: Tab[] = [];
    const ocr = {
      id: 2, kind: "ocr" as const, title: "lease-1998-scan.pdf", messages: 0,
      ocr: { pages: 12, status: "paused" as const }, created_at: "", updated_at: "2026-09-29T09:00:00Z",
    };
    renderOverview((tab) => opened.push(tab), { sessions: [ocr], artifacts: [] });
    const row = screen.getAllByRole("row")[1];
    expect(within(row).getByText("OCR session")).toBeTruthy();
    expect(within(row).getByText("12 pages · Paused")).toBeTruthy();
    fireEvent.click(row);
    expect(opened).toEqual([{ kind: "session", id: 2 }]);
  });

  it("offers OCR on a PDF's row, which opens the session and not the file", async () => {
    const opened: Tab[] = [];
    renderOverview((tab) => opened.push(tab));
    const buttons = screen.getAllByRole("button", { name: "Open as OCR session" });
    expect(buttons).toHaveLength(1); // the PDF, not the Markdown letter
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(opened).toEqual([{ kind: "session", id: 7 }]));
    const posted = requests.find((r) => r.url === "/api/projects/1/ocr");
    expect(JSON.parse(String(posted?.init?.body))).toEqual({ artifact_id: 3 });
  });

  it("lists everything newest first, with kind, details and From", () => {
    renderOverview();
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[0].textContent)).toEqual([
      "reply-letter.md", "Payment terms", "lease-1998-scan.pdf",
    ]);
    expect(within(rows[0]).getByText("Generated · Markdown")).toBeTruthy();
    expect(screen.getByTestId("from-4").textContent).toBe("written in Payment terms");
    expect(screen.getByTestId("from-3").textContent).toBe("imported");
    expect(within(rows[1]).getByText("14 messages")).toBeTruthy();
    expect(within(rows[2]).getByText(/14.2 MB · no text layer/)).toBeTruthy();
  });

  it("filters by what kind of item it is", () => {
    renderOverview();
    fireEvent.click(screen.getByRole("button", { name: "Produced 1" }));
    expect(screen.getAllByRole("row")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Sessions 1" }));
    expect(screen.getByText("Payment terms")).toBeTruthy();
    expect(screen.queryByText("reply-letter.md")).toBeNull();
  });

  it("opens a row in a tab", () => {
    const opened: Tab[] = [];
    renderOverview((tab) => opened.push(tab));
    fireEvent.click(screen.getByText("lease-1998-scan.pdf"));
    fireEvent.click(screen.getByText("Payment terms"));
    expect(opened).toEqual([{ kind: "artifact", id: 3 }, { kind: "session", id: 1 }]);
  });

  it("imports dropped files, one request per file with its name", async () => {
    renderOverview();
    const files = [new File(["a"], "notes.md"), new File(["b"], "Договір 1998.docx")];
    fireEvent.drop(screen.getByTestId("overview"), { dataTransfer: { files } });

    await waitFor(() => expect(requests.filter((r) => r.init?.method === "PUT")).toHaveLength(2));
    const puts = requests.filter((r) => r.init?.method === "PUT");
    expect(puts[0].url).toBe("/api/projects/1/files?name=notes.md");
    expect(puts[1].url).toBe(`/api/projects/1/files?name=${encodeURIComponent("Договір 1998.docx")}`);
    expect(puts[0].init?.body).toBe(files[0]);
  });

  it("starts a new chat and opens it", async () => {
    const opened: Tab[] = [];
    renderOverview((tab) => opened.push(tab));
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    await waitFor(() => expect(opened).toEqual([{ kind: "session", id: 2 }]));
  });

  it("searches the files' text", async () => {
    const opened: Tab[] = [];
    renderOverview((tab) => opened.push(tab));
    fireEvent.change(screen.getByLabelText("Search the text of this project's files"), {
      target: { value: "пеня" },
    });
    fireEvent.click(await screen.findByText("0,5% a day"));
    expect(opened).toEqual([{ kind: "artifact", id: 4 }]);
    expect(requests.some((r) => r.url === `/api/projects/1/search?q=${encodeURIComponent("пеня")}`)).toBe(true);
  });
});
