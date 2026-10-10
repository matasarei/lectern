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
let puts: { url: string; body: unknown }[] = [];
let onPut: ((url: string, body: unknown) => void) | null = null; // what the server would then say
let putRefused: string | null = null;

const idle = (id: string) => ({
  model_id: id, status: "none", bytes_done: 0, bytes_total: 1, rate: 0, error: null,
});
const chatModel = (id: string, title: string, size: number, more: object = {}) => ({
  id, title, quant: "Q4_K_M", size, licence: "Apache-2.0", recommended: false, suits_tier: true,
  download: idle(id),
  mlx_build: { id: `${id}-mlx`, title: `${title} (MLX)`, size: size + 4 * 10 ** 8, download: idle(`${id}-mlx`) },
  ...more,
});
const four = chatModel("qwen3.5-4b", "Qwen3.5 4B", 27 * 10 ** 8);
const nine = chatModel("qwen3.5-9b", "Qwen3.5 9B", 6 * 10 ** 9, { recommended: true });
const big = chatModel("qwen3.5-35b-a3b", "Qwen3.5 35B-A3B", 22 * 10 ** 9, { suits_tier: false });
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
    chat_model: nine,
    chat_models: [four, nine, big],
    mlx: { supported: false, available: false, enabled: false },
  },
});

beforeEach(() => {
  sandboxAnswer = stopped;
  sandboxFails = false;
  sandboxChecks = 0;
  firstRun = null;
  posts = [];
  puts = [];
  onPut = null;
  putRefused = null;
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/events")) return new Response(new ReadableStream({ start: (c) => c.close() }));
      if (init?.method === "POST") {
        posts.push(url);
        return new Response(JSON.stringify(firstRun ?? responses()["/api/first-run"]), { status: 202 });
      }
      if (init?.method === "PUT") {
        const body: unknown = JSON.parse(init.body as string);
        puts.push({ url, body });
        if (putRefused) return new Response(JSON.stringify({ detail: putRefused }), { status: 409 });
        onPut?.(url, body);
        return new Response(JSON.stringify({}));
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

describe("First run: choosing the chat model", () => {
  const options = () =>
    Array.from(screen.getByLabelText<HTMLSelectElement>("Chat model").options).map((o) => o.text);

  it("lists the chat models, the recommended one marked and selected", async () => {
    firstRun = atDownload(engine("none"));
    renderPage();

    const list = await screen.findByLabelText<HTMLSelectElement>("Chat model");

    expect(list.value).toBe("qwen3.5-9b");
    expect(options()).toEqual([
      "Qwen3.5 4B · 2.7 GB",
      "Qwen3.5 9B · 6 GB · recommended",
      "Qwen3.5 35B-A3B · 22 GB · more than this computer is meant for",
    ]);
    expect(screen.queryByText(/it may be slow/)).toBeNull();
  });

  it("saves a pick, and the card and the download follow it", async () => {
    firstRun = atDownload(engine("done"));
    onPut = () => {
      firstRun = { ...atDownload(engine("done")), chat_model: four };
    };
    renderPage();

    fireEvent.change(await screen.findByLabelText("Chat model"), { target: { value: "qwen3.5-4b" } });

    expect(await screen.findByText("2.7 GB")).toBeTruthy();
    expect(puts).toEqual([{ url: "/api/models/roles/main", body: { model_id: "qwen3.5-4b" } }]);
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    await vi.waitFor(() => expect(posts).toEqual(["/api/models/qwen3.5-4b/download"]));
  });

  it("says what to expect of a model that's more than this computer is meant for", async () => {
    firstRun = { ...atDownload(engine("none")), chat_model: big };
    renderPage();

    expect(await screen.findByText(/it may be slow, run with a small context, or not load/)).toBeTruthy();
  });

  it("keeps the pick while its download runs", async () => {
    firstRun = atDownload(engine("done"), "downloading");
    renderPage();

    expect((await screen.findByLabelText<HTMLSelectElement>("Chat model")).disabled).toBe(true);
  });

  it("says why a pick was refused", async () => {
    firstRun = atDownload(engine("none"));
    putRefused = "No model called qwen3.5-4b.";
    renderPage();

    fireEvent.change(await screen.findByLabelText("Chat model"), { target: { value: "qwen3.5-4b" } });

    expect((await screen.findByRole("alert")).textContent).toBe("No model called qwen3.5-4b.");
    expect(screen.getByLabelText<HTMLSelectElement>("Chat model").value).toBe("qwen3.5-9b");
  });
});
