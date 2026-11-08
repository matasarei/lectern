import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Artifact, ProjectItems, Tab } from "../api/types";
import { FileTab, PDF_PREVIEW_LIMIT, TEXT_PREVIEW_LIMIT } from "./file";

const artifact = (over: Partial<Artifact>): Artifact => ({
  id: 4, name: "reply.md", kind: "generated", mime: "text/markdown", size: 40, text: "indexed",
  text_note: null, preview: "markdown", from: { kind: "session", id: 1, note: null, title: "Payment terms" },
  created_at: "", modified_at: "", ...over,
});

const ITEMS: ProjectItems = {
  sessions: [{ id: 1, kind: "chat", title: "Payment terms", messages: 2, ocr: null, created_at: "", updated_at: "" }],
  artifacts: [],
};

let requested: string[] = [];
let body: BodyInit = "# Reply\n<script>alert(1)</script>"; // what a file's content answers
let viewFails = false; // an HTML file's link can't be had (a failed renewal)

beforeEach(() => {
  requested = [];
  body = "# Reply\n<script>alert(1)</script>";
  viewFails = false;
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requested.push(url);
      expect(new Headers(init?.headers).get("X-Lectern-Token")).toBe("t");
      if (url.endsWith("/5/content")) return new Response(new Blob(["png"], { type: "image/png" }));
      if (url === "/api/projects/1/ocr") return new Response(JSON.stringify({ id: 7, kind: "ocr" }));
      if (url.endsWith("/8/markdown")) {
        return new Response(JSON.stringify({ detail: "lease.odt couldn't be read as a document (TypeError)." }),
                            { status: 400 });  // prettier-ignore
      }
      if (url.endsWith("/10/view")) return new Response(JSON.stringify({ detail: "No such file." }), { status: 404 });
      if (url.endsWith("/view") && viewFails) {
        return new Response(JSON.stringify({ detail: "Lectern stopped." }), { status: 503 });
      }
      if (url.endsWith("/view")) return new Response(JSON.stringify({ url: "/view/tkt" }));
      if (url.endsWith("/9/markdown")) return new Response(JSON.stringify({ markdown: "# Long", cut: true }));
      if (url.endsWith("/markdown")) {
        return new Response(JSON.stringify({ markdown: "# Lease\n\n| a | b |\n|---|---|\n| 1 | 2 |", cut: false }));
      }
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
    const { container } = renderFile(artifact({ name: "notes.txt", mime: "text/plain", preview: "text" }));
    const pre = await screen.findByText(/<script>alert\(1\)<\/script>/);
    expect(pre.tagName).toBe("PRE");
    expect(container.querySelector("script")).toBeNull();
    expect(requested).toEqual(["/api/projects/1/artifacts/4/content"]);
  });

  it("shows code and data files as text", async () => {
    body = '{"rent": 1200}';
    renderFile(artifact({ name: "terms.json", mime: "application/json", preview: "text" }));
    expect((await screen.findByText('{"rent": 1200}')).tagName).toBe("PRE");
  });

  it("reads a text file that isn't UTF-8 as windows-1251, as the chat does", async () => {
    body = new Uint8Array([196, 238, 227, 238, 226, 179, 240, 32, 238, 240, 229, 237, 228, 232]);
    renderFile(artifact({ name: "lease.txt", mime: "text/plain", preview: "text" }));
    expect((await screen.findByText("Договір оренди")).tagName).toBe("PRE");
  });

  it("reads UTF-8 as UTF-8, without its byte-order mark", async () => {
    body = new Uint8Array([0xef, 0xbb, 0xbf, ...new TextEncoder().encode("Договір оренди")]);
    renderFile(artifact({ name: "lease.txt", mime: "text/plain", preview: "text" }));
    expect((await screen.findByText(/Договір оренди/)).textContent).toBe("Договір оренди");
  });

  it("shows an HTML file's source as text, never as HTML", async () => {
    body = "<h1>Hi</h1><script>alert(1)</script>";
    const { container } = renderFile(artifact({ name: "page.html", mime: "text/html", preview: "html" }));
    expect((await screen.findByText(body)).tagName).toBe("PRE");
    expect(container.querySelector("h1, script")).toBeNull();
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

  it("shows a page opened from the web by its host, and links its address", () => {
    const url = "https://docs.python.org/3/library/html.parser.html";
    renderFile(artifact({ name: "docs.python.org-html-parser.md",
                          from: { kind: "import", id: null, note: url, title: null } }));
    expect(screen.getByText("Opened from docs.python.org in a chat:")).toBeTruthy();
    const link = screen.getByRole("link", { name: url });
    expect(link.getAttribute("href")).toBe(url);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("links no note that isn't a web address", () => {
    for (const note of ["javascript:alert(1)", "from the old archive", "file:///etc/passwd"]) {
      const { unmount } = renderFile(artifact({ from: { kind: "import", id: null, note, title: null } }));
      expect(screen.getByText("imported")).toBeTruthy();
      expect(screen.queryByRole("link")).toBeNull();
      unmount();
    }
  });

  it("shows images from a blob URL", async () => {
    renderFile(artifact({ id: 5, name: "stamp.png", mime: "image/png", text: "none", text_note: "no text",
                        preview: "image" }));
    const img = await screen.findByAltText("Preview of stamp.png");
    expect(img.getAttribute("src")).toBe("blob:preview");
  });

  it("offers a download for other files, and big text files", () => {
    renderFile(
      artifact({ name: "archive.zip", mime: "application/zip", kind: "imported", text: "none", preview: null,
                 text_note: "no text to read", from: { kind: "import", id: null, note: null, title: null } }),
    );  // prettier-ignore
    expect(screen.getByText("This kind of file can't be shown here yet.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Download" })).toBeTruthy();
    expect(screen.getByText("no text to read")).toBeTruthy();
    expect(screen.getByText("imported")).toBeTruthy();
    expect(requested).toEqual([]);
    cleanup();

    renderFile(artifact({ size: TEXT_PREVIEW_LIMIT + 1 }));
    expect(screen.getByText("This file is too large to show here. Download it to open it.")).toBeTruthy();
    expect(requested).toEqual([]);
  });

  it("opens an HTML file from a link of its own, in a tab that starts with nothing of this one's", async () => {
    vi.stubGlobal("open", vi.fn());
    renderFile(artifact({ id: 4, name: "page.html", mime: "text/html", preview: "html" }));
    const link = await screen.findByRole("link", { name: "Open as a page in a new tab" });
    expect(link.getAttribute("href")).toBe("/view/tkt");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(requested).toContain("/api/projects/1/artifacts/4/view");
    expect(window.open).not.toHaveBeenCalled();
  });

  it("asks for a new link once one has been used", async () => {
    renderFile(artifact({ id: 4, name: "page.html", mime: "text/html", preview: "html" }));
    const link = await screen.findByRole("link", { name: "Open as a page in a new tab" });
    const asked = () => requested.filter((u) => u.endsWith("/4/view")).length;
    const before = asked();
    link.addEventListener("click", (e) => e.preventDefault()); // jsdom doesn't open tabs
    fireEvent.click(link);
    await waitFor(() => expect(asked()).toBe(before + 1));
  });

  it("keeps the link it has when renewing it fails, and says why", async () => {
    renderFile(artifact({ id: 4, name: "page.html", mime: "text/html", preview: "html" }));
    const link = await screen.findByRole("link", { name: "Open as a page in a new tab" });
    viewFails = true;
    link.addEventListener("click", (e) => e.preventDefault()); // jsdom doesn't open tabs
    fireEvent.click(link);
    expect(await screen.findByText("Lectern stopped.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open as a page in a new tab" })).toBeTruthy();
  });

  it("says why, and offers no link, when one can't be had", async () => {
    renderFile(artifact({ id: 10, name: "page.html", mime: "text/html", preview: "html" }));
    expect(await screen.findByText("No such file.")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Open as a page in a new tab" })).toBeNull();
  });

  it("shows a DOCX or ODT as the Markdown the server made of it", async () => {
    const { container } = renderFile(artifact({ name: "lease.docx", mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", preview: "document" }));
    expect((await screen.findByText("Lease")).tagName).toBe("H3");
    expect(screen.getByTestId("document-preview")).toBeTruthy();
    await waitFor(() => expect(container.querySelector("table")).not.toBeNull());
    expect(requested).toEqual(["/api/projects/1/artifacts/4/markdown"]);
    expect(screen.queryByText(/Only the beginning/)).toBeNull();
  });

  it("says when only the beginning of a long document is shown", async () => {
    renderFile(artifact({ id: 9, name: "code.odt", mime: "application/vnd.oasis.opendocument.text", preview: "document" }));
    expect(await screen.findByText("Only the beginning of this document is shown here. Download it to see all of it.")).toBeTruthy();
    expect((await screen.findByText("Long")).tagName).toBe("H3");
  });

  it("says so when a document can't be read", async () => {
    renderFile(artifact({ id: 8, name: "lease.odt", mime: "application/vnd.oasis.opendocument.text", preview: "document" }));
    expect(await screen.findByText("lease.odt couldn't be read as a document (TypeError).")).toBeTruthy();
  });

  it("says an old Word or RTF file can't be shown, and offers it to download", () => {
    renderFile(artifact({ name: "lease-1998.doc", mime: "application/msword", preview: "legacy" }));
    expect(screen.getByText(/Word 97–2003 and RTF files can't be shown here/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Download" })).toBeTruthy();
    expect(requested).toEqual([]);
  });

  it("shows a PDF in the browser's viewer, from a blob URL typed as a PDF", async () => {
    const made: Blob[] = [];
    vi.stubGlobal("URL", class extends URL {
      static createObjectURL = (b: Blob) => (made.push(b), "blob:pdf");
      static revokeObjectURL = () => {};
    });  // prettier-ignore
    renderFile(artifact({ name: "lease.pdf", mime: "application/pdf", preview: "pdf" }));
    const frame = await screen.findByTitle("lease.pdf in the browser's PDF viewer");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame.getAttribute("src")).toBe("blob:pdf");
    expect(made.map((b) => b.type)).toEqual(["application/pdf"]);
    expect(requested).toEqual(["/api/projects/1/artifacts/4/content"]);
  });

  it("offers a PDF too large to show as a download", () => {
    renderFile(artifact({ name: "atlas.pdf", mime: "application/pdf", preview: "pdf", size: PDF_PREVIEW_LIMIT + 1 }));
    expect(screen.getByText("This file is too large to show here. Download it to open it.")).toBeTruthy();
    expect(requested).toEqual([]);
  });

  it("opens a PDF or an image as an OCR session in a new tab", async () => {
    const opened: Tab[] = [];
    renderFile(
      artifact({ id: 6, name: "lease-scan.PDF", mime: "application/pdf", kind: "imported", preview: "pdf" }),
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
