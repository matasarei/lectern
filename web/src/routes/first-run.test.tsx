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
  sandboxChecks = 0;
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.endsWith("/events")) return new Response(new ReadableStream({ start: (c) => c.close() }));
      if (url === "/api/system/sandbox") {
        sandboxChecks += 1;
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
});
