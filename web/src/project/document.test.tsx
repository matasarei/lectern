import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OcrDocument, OcrSession, ProjectItems, Tab } from "../api/types";
import { DocumentTab } from "./document";
import { block, doubt, documentOf, session } from "./ocr-fixtures";

let current: OcrSession;
let currentDoc: OcrDocument;

const ITEMS: ProjectItems = {
  sessions: [],
  artifacts: [
    {
      id: 5, name: "lease-scan.pdf", kind: "imported", mime: "application/pdf", size: 1, text: "none",
      text_note: null, from: { kind: "import", id: null, note: null, title: null }, created_at: "",
      modified_at: "",
    },
  ],
};

beforeEach(() => {
  current = session({ status: "done", document: { version: 7, artifact_id: 40, open_doubts: 1 } });
  currentDoc = documentOf([
    block("b1.1", "3. Орендна плата", { type: "heading", level: 2 }),
    block("b1.2", "Оплата до {d2.1} числа, що діє", {
      pages: [1, 2],
      doubts: [doubt("d2.1", "1?", { candidates: ["15"] })],
    }),
    block("b2.1", "Сума {d2.2}", { doubts: [doubt("d2.2", "2?0,00", { status: "resolved", value: "250,00" })] }),
  ]);
  currentDoc.version = 7;
  currentDoc.pages[0].note = "The stamp is the landlord's.";
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      new Response(JSON.stringify(url.endsWith("/document") ? currentDoc : current)),
    ),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderTab() {
  const opened: Tab[] = [];
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <DocumentTab projectId={1} sessionId={2} items={ITEMS} onOpen={(tab) => opened.push(tab)} />
    </QueryClientProvider>,
  );
  return opened;
}

describe("Document tab", () => {
  it("shows the whole Document once, its open questions marked and answered ones as read", async () => {
    renderTab();
    expect(await screen.findByText("3. Орендна плата")).toBeTruthy();
    expect(screen.getAllByText(/Оплата до/)).toHaveLength(1); // runs onto page 2, shown once
    expect(screen.getByText("1?", { selector: "mark" })).toBeTruthy();
    expect(screen.getByText("250,00")).toBeTruthy();
  });

  it("says where it came from, and each link opens its tab", async () => {
    const opened = renderTab();
    await screen.findByText("3. Орендна плата");
    expect(screen.getByText("Version").nextSibling?.textContent).toBe("7");
    expect(screen.getByText("Open questions").nextSibling?.textContent).toBe("1");
    expect(screen.getByText("Page 1: The stamp is the landlord's.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "lease-scan.pdf" }));
    fireEvent.click(screen.getByRole("button", { name: "OCR session · lease-scan.pdf" }));
    expect(opened).toEqual([{ kind: "artifact", id: 5 }, { kind: "session", id: 2 }]);
  });

  it("opens the OCR tab at the page a question came from", async () => {
    const opened = renderTab();
    fireEvent.click(await screen.findByText("1?", { selector: "mark" }));
    expect(opened).toEqual([{ kind: "session", id: 2, page: 2 }]);
  });

  it("says so when the session has no Document yet", async () => {
    current = session({ document: null });
    renderTab();
    expect(await screen.findByText("This session has no Document yet.")).toBeTruthy();
  });
});
