import "../../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routeTree } from "../../router";

const GIB = 1024 ** 3;

const download = (id: string, status: string) => ({
  model_id: id, status, bytes_done: 0, bytes_total: 1, rate: 0, error: null,
});

const model = (id: string, title: string, roles: string[], status: string) => ({
  id, title, quant: "Q4_K_M", roles, size: 2 * 10 ** 9, licence: "Apache-2.0",
  licence_notice: false, notes: null, repo: "x/y", suits_tier: true,
  download: download(id, status),
});

const RESPONSES: Record<string, unknown> = {
  "/api/projects": [],
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
    chosen: { main: "qwen3.5-9b", ocr: "paddleocr-vl-1.6", "ocr.hand": "qwen3-vl-8b", code: "coder-7b" },
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
    mlx_supported: true, mlx_available: true,
  },
};

let puts: unknown[] = [];
let choices: { role: string; modelId: string | null }[] = [];
let posts: { url: string; body: unknown }[] = [];
let overrides: Record<string, unknown> = {};

beforeEach(() => {
  puts = [];
  choices = [];
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
      if (init?.method === "PUT" && url.startsWith("/api/models/roles/")) {
        // As the server: the list again, with the role on the chosen model (null: the
        // recommended one).
        const role = decodeURIComponent(url.split("/")[4]);
        const modelId = JSON.parse(String(init.body)).model_id as string | null;
        choices.push({ role, modelId });
        const list = (overrides["/api/models"] ?? RESPONSES["/api/models"]) as {
          defaults: Record<string, string>; chosen: Record<string, string>;
        };
        const chosen = { ...list.chosen, [role]: modelId ?? list.defaults[role] };
        return new Response(JSON.stringify({ ...list, chosen }));
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

// Through the app's router, as the page is reached: its links are router links.
function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/settings/models"] }) });
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("Models & memory", () => {
  it("goes back to Projects inside the app, without reloading it", async () => {
    renderPage();
    const back = await screen.findByRole("link", { name: "Projects" });
    expect(back.getAttribute("href")).toBe("/projects");
    fireEvent.click(back);

    // A plain <a href> would leave the page as it is here (jsdom doesn't navigate) and, in a
    // browser, reload the whole app; the router's link shows the projects page in place.
    expect(await screen.findByRole("heading", { name: "Projects" })).toBeTruthy();
  });

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

  it("says why a download stopped, next to the button that resumes it", async () => {
    const response = RESPONSES["/api/models"] as { models: ReturnType<typeof model>[] };
    overrides["/api/models"] = {
      ...response,
      models: response.models.map((m) =>
        m.id === "qwen3-vl-8b"
          ? { ...m, download: { ...m.download, status: "failed", bytes_done: 5, error: "disk full" } }
          : m,
      ),
    };
    renderPage();

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "disk full");
    expect(screen.getByRole("button", { name: "Download Qwen3-VL 8B" }).textContent).toBe("Resume");
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

  it("says an OCR job waits for the chat, not for memory", async () => {
    overrides["/api/runtime"] = {
      ...(RESPONSES["/api/runtime"] as object), waiting: 1, activity: null,
      hold: { role: "ocr", resume_in: 100 },
    };
    renderPage();

    expect(await screen.findByText(/OCR waits for the chat to be quiet · starts in 1:(40|39)/)).toBeTruthy();
    expect(screen.queryByText(/waiting for memory/)).toBeNull();
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

  // A Mac: the chat model has an MLX build in the catalog.
  const withMlxBuild = (status: string, mlxEnabled: boolean) => {
    const base = RESPONSES["/api/models"] as { models: unknown[] };
    const build = {
      ...model("qwen3.5-9b-mlx", "Qwen3.5 9B (MLX)", ["main"], status),
      quant: "4bit", format: "mlx", replaces: "qwen3.5-9b",
    };
    overrides["/api/models"] = { ...base, models: [...base.models, build] };
    const settings = RESPONSES["/api/runtime/settings"] as { values: object };
    overrides["/api/runtime/settings"] = {
      ...settings, values: { ...settings.values, mlx_enabled: mlxEnabled },
    };
  };

  it("lists no MLX build while MLX is off", async () => {
    withMlxBuild("none", false);
    renderPage();

    await screen.findByTestId("status-main");
    await screen.findByLabelText(/Use MLX for MLX models/);
    expect(screen.queryByText("Qwen3.5 9B (MLX)")).toBeNull();
    expect(screen.queryByTestId("chosen-main")).toBeNull();
  });

  it("with MLX on, offers the model's MLX build for download, and the model stays the one used", async () => {
    withMlxBuild("none", true);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Download Qwen3.5 9B (MLX)" }));
    await waitFor(() =>
      expect(posts).toEqual([
        { url: "/api/models/qwen3.5-9b-mlx/download", body: { accept_licence: false } },
      ]),
    );
    expect(screen.getByTestId("status-main-mlx").textContent).toBe("Not downloaded");
    // Until the build is on disk the usual model runs: the mark is on its row.
    const chosen = screen.getByTestId("chosen-main");
    expect(chosen.textContent).toBe(" · used for this role");
    expect(chosen.closest("tr")?.textContent).toContain("Q4_K_M");
  });

  it("marks the MLX build as the one used once it's on disk", async () => {
    withMlxBuild("done", true);
    renderPage();

    const chosen = await screen.findByTestId("chosen-main");
    expect(chosen.closest("tr")?.textContent).toContain("Qwen3.5 9B (MLX)");
    expect(screen.getAllByTestId("chosen-main")).toHaveLength(1);
  });

  it("shows a loaded model on its own row, not on its MLX build's", async () => {
    withMlxBuild("done", true);
    renderPage();

    // The runtime still holds the usual model (loaded before the build arrived).
    expect((await screen.findByTestId("status-main")).textContent).toBe("Loaded, in use");
    expect(screen.getByTestId("status-main-mlx").textContent).toBe("Downloaded, not loaded");
  });

  it("shows a loaded MLX build on the build's row", async () => {
    withMlxBuild("done", true);
    const runtime = RESPONSES["/api/runtime"] as { residents: object[] };
    overrides["/api/runtime"] = {
      ...runtime, residents: [{ ...runtime.residents[0], model_id: "qwen3.5-9b-mlx" }],
    };
    renderPage();

    expect((await screen.findByTestId("status-main-mlx")).textContent).toBe("Loaded, in use");
    expect(screen.getByTestId("status-main").textContent).toBe("Downloaded, not loaded");
  });

  it("shows the usual model as loaded, in the chat's colour, while it cleans scanned pages", async () => {
    withMlxBuild("done", true);
    const runtime = RESPONSES["/api/runtime"] as { residents: object[] };
    // The chat runs on the build; OCR cleanup holds the usual model under its own name.
    overrides["/api/runtime"] = {
      ...runtime,
      residents: [
        { ...runtime.residents[0], model_id: "qwen3.5-9b-mlx", leases: 0 },
        { ...runtime.residents[0], role: "main@qwen3.5-9b", pid: 8 },
      ],
    };
    renderPage();

    expect((await screen.findByTestId("status-main")).textContent).toBe("Loaded, in use");
    expect(screen.getByTestId("status-main-mlx").textContent).toBe("Loaded");
    const segment = screen.getByTitle(/^main@qwen3.5-9b · /);
    expect(segment.className).toContain("bg-accent");
  });

  // Three chat models in the catalog: the 9B recommended here, the 35B more than this card
  // is meant for.
  const withChatModels = (chosen = "qwen3.5-9b") => {
    const base = RESPONSES["/api/models"] as { models: unknown[]; chosen: object };
    overrides["/api/models"] = {
      ...base,
      chosen: { ...base.chosen, main: chosen },
      models: [
        model("qwen3.5-4b", "Qwen3.5 4B", ["main"], "none"),
        ...base.models,
        { ...model("qwen3.5-35b-a3b", "Qwen3.5 35B-A3B", ["main"], "none"), suits_tier: false },
      ],
    };
  };

  it("offers a role's models, marking the recommended one and one too big for this computer", async () => {
    withChatModels();
    renderPage();

    const select = (await screen.findByLabelText("Model for main")) as HTMLSelectElement;
    expect(select.value).toBe("qwen3.5-9b");
    expect([...select.options].map((o) => o.textContent)).toEqual([
      "Qwen3.5 4B · 2 GB",
      "Qwen3.5 9B · 2 GB · recommended",
      "Qwen3.5 35B-A3B · 2 GB · more than this computer is meant for",
    ]);
    // A list is as wide as its longest option unless told otherwise, and would push the
    // Status column (the Download buttons) off the card.
    expect(select.className).toContain("w-48");
    // What the closed list has no room for is under it.
    expect(select.closest("td")?.textContent).toContain("Q4_K_M · recommended");
    // On the recommended model there's nothing to go back to.
    expect(screen.queryByText("Use it")).toBeNull();
  });

  it("has nothing to choose for a role with one model, and still marks it recommended", async () => {
    renderPage();

    await screen.findByTestId("status-ocr");
    expect(screen.queryByLabelText("Model for ocr")).toBeNull();
    const row = screen.getByTestId("status-ocr").closest("tr");
    expect(row?.textContent).toContain("PaddleOCR-VL 1.6");
    expect(row?.textContent).toContain("recommended");
  });

  it("saves a chosen model, shows its row, and offers the way back to the recommended one", async () => {
    withChatModels();
    renderPage();

    fireEvent.change(await screen.findByLabelText("Model for main"), {
      target: { value: "qwen3.5-4b" },
    });

    await waitFor(() => expect(choices).toEqual([{ role: "main", modelId: "qwen3.5-4b" }]));
    // The row is the chosen model's now: its download state, its Download button.
    await waitFor(() => expect(screen.getByTestId("status-main").textContent).toBe("Not downloaded"));
    expect(screen.getByRole("button", { name: "Download Qwen3.5 4B" })).toBeTruthy();
    expect(screen.getByText(/Recommended for this computer: Qwen3.5 9B/)).toBeTruthy();

    // Read aloud, "Use it" on its own says nothing: every row can have one.
    const back = screen.getByRole("button", { name: "Use Qwen3.5 9B for main" });
    expect(back.textContent).toBe("Use it");
    fireEvent.click(back);

    await waitFor(() => expect(choices[1]).toEqual({ role: "main", modelId: null }));
    await waitFor(() => expect(screen.queryByText("Use it")).toBeNull());
  });

  it("warns under a chosen model that this computer isn't meant for", async () => {
    withChatModels("qwen3.5-35b-a3b");
    renderPage();

    await screen.findByLabelText("Model for main");
    expect(screen.getByText(/may be slow, run with a small context, or not load/)).toBeTruthy();
  });

  it("lists the MLX build of the chosen model, not of the recommended one", async () => {
    withChatModels("qwen3.5-4b");
    const list = overrides["/api/models"] as { models: unknown[] };
    const build = (id: string, title: string, replaces: string) => ({
      ...model(id, title, ["main"], "none"), quant: "4bit", format: "mlx", replaces,
    });
    overrides["/api/models"] = {
      ...list,
      models: [
        ...list.models,
        build("qwen3.5-4b-mlx", "Qwen3.5 4B (MLX)", "qwen3.5-4b"),
        build("qwen3.5-9b-mlx", "Qwen3.5 9B (MLX)", "qwen3.5-9b"),
      ],
    };
    const settings = RESPONSES["/api/runtime/settings"] as { values: object };
    overrides["/api/runtime/settings"] = {
      ...settings, values: { ...settings.values, mlx_enabled: true },
    };
    renderPage();

    const buildRow = (await screen.findByTestId("status-main-mlx")).closest("tr");
    expect(buildRow?.textContent).toContain("Qwen3.5 4B (MLX)");
    expect(screen.queryByText(/Qwen3.5 9B \(MLX\)/)).toBeNull();
    // A build isn't something to choose: it comes with its model.
    const select = screen.getByLabelText("Model for main") as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual([
      "qwen3.5-4b", "qwen3.5-9b", "qwen3.5-35b-a3b",
    ]);
  });

  it("says MLX isn't installed, and keeps the switch off, on a Mac without it", async () => {
    withMlxBuild("none", false);
    overrides["/api/runtime/settings"] = {
      ...(RESPONSES["/api/runtime/settings"] as object),
      mlx_available: false,
    };
    renderPage();

    const mlxSwitch = (await screen.findByLabelText(/Use MLX for MLX models/)) as HTMLInputElement;
    expect(mlxSwitch.disabled).toBe(true);
    expect(mlxSwitch.checked).toBe(false);
    expect(screen.getByText(/MLX isn't installed/)).toBeTruthy();
    expect(screen.queryByTestId("status-main-mlx")).toBeNull();
  });

  it("hides the MLX switch where MLX can't run", async () => {
    overrides["/api/runtime/settings"] = {
      ...(RESPONSES["/api/runtime/settings"] as object),
      mlx_supported: false, mlx_available: false,
    };
    renderPage();

    await screen.findByLabelText(/Pause the job after the current page/);
    expect(screen.queryByLabelText(/Use MLX for MLX models/)).toBeNull();
  });
});
