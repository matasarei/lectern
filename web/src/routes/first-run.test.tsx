import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FirstRunPage } from "./first-run";

const GIB = 1024 ** 3;

vi.mock("@tanstack/react-router", () => ({ useNavigate: () => vi.fn() }));

const stopped = { runtime: "docker", version: null, engine_running: false, found: ["docker"] };
const running = { runtime: "docker", version: "28.1", engine_running: true, found: ["docker"] };

let sandboxAnswer: unknown;
let firstRun: Record<string, unknown> | null = null; // overrides /api/first-run
let posts: string[] = [];
let sandboxFails = false;
let sandboxChecks = 0;

const responses = (): Record<string, unknown> => ({
  "/api/system": {
    hardware: {
      os: "macos", arch: "arm64", cpu: "M4", cpu_cores: 10, ram_total: 16 * GIB,
      ram_available: 8 * GIB, gpu_budget: 10 * GIB, gpus: [],
    },
    tier: "recommended",
    disk_free: 100 * 10 ** 9,
    sandbox: stopped,
  },
  "/api/first-run": {
    step: "hardware",
    chat_model: {
      id: "qwen3.5-9b", title: "Qwen3.5 9B", quant: "Q4_K_M", size: 6 * 10 ** 9, licence: "Apache-2.0",
      download: { model_id: "qwen3.5-9b", status: "none", bytes_done: 0, bytes_total: 1, rate: 0, error: null },
    },
  },
});

beforeEach(() => {
  sandboxAnswer = stopped;
  sandboxFails = false;
  sandboxChecks = 0;
  firstRun = null;
  posts = [];
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/events")) return new Response(new ReadableStream({ start: (c) => c.close() }));
      if (init?.method === "POST") {
        posts.push(url);
        return new Response(JSON.stringify(firstRun ?? responses()["/api/first-run"]), { status: 202 });
      }
      if (url === "/api/first-run" && firstRun) return new Response(JSON.stringify(firstRun));
      if (url === "/api/system/sandbox") {
        sandboxChecks += 1;
        if (sandboxFails) throw new TypeError("Failed to fetch");
        return new Response(JSON.stringify(sandboxAnswer));
      }
      return new Response(JSON.stringify(responses()[url]));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <FirstRunPage />
    </QueryClientProvider>,
  );
}

describe("First run: the sandbox card", () => {
  it("shows Docker once it starts, without a reload", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderPage();
    expect((await screen.findByTestId("sandbox-status")).textContent).toMatch(/not running/);
    expect(sandboxChecks).toBe(0);

    sandboxAnswer = running;
    await act(() => vi.advanceTimersByTimeAsync(10_000));

    expect(await screen.findByText(/Docker 28.1 is running/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Check again" })).toBeNull();
    const checks = sandboxChecks;
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(sandboxChecks).toBe(checks); // no more polling once it's running
  });

  it("checks at once when asked", async () => {
    renderPage();
    const button = await screen.findByRole("button", { name: "Check again" });

    sandboxAnswer = running;
    fireEvent.click(button);

    expect(await screen.findByText(/Docker 28.1 is running/)).toBeTruthy();
    expect(sandboxChecks).toBe(1);
  });

  it("says so when a check fails", async () => {
    renderPage();
    const button = await screen.findByRole("button", { name: "Check again" });

    sandboxFails = true;
    fireEvent.click(button);

    expect((await screen.findByRole("alert")).textContent).toMatch(/Lectern didn't answer/);
    expect(screen.getByTestId("sandbox-status").textContent).toMatch(/not running/);
  });
});


const engine = (status: string, done = 0, error: string | null = null) => ({
  variant: "linux-cuda12", status, bytes_done: done, bytes_total: 400 * 10 ** 6, error,
});

function atDownload(engineState: unknown, chatStatus = "none"): Record<string, unknown> {
  const run = responses()["/api/first-run"] as { chat_model: { download: object } };
  return {
    ...run,
    step: "download",
    chat_model: {
      ...run.chat_model,
      download: { ...run.chat_model.download, status: chatStatus, bytes_done: chatStatus === "done" ? 1 : 0 },
    },
    engine: engineState,
  };
}

describe("First run: the engine", () => {
  it("downloads the engine together with the chat model", async () => {
    firstRun = atDownload(engine("none"));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Download" }));

    await vi.waitFor(() =>
      expect(posts.sort()).toEqual(["/api/first-run/engine", "/api/models/qwen3.5-9b/download"]),
    );
    expect(screen.getByTestId("engine-status").textContent).toMatch(/llama\.cpp/);
  });

  it("shows the engine's progress, and Finish only once both are there", async () => {
    firstRun = atDownload(engine("downloading", 100 * 10 ** 6), "done");
    renderPage();

    expect(await screen.findByText(/100 MB of 400 MB/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Finish setup" })).toBeNull();
  });

  it("says why the engine stopped and tries again", async () => {
    firstRun = atDownload(engine("failed", 0, "no network"), "done");
    renderPage();

    expect((await screen.findByRole("alert")).textContent).toBe("no network");
    fireEvent.click(screen.getByRole("button", { name: "Try the engine again" }));
    await vi.waitFor(() => expect(posts).toEqual(["/api/first-run/engine"]));
  });
});
