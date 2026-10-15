import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Artifact, ProjectItems, Tab } from "../api/types";
import { FileTab, TEXT_PREVIEW_LIMIT } from "./file";

const artifact = (over: Partial<Artifact>): Artifact => ({
  id: 4, name: "reply.md", kind: "generated", mime: "text/markdown", size: 40, text: "indexed",
  text_note: null, from: { kind: "session", id: 1, note: null, title: "Payment terms" },
  created_at: "", modified_at: "", ...over,
});

const ITEMS: ProjectItems = {
  sessions: [{ id: 1, kind: "chat", title: "Payment terms", messages: 2, ocr: null, created_at: "", updated_at: "" }],
  artifacts: [],
};

let requested: string[] = [];
let body = "# Reply\n<script>alert(1)</script>"; // what a file's content answers

beforeEach(() => {
  requested = [];
  body = "# Reply\n<script>alert(1)</script>";
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requested.push(url);
      expect(new Headers(init?.headers).get("X-Lectern-Token")).toBe("t");
      if (url.endsWith("/5/content")) return new Response(new Blob(["png"], { type: "image/png" }));
      if (url === "/api/projects/1/ocr") return new Response(JSON.stringify({ id: 7, kind: "ocr" }));
      return new Response(body);
    }),
  );
  // Still a constructor: loading a module on demand (the Markdown viewer) makes URLs.
  class PreviewUrl extends URL {
    static createObjectURL = () => "blob:preview";
    static revokeObjectURL = () => {};
  }
  vi.stubGlobal("URL", PreviewUrl);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderFile(a: Artifact | undefined, onOpen: (tab: Tab) => void = () => {}, items = ITEMS) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <FileTab projectId={1} artifact={a} items={items} onOpen={onOpen} />
    </QueryClientProvider>,
  );
}

describe("File tab", () => {
  it("shows text as text, never as HTML", async () => {
    const { container } = renderFile(artifact({ name: "notes.txt", mime: "text/plain" }));
    const pre = await screen.findByText(/<script>alert\(1\)<\/script>/);
    expect(pre.tagName).toBe("PRE");
    expect(container.querySelector("script")).toBeNull();
    expect(requested).toEqual(["/api/projects/1/artifacts/4/content"]);
  });

  it("shows Markdown as Markdown, its raw HTML never as HTML", async () => {
    const { container } = renderFile(artifact({}));
    expect((await screen.findByText("Reply")).tagName).toBe("H3");
    expect(screen.getByTestId("markdown-preview")).toBeTruthy();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("pre")).toBeNull();
  });

  it("keeps a published Document's lines, and joins them in other Markdown", async () => {
    body = "Account 1234\nBalance 56.00";
    const ocr = {
      ...ITEMS,
      sessions: [...ITEMS.sessions, { id: 2, kind: "ocr" as const, title: "statement.pdf", messages: 0,
                                     ocr: null, created_at: "", updated_at: "" }],
    };  // prettier-ignore
    const published = artifact({ name: "document.md", from: { kind: "session", id: 2, note: null, title: null } });
    const { container } = renderFile(published, () => {}, ocr);
    await screen.findByTestId("markdown-preview");
    await waitFor(() => expect(container.querySelector("p")?.innerHTML).toBe("Account 1234<br>\nBalance 56.00"));
    cleanup();

    const written = renderFile(artifact({}), () => {}, ocr); // a chat's own .md: Markdown's rules
    await screen.findByTestId("markdown-preview");
    await waitFor(() => expect(written.container.querySelector("p")?.textContent).toBe("Account 1234\nBalance 56.00"));
    expect(written.container.querySelector("br")).toBeNull();
  });

  it("shows where it came from, and opens it", () => {
    const opened: Tab[] = [];
    renderFile(artifact({}), (tab) => opened.push(tab));
    expect(screen.getByText("written in Payment terms")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open Payment terms" }));
    expect(opened).toEqual([{ kind: "session", id: 1 }]);
  });

  it("shows images from a blob URL", async () => {
    renderFile(artifact({ id: 5, name: "stamp.png", mime: "image/png", text: "none", text_note: "no text" }));
    const img = await screen.findByAltText("Preview of stamp.png");
    expect(img.getAttribute("src")).toBe("blob:preview");
  });

  it("offers a download for other files, and big text files", () => {
    renderFile(
      artifact({ name: "scan.pdf", mime: "application/pdf", kind: "imported", text: "none",
                 text_note: "no text layer", from: { kind: "import", id: null, note: null, title: null } }),
    );  // prettier-ignore
    expect(screen.getByText("This kind of file can't be shown here yet.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Download" })).toBeTruthy();
    expect(screen.getByText("no text layer")).toBeTruthy();
    expect(screen.getByText("imported")).toBeTruthy();
    expect(requested).toEqual([]);
    cleanup();

    renderFile(artifact({ size: TEXT_PREVIEW_LIMIT + 1 }));
    expect(screen.getByText("This file is too large to show here. Download it to open it.")).toBeTruthy();
    expect(requested).toEqual([]);
  });

  it("opens a PDF or an image as an OCR session in a new tab", async () => {
    const opened: Tab[] = [];
    renderFile(
      artifact({ id: 6, name: "lease-scan.PDF", mime: "application/pdf", kind: "imported" }),
      (tab) => opened.push(tab),
    );
    fireEvent.click(screen.getByRole("button", { name: "Open as OCR session" }));
    await waitFor(() => expect(opened).toEqual([{ kind: "session", id: 7 }]));
    expect(requested).toContain("/api/projects/1/ocr");
  });

  it("offers OCR only for PDFs and images", () => {
    renderFile(artifact({ name: "reply-letter.md" }));
    expect(screen.queryByRole("button", { name: "Open as OCR session" })).toBeNull();
  });

  it("says so when the file is gone", () => {
    renderFile(undefined);
    expect(screen.getByText("This file is no longer in the project.")).toBeTruthy();
  });
});
