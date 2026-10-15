import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Tab } from "../api/types";
import { ExportButtons } from "./export-buttons";

let posts: unknown[] = [];

beforeEach(() => {
  posts = [];
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/exporters") {
        return new Response(JSON.stringify([
          { format: "docx", extension: ".docx", capabilities: ["tables", "comments"] },
          { format: "md", extension: ".md", capabilities: ["tables"] },
        ]));
      }
      if (url === "/api/projects/1/export" && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        posts.push(body);
        if (body.format === "md" && !body.accept_loss) {
          return new Response(
            JSON.stringify({ detail: "MD has no comments: the open questions become inline marks.", lost: ["comments"] }),
            { status: 409 },
          );
        }
        return new Response(JSON.stringify({ id: body.format === "md" ? 31 : 30, name: `Lease.${body.format}` }), { status: 201 });
      }
      return new Response("{}");
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderButtons(onOpen: (tab: Tab) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ExportButtons projectId={1} artifactId={7} onOpen={onOpen} />
    </QueryClientProvider>,
  );
}

describe("ExportButtons", () => {
  it("offers the installed formats and opens the file it makes", async () => {
    const opened: Tab[] = [];
    renderButtons((tab) => opened.push(tab));
    fireEvent.click(await screen.findByRole("button", { name: "DOCX" }));
    await waitFor(() => expect(opened).toEqual([{ kind: "artifact", id: 30 }]));
    expect(posts).toEqual([{ artifact_id: 7, format: "docx", accept_loss: false }]);
    expect(screen.getByRole("button", { name: "MD" })).toBeTruthy();
  });

  it("says what a format loses, and exports only once told to go on", async () => {
    const opened: Tab[] = [];
    renderButtons((tab) => opened.push(tab));
    fireEvent.click(await screen.findByRole("button", { name: "MD" }));
    expect(await screen.findByText("MD has no comments: the open questions become inline marks.")).toBeTruthy();
    expect(opened).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alert")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "MD" }));
    fireEvent.click(await screen.findByRole("button", { name: "Export anyway" }));
    await waitFor(() => expect(opened).toEqual([{ kind: "artifact", id: 31 }]));
    expect(posts.at(-1)).toEqual({ artifact_id: 7, format: "md", accept_loss: true });
  });

  it("says so when the formats can't be read, rather than showing nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "boom" }), { status: 500 })));
    renderButtons(() => {});
    expect(await screen.findByText("The export formats couldn't be read: boom")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
