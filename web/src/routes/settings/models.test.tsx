import "../../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ModelsPage } from "./models";

const GIB = 1024 ** 3;

const download = (id: string, status: string) => ({
  model_id: id, status, bytes_done: 0, bytes_total: 1, rate: 0, error: null,
});

const model = (id: string, title: string, roles: string[], status: string) => ({
  id, title, quant: "Q4_K_M", roles, size: 2 * 10 ** 9, licence: "Apache-2.0",
  licence_notice: false, notes: null, repo: "x/y", download: download(id, status),
});

const RESPONSES: Record<string, unknown> = {
  "/api/system": {
    hardware: {
      os: "linux", arch: "x86_64", cpu: "Ryzen", cpu_cores: 8, ram_total: 16 * GIB,
      ram_available: 8 * GIB, gpu_budget: 7.4 * GIB,
      gpus: [{ vendor: "nvidia", name: "RTX 4060", memory_total: 8 * GIB, memory_free: 6 * GIB,
               unified: false, cuda_driver: "13.0" }],
    },
    tier: "recommended",
    disk_free: 100 * 10 ** 9,
    sandbox: { runtime: null, version: null, engine_running: false },
  },
  "/api/models": {
    tier: "recommended",
    defaults: { main: "qwen3.5-9b", ocr: "paddleocr-vl-1.6", "ocr.hand": "qwen3-vl-8b", code: "coder-7b" },
    models: [
      model("qwen3.5-9b", "Qwen3.5 9B", ["main"], "done"),
      model("paddleocr-vl-1.6", "PaddleOCR-VL 1.6", ["ocr"], "done"),
      model("qwen3-vl-8b", "Qwen3-VL 8B", ["ocr.hand"], "none"),
      model("coder-7b", "Coder 7B", ["code"], "none"),
    ],
  },
  "/api/runtime": {
    tier: "recommended", budget: 7.4 * GIB, unified: false, used: 5.5 * GIB, swaps: 3, waiting: 0,
    residents: [{ role: "main", model_id: "qwen3.5-9b", bytes: 5.5 * GIB, ctx: 16384,
                  kv_type: "f16", on_gpu: true, leases: 1, status: "loaded", pid: 7 }],
  },
  "/api/runtime/settings": {
    values: { mlx_enabled: false, when_models_dont_fit: "swap_between_phases",
              chat_during_job: "pause_job_after_current_item" },
    implemented: { when_models_dont_fit: ["swap_between_phases"],
                   chat_during_job: ["pause_job_after_current_item"] },
    mlx_supported: true,
  },
};

let puts: unknown[] = [];
let posts: { url: string; body: unknown }[] = [];
let overrides: Record<string, unknown> = {};

beforeEach(() => {
  puts = [];
  posts = [];
  overrides = {};
  window.sessionStorage.setItem("lectern.token", "t");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/events")) return new Response(new ReadableStream({ start: (c) => c.close() }));
      if (init?.method === "POST") {
        posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null });
        return new Response(JSON.stringify(download(url.split("/")[3], "queued")), { status: 202 });
      }
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        puts.push(body);
        return new Response(JSON.stringify({ ...(RESPONSES["/api/runtime/settings"] as object), values: body }));
      }
      return new Response(JSON.stringify(overrides[url] ?? RESPONSES[url]));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

describe("Models & memory", () => {
  it("shows memory in use and the state of each role", async () => {
    renderPage();

    expect(await screen.findByTestId("memory-used")).toHaveProperty("textContent", "5.5 GB of 7.4 GB in use");
    expect(screen.getByText("3 swaps since start")).toBeTruthy();
    expect(screen.getByTestId("status-main").textContent).toBe("Loaded, in use");
    expect(screen.getByTestId("status-ocr").textContent).toBe("Downloaded, not loaded");
    expect(screen.getByTestId("status-ocr.hand").textContent).toBe("Not downloaded");
  });

  it("downloads a role's model that isn't on disk, and only those", async () => {
    renderPage();

    const button = await screen.findByRole("button", { name: "Download Qwen3-VL 8B" });
    expect(screen.queryByRole("button", { name: "Download PaddleOCR-VL 1.6" })).toBeNull();
    fireEvent.click(button);

    await waitFor(() =>
      expect(posts).toEqual([
        { url: "/api/models/qwen3-vl-8b/download", body: { accept_licence: false } },
      ]),
    );
  });

  it("names the licence on the button when the model's licence needs accepting", async () => {
    const response = RESPONSES["/api/models"] as { models: ReturnType<typeof model>[] };
    overrides["/api/models"] = {
      ...response,
      models: response.models.map((m) =>
        m.id === "coder-7b" ? { ...m, licence: "Qwen Research", licence_notice: true } : m,
      ),
    };
    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: "Accept the Qwen Research licence and download Coder 7B" }),
    );
    await waitFor(() =>
      expect(posts).toEqual([{ url: "/api/models/coder-7b/download", body: { accept_licence: true } }]),
    );
  });

  it("offers only the implemented policies", async () => {
    renderPage();

    const later = await screen.findByLabelText(/Chat waits until the job has finished/);
    expect((later as HTMLInputElement).disabled).toBe(true);
    const current = screen.getByLabelText(/Pause the job after the current page/) as HTMLInputElement;
    expect(current.checked).toBe(true);
  });

  it("saves the MLX switch", async () => {
    renderPage();

    fireEvent.click(await screen.findByLabelText(/Use MLX for MLX models/));

    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toMatchObject({ mlx_enabled: true, chat_during_job: "pause_job_after_current_item" });
  });

  it("hides the MLX switch where MLX can't run", async () => {
    overrides["/api/runtime/settings"] = {
      ...(RESPONSES["/api/runtime/settings"] as object),
      mlx_supported: false,
    };
    renderPage();

    await screen.findByLabelText(/Pause the job after the current page/);
    expect(screen.queryByLabelText(/Use MLX for MLX models/)).toBeNull();
  });
});
