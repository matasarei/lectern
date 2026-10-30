import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OcrDocument } from "../api/types";
import { block, doubt, documentOf } from "./ocr-fixtures";
import { ReviewPage } from "./review";

let calls: { method: string; url: string; body: unknown }[] = [];

beforeEach(() => {
  calls = [];
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify({ version: 2, artifact_id: 40, open_doubts: 1 }));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const lease = (): OcrDocument =>
  documentOf([
    block("b1.1", "3. Орендна плата"),
    block("b1.2", "Оплата до {d1.1} числа, вул. {d1.2}, 26.", {
      doubts: [
        doubt("d1.1", "1?", { candidates: ["1?", "15", "18"], reason: "smudged" }),
        doubt("d1.2", "Хр?щатик", { candidates: ["Хрещатик"], source: "ocr" }),
      ],
    }),
    block("b1.3", "", {
      type: "table",
      rows: [["Період", "Сума"], ["квітень 1998", "{d1.3}"]],
      doubts: [doubt("d1.3", "2?0,00", { status: "resolved", value: "250,00", candidates: ["250,00"] })],
    }),
    block("b3.1", "Підпис {d3.1}", { doubts: [doubt("d3.1", "<b>Ков</b>")] }),
  ]);

function renderPage(doc: OcrDocument = lease(), page = 1) {
  const selected: number[] = [];
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <button>outside</button>
      <ReviewPage projectId={1} sessionId={2} doc={doc} page={page} onSelectPage={(n) => selected.push(n)} />
    </QueryClientProvider>,
  );
  return selected;
}

const card = () => screen.getByTestId("question-card");
const area = () => screen.getByTestId("review");

describe("reviewing a page", () => {
  it("puts the card for the first open question under its block, with the suggestion", () => {
    renderPage();
    const holder = screen.getByText("1?", { selector: "mark" }).closest("div.grid")!;
    expect(within(holder as HTMLElement).getByTestId("question-card")).toBeTruthy();
    expect(within(card()).getByText("1? → 15")).toBeTruthy();
    expect(within(card()).getByText("smudged")).toBeTruthy();
    expect(screen.getByTestId("question-bar").textContent).toContain("Page 1: 2 questions · document: 3");
    expect(screen.getByText("250,00").tagName).toBe("SPAN"); // answered: its value, in the table
  });

  it("accepts with Enter and moves to the next question", async () => {
    renderPage();
    fireEvent.keyDown(area(), { key: "Enter" });
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      method: "PUT",
      url: "/api/projects/1/ocr/2/doubts/d1.1",
      body: { value: "15", action: "accept" },
    });
    await waitFor(() => expect(within(card()).getByText("Хр?щатик → Хрещатик")).toBeTruthy());
    expect(within(card()).getByText(/OCR/)).toBeTruthy();
  });

  it("moves on with Tab (Later) without saving anything, and wraps round on the page", () => {
    renderPage();
    fireEvent.keyDown(area(), { key: "Tab" });
    expect(within(card()).getByText("Хр?щатик → Хрещатик")).toBeTruthy();
    fireEvent.keyDown(area(), { key: "Tab" });
    expect(within(card()).getByText("1? → 15")).toBeTruthy();
    expect(calls).toEqual([]);
  });

  it("edits with E; typing in the box never triggers the keys", async () => {
    renderPage();
    fireEvent.keyDown(area(), { key: "e" });
    const input = screen.getByLabelText("The reading");
    fireEvent.keyDown(input, { key: "e" });
    fireEvent.keyDown(input, { key: "Tab" });
    fireEvent.change(input, { target: { value: "16" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ value: "16", action: "edit" });
  });

  it("leaves the keys alone while focus is outside the review", () => {
    renderPage();
    const outside = screen.getByRole("button", { name: "outside" });
    fireEvent.keyDown(outside, { key: "Enter" });
    fireEvent.keyDown(outside, { key: "Tab" });
    expect(calls).toEqual([]);
    expect(within(card()).getByText("1? → 15")).toBeTruthy();
  });

  it("leaves Enter and Tab to a button inside the review", () => {
    renderPage();
    const laterButton = within(card()).getByRole("button", { name: "Later" });
    fireEvent.keyDown(laterButton, { key: "Enter" });
    fireEvent.keyDown(laterButton, { key: "Tab" });
    expect(calls).toEqual([]); // not accepted
    expect(within(card()).getByText("1? → 15")).toBeTruthy(); // not skipped either
  });

  it("keeps the reading as it is with K: recorded as kept, not as an edit", async () => {
    renderPage();
    fireEvent.keyDown(area(), { key: "k" });
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toMatchObject({ url: "/api/projects/1/ocr/2/doubts/d1.1", body: { action: "keep" } });
  });

  it("accepts any one of the candidates by clicking it", async () => {
    renderPage();
    expect(within(card()).getAllByRole("button", { name: "Accept 15" })).toHaveLength(1); // the suggestion, once
    fireEvent.click(within(card()).getByRole("button", { name: "Accept 18" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ value: "18", action: "accept" });
  });

  it("on a page's last open question, Later opens the next page with questions", () => {
    const selected = renderPage(lease(), 3);
    fireEvent.keyDown(area(), { key: "Tab" });
    expect(selected).toEqual([1]); // wrapping round to the first
    expect(calls).toEqual([]);
  });

  it("on the document's only open question, Later says so", () => {
    const doc = documentOf([block("b1.1", "Оплата до {d1.1} числа.", { doubts: [doubt("d1.1", "1?", { candidates: ["15"] })] })]);
    const selected = renderPage(doc);
    fireEvent.click(within(card()).getByRole("button", { name: "Later" }));
    expect(selected).toEqual([]);
    expect(within(card()).getByText("This is the document's only open question.")).toBeTruthy();
  });

  it("offers no Accept when the suggestion is the reading itself; Enter keeps it", async () => {
    const doc = documentOf([
      block("b1.1", "вісімнадцять {d1.1} гривень", {
        doubts: [doubt("d1.1", "тисяч", { candidates: ["тисяч"], reason: "fixed" })],
      }),
    ]);
    renderPage(doc);
    expect(within(card()).getByText("тисяч", { selector: "p" })).toBeTruthy(); // no "тисяч → тисяч"
    expect(within(card()).queryByRole("button", { name: /^Accept/ })).toBeNull();
    fireEvent.keyDown(area(), { key: "Enter" });
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ action: "keep" });
  });

  it("offers Remove only for a question that is all of its block", async () => {
    const doc = lease();
    doc.blocks.splice(2, 0, block("b1.4", "{d1.4}.", { doubts: [doubt("d1.4", "луги")] }));
    doc.pages[0].blocks.splice(2, 0, "b1.4");
    renderPage(doc);
    expect(within(card()).queryByRole("button", { name: "Remove" })).toBeNull(); // "Оплата до 1? …"
    fireEvent.click(screen.getByText("луги", { selector: "mark" }));
    fireEvent.click(within(card()).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toMatchObject({ url: "/api/projects/1/ocr/2/doubts/d1.4", body: { action: "remove" } });
  });

  it("shows a removed block struck out, and a kept reading, each with Reopen", () => {
    const doc = lease();
    doc.blocks[1].doubts[0] = doubt("d1.1", "1?", { status: "resolved", value: "1?" });
    doc.blocks.splice(2, 0, block("b1.4", "{d1.4}.", { doubts: [doubt("d1.4", "луги", { status: "resolved", value: "" })] }));
    doc.pages[0].blocks.splice(2, 0, "b1.4");
    renderPage(doc);
    expect(screen.getByText("луги", { selector: "s" })).toBeTruthy();
    for (const chip of ["луги · removed", "1? · kept"]) {
      expect(within(screen.getByText(chip).closest("li")!).getByRole("button", { name: "Reopen" })).toBeTruthy();
    }
  });

  it("reopens an answered question", async () => {
    const doc = lease();
    doc.blocks[1].doubts[0] = doubt("d1.1", "1?", { status: "resolved", value: "15" });
    renderPage(doc);
    const answered = screen.getByText("1? → 15").closest("li")!;
    fireEvent.click(within(answered).getByRole("button", { name: "Reopen" }));
    await waitFor(() => expect(calls[0]).toMatchObject({ method: "DELETE", url: "/api/projects/1/ocr/2/doubts/d1.1" }));
  });

  it("offers the next page with questions", () => {
    const selected = renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Next page with questions ›" }));
    expect(selected).toEqual([3]);
  });

  it("shows a page's text as text, never as HTML", () => {
    renderPage(lease(), 3);
    expect(screen.getByText("<b>Ков</b>", { selector: "mark" })).toBeTruthy();
    expect(document.querySelector("mark b")).toBeNull();
  });

  it("says which answers are no longer in the page's text, on their page only", () => {
    const doc = {
      ...lease(),
      unplaced: [
        { page: 1, doubt_id: "d1.9", raw: "1O", value: "10", action: "edit" },
        { page: 1, doubt_id: "d1.8", raw: "луги.", value: "", action: "remove" },
        { page: 3, doubt_id: "d3.9", raw: "Ков?", value: "Коваль", action: "accept" },
      ],
    };
    renderPage(doc, 1);
    const notes = screen.getAllByTestId("unplaced");
    expect(notes.map((n) => n.textContent)).toEqual([
      "Your answer “10” to “1O” is no longer in this page's text: the page was cleaned again. Check it in the text below.",
      "You removed “луги.”, but the page was cleaned again and it is part of a longer block now, so it wasn't removed. Check it in the text below.",
    ]);
  });
});
