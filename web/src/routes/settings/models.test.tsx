import "../../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetSession } from "../../api/session";
import { routeTree } from "../../router";
import { installFakeSockets } from "../../test/fake-socket";

// The page asks for the list again while an update runs; no test should wait out the real gap.
vi.mock("./polling", () => ({ UPDATE_POLL_MS: 20 }));

const GIB = 1024 ** 3;

const download = (id: string, status: string) => ({
  model_id: id, status, bytes_done: 0, bytes_total: 1, rate: 0, error: null,
});

const model = (id: string, title: string, roles: string[], status: string) => ({
  id, title, quant: "Q4_K_M", roles, size: 2 * 10 ** 9, licence: "Apache-2.0",
  licence_notice: false, notes: null, repo: "x/y", suits_tier: true,
  download: download(id, status), installed: null, used_by: [],
  update_available: false, update: null,
});

const updating = (done: number, error: string | null = null) => ({
  model_id: "qwen3.5-9b", status: error ? "failed" : "updating",
  bytes_done: done, bytes_total: 5 * 10 ** 9, rate: 0, error,
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
    unlisted: [],
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

/** As the server: the list says what each role, and cleanup, can be given. A role takes the
 * models that have it; cleanup the chat's; neither an MLX build. */
function withChoices(list: unknown): unknown {
  const { models, chosen, ...rest } = list as {
    models: { id: string; roles: string[]; replaces?: string }[];
    chosen: Record<string, string>;
    choices?: unknown;
  };
  const having = (role: string) =>
    models.filter((m) => m.roles.includes(role) && !m.replaces).map((m) => m.id);
  const derived = Object.fromEntries(
    Object.keys(chosen).map((role) => [role, having(role === "clean" ? "main" : role)]),
  );
  return { ...rest, models, chosen, choices: rest.choices ?? derived };
}

let puts: unknown[] = [];
let choices: { role: string; modelId: string | null }[] = [];
let refusal: string | null = null; // what the server answers a choice with, when it refuses
let removed: string[] = [];
let afterUpdate: (() => void) | null = null; // what the server's list says once Update was pressed
let posts: { url: string; body: unknown }[] = [];
let overrides: Record<string, unknown> = {};

beforeEach(() => {
  puts = [];
  choices = [];
  refusal = null;
  removed = [];
  afterUpdate = null;
  posts = [];
  overrides = {};
  window.sessionStorage.setItem("lectern.token", "t");
  forgetSession();
  installFakeSockets(); // the event streams: open, silent
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST" && url.endsWith("/update")) {
        posts.push({ url, body: null });
        if (refusal) return new Response(JSON.stringify({ detail: refusal }), { status: 409 });
        afterUpdate?.();
        return new Response(JSON.stringify(updating(0)), { status: 202 });
      }
      if (init?.method === "POST") {
        posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null });
        return new Response(JSON.stringify(download(url.split("/")[3], "queued")), { status: 202 });
      }
      if (init?.method === "DELETE") {
        // As the server: the list again, without what was removed.
        const name = decodeURIComponent(url.split("/")[3]);
        removed.push(name);
        if (refusal) return new Response(JSON.stringify({ detail: refusal }), { status: 409 });
        const list = (overrides["/api/models"] ?? RESPONSES["/api/models"]) as {
          models: { id: string }[]; unlisted: { name: string }[];
        };
        return new Response(JSON.stringify(withChoices({
          ...list,
          models: list.models.map((m) => (m.id === name ? { ...m, installed: null } : m)),
          unlisted: list.unlisted.filter((u) => u.name !== name),
        })));
      }
      if (init?.method === "PUT" && url.startsWith("/api/models/roles/")) {
        // As the server: the list again, with the role on the chosen model (null: the
        // recommended one).
        const role = decodeURIComponent(url.split("/")[4]);
        const modelId = JSON.parse(String(init.body)).model_id as string | null;
        choices.push({ role, modelId });
        if (refusal) return new Response(JSON.stringify({ detail: refusal }), { status: 409 });
        const list = (overrides["/api/models"] ?? RESPONSES["/api/models"]) as {
          defaults: Record<string, string>; chosen: Record<string, string>;
        };
        const chosen = { ...list.chosen, [role]: modelId ?? list.defaults[role] };
        return new Response(JSON.stringify(withChoices({ ...list, chosen })));
      }
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        puts.push(body);
        return new Response(JSON.stringify({ ...(RESPONSES["/api/runtime/settings"] as object), values: body }));
      }
      const answer = overrides[url] ?? RESPONSES[url];
      return new Response(JSON.stringify(url === "/api/models" ? withChoices(answer) : answer));
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

  it("with MLX on and neither file on disk, the chat's one row is its MLX build's", async () => {
    withMlxBuild("none", true);
    const list = overrides["/api/models"] as { models: { id: string }[] };
    overrides["/api/models"] = {
      ...list,
      models: list.models.map((m) =>
        m.id === "qwen3.5-9b" ? { ...m, download: download("qwen3.5-9b", "none") } : m,
      ),
    };
    overrides["/api/runtime"] = { ...(RESPONSES["/api/runtime"] as object), residents: [] };
    renderPage();

    // MLX is what was asked for: the build is the one to download, not the usual file.
    const row = (await screen.findByTestId("status-main")).closest("tr")!;
    await waitFor(() => expect(row.textContent).toContain("runs on its MLX build · 4bit"));
    expect(screen.getByTestId("status-main").textContent).toBe("Not downloaded");
    expect(document.querySelectorAll("tr[data-role=main]")).toHaveLength(1);
    expect(screen.queryByTestId("chosen-main")).toBeNull();
    expect(screen.queryByRole("button", { name: "Download Qwen3.5 9B" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Download Qwen3.5 9B (MLX)" }));
    await waitFor(() =>
      expect(posts).toEqual([
        { url: "/api/models/qwen3.5-9b-mlx/download", body: { accept_licence: false } },
      ]),
    );
  });

  it("with MLX on and neither file on disk, cleanup's model isn't the chat's copy", async () => {
    withMlxBuild("none", true);
    const list = overrides["/api/models"] as {
      models: { id: string }[]; defaults: object; chosen: object;
    };
    // As the server answers then: cleanup named for the chat's own model by choice.
    overrides["/api/models"] = {
      ...list,
      defaults: { ...list.defaults, clean: "qwen3.5-9b" },
      chosen: { ...list.chosen, clean: "qwen3.5-9b" },
      models: list.models.map((m) =>
        m.id === "qwen3.5-9b" ? { ...m, download: download("qwen3.5-9b", "none") } : m,
      ),
    };
    overrides["/api/runtime"] = { ...(RESPONSES["/api/runtime"] as object), residents: [] };
    renderPage();

    const row = (await screen.findByTestId("status-clean")).closest("tr")!;
    await screen.findByText(/runs on its MLX build/);
    // The chat will run on its build, which can't clean: this is a download of its own.
    expect(row.textContent).not.toContain("The chat's own model");
    expect(screen.getByRole("button", { name: "Download Qwen3.5 9B" }).closest("tr")).toBe(row);
  });

  it("shows the MLX build as the chat's model once it's on disk: one row, the build's facts", async () => {
    withMlxBuild("done", true);
    renderPage();

    const row = (await screen.findByTestId("status-main")).closest("tr")!;
    await waitFor(() => expect(row.textContent).toContain("runs on its MLX build · 4bit"));
    expect(row.textContent).not.toContain("Q4_K_M"); // the usual file's own detail
    expect(screen.queryByTestId("status-main-mlx")).toBeNull();
    expect(screen.queryByTestId("chosen-main")).toBeNull();
    expect(document.querySelectorAll("tr[data-role=main]")).toHaveLength(1);
  });

  it("gives the build's size in the list for the model that runs on it", async () => {
    withMlxBuild("done", true);
    const list = overrides["/api/models"] as { models: { id: string }[] };
    overrides["/api/models"] = {
      ...list,
      models: [
        model("qwen3.5-4b", "Qwen3.5 4B", ["main"], "none"),
        ...list.models.map((m) => (m.id === "qwen3.5-9b-mlx" ? { ...m, size: 3 * 10 ** 9 } : m)),
      ],
    };
    renderPage();

    const select = (await screen.findByLabelText("Model for main")) as HTMLSelectElement;
    const row = select.closest("tr")!;
    await waitFor(() => expect(row.textContent).toContain("runs on its MLX build"));
    // The Size column is the build's; the entry of the model in use agrees with it.
    expect([...select.options].map((o) => o.text)).toEqual([
      "Qwen3.5 4B · 2 GB",
      "Qwen3.5 9B · 3 GB · recommended",
    ]);
    expect(row.children[2].textContent).toBe("3 GB");
  });

  it("offers no download of the usual file on the chat's row while its MLX build runs", async () => {
    withMlxBuild("done", true);
    const list = overrides["/api/models"] as { models: { id: string }[] };
    overrides["/api/models"] = {
      ...list,
      models: list.models.map((m) =>
        m.id === "qwen3.5-9b" ? { ...m, download: download("qwen3.5-9b", "none") } : m,
      ),
    };
    overrides["/api/runtime"] = { ...(RESPONSES["/api/runtime"] as object), residents: [] };
    renderPage();

    const status = await screen.findByTestId("status-main");
    await waitFor(() => expect(status.textContent).toBe("Downloaded, not loaded")); // the build
    expect(screen.queryByRole("button", { name: "Download Qwen3.5 9B" })).toBeNull();
  });

  it("shows the build's state on the chat's row, not a usual copy that is still loaded", async () => {
    withMlxBuild("done", true);
    renderPage();

    // The runtime still holds the usual model (loaded before the build arrived).
    const status = await screen.findByTestId("status-main");
    await waitFor(() => expect(status.textContent).toBe("Downloaded, not loaded"));
  });

  it("shows a loaded MLX build on the chat's row", async () => {
    withMlxBuild("done", true);
    const runtime = RESPONSES["/api/runtime"] as { residents: object[] };
    overrides["/api/runtime"] = {
      ...runtime, residents: [{ ...runtime.residents[0], model_id: "qwen3.5-9b-mlx" }],
    };
    renderPage();

    const status = await screen.findByTestId("status-main");
    await waitFor(() => expect(status.closest("tr")?.textContent).toContain("MLX build"));
    expect(status.textContent).toBe("Loaded, in use");
  });

  it("shows the model that cleans scanned pages as loaded on its own row, in the chat's colour", async () => {
    withMlxBuild("done", true);
    const list = overrides["/api/models"] as { defaults: object; chosen: object };
    overrides["/api/models"] = {
      ...list,
      defaults: { ...list.defaults, clean: "qwen3.5-9b" },
      chosen: { ...list.chosen, clean: "qwen3.5-9b" },
    };
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

    const cleans = await screen.findByTestId("status-clean");
    expect(cleans.textContent).toBe("Loaded, in use");
    // The chat's own model, but not the copy the chat runs on: that one is the build.
    expect(cleans.closest("tr")?.textContent).not.toContain("The chat's own model");
    await waitFor(() => expect(screen.getByTestId("status-main").textContent).toBe("Loaded"));
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

  it("says why a choice was refused, and leaves the role on its model", async () => {
    withChatModels();
    refusal = "Qwen3.5 4B can't be used for the 'main' role.";
    renderPage();

    fireEvent.change(await screen.findByLabelText("Model for main"), {
      target: { value: "qwen3.5-4b" },
    });

    expect(await screen.findByText(refusal)).toBeTruthy();
    expect((screen.getByLabelText("Model for main") as HTMLSelectElement).value).toBe("qwen3.5-9b");
    expect(screen.getByTestId("status-main").textContent).toBe("Loaded, in use");
  });

  it("warns under a chosen model that this computer isn't meant for", async () => {
    withChatModels("qwen3.5-35b-a3b");
    renderPage();

    await screen.findByLabelText("Model for main");
    expect(screen.getByText(/may be slow, run with a small context, or not load/)).toBeTruthy();
  });

  // Cleanup of scanned pages has a model of its own to choose: a chat model does it.
  const withCleanup = (cleans: string, chat = "qwen3.5-9b") => {
    withChatModels(chat);
    const list = overrides["/api/models"] as { defaults: object; chosen: object };
    overrides["/api/models"] = {
      ...list,
      defaults: { ...list.defaults, clean: chat },
      chosen: { ...list.chosen, clean: cleans },
    };
  };
  const cleanRow = async () => (await screen.findByTestId("status-clean")).closest("tr")!;

  it("lists the model that cleans scanned pages after the chat's, the chat's own by default", async () => {
    withCleanup("qwen3.5-9b");
    renderPage();

    const row = await cleanRow();
    expect(row.previousElementSibling?.getAttribute("data-role")).toBe("main");
    const select = screen.getByLabelText("Model for clean") as HTMLSelectElement;
    expect(select.value).toBe("qwen3.5-9b");
    expect([...select.options].map((o) => o.text)).toEqual([
      "Qwen3.5 4B · 2 GB",
      "Qwen3.5 9B · 2 GB · recommended",
      "Qwen3.5 35B-A3B · 2 GB · more than this computer is meant for",
    ]);
    expect(row.textContent).toContain("The chat's own model: nothing more is loaded for it.");
    // "clean" on its own says little: the row says what the model is for.
    expect(row.textContent).toContain("Cleans up scanned pages.");
  });

  it("offers one download of a model the chat and cleanup share: on the chat's row", async () => {
    withCleanup("qwen3.5-4b", "qwen3.5-4b");
    overrides["/api/runtime"] = { ...(RESPONSES["/api/runtime"] as object), residents: [] };
    renderPage();

    const row = await cleanRow();
    expect(row.textContent).toContain("The chat's own model");
    expect(screen.getByTestId("status-clean").textContent).toBe("Not downloaded");
    const buttons = screen.getAllByRole("button", { name: "Download Qwen3.5 4B" });
    expect(buttons).toHaveLength(1);
    expect(buttons[0].closest("tr")?.getAttribute("data-role")).toBe("main");
    expect(row.querySelector("button")).toBeNull();
  });

  it("has no cleanup row when the server names no model for it", async () => {
    renderPage();

    await screen.findByTestId("status-main");
    expect(screen.queryByTestId("status-clean")).toBeNull();
  });

  it("saves a model chosen for cleanup, and shows its own state and download", async () => {
    withCleanup("qwen3.5-9b");
    renderPage();

    fireEvent.change(await screen.findByLabelText("Model for clean"), {
      target: { value: "qwen3.5-4b" },
    });

    await waitFor(() => expect(choices).toEqual([{ role: "clean", modelId: "qwen3.5-4b" }]));
    await waitFor(() => expect(screen.getByTestId("status-clean").textContent).toBe("Not downloaded"));
    const row = await cleanRow();
    expect(row.textContent).not.toContain("The chat's own model");
    expect(screen.getByRole("button", { name: "Use Qwen3.5 9B for clean" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Download Qwen3.5 4B" }));
    await waitFor(() =>
      expect(posts).toEqual([
        { url: "/api/models/qwen3.5-4b/download", body: { accept_licence: false } },
      ]),
    );
    expect(screen.getByTestId("status-main").textContent).toBe("Loaded, in use"); // the chat's row
  });

  it("shows cleanup's own loaded copy on its row", async () => {
    withCleanup("qwen3.5-4b");
    const runtime = RESPONSES["/api/runtime"] as { residents: object[] };
    overrides["/api/runtime"] = {
      ...runtime,
      residents: [
        runtime.residents[0],
        { ...runtime.residents[0], role: "main@qwen3.5-4b", model_id: "qwen3.5-4b", leases: 0, pid: 8 },
      ],
    };
    renderPage();

    expect((await screen.findByTestId("status-clean")).textContent).toBe("Loaded");
    expect(screen.getByTestId("status-main").textContent).toBe("Loaded, in use");
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

    // Neither file of the 4B is on disk: its build is the chat's row, and the one to get.
    const get = await screen.findByRole("button", { name: "Download Qwen3.5 4B (MLX)" });
    expect(get.closest("tr")?.getAttribute("data-role")).toBe("main");
    expect(screen.queryByRole("button", { name: /Qwen3.5 9B \(MLX\)/ })).toBeNull();
    expect(screen.queryByText(/Qwen3.5 9B \(MLX\)/)).toBeNull();
    // A build isn't something to choose: it comes with its model.
    const select = screen.getByLabelText("Model for main") as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual([
      "qwen3.5-4b", "qwen3.5-9b", "qwen3.5-35b-a3b",
    ]);
  });

  // On disk: the chat model (a recorded version), the OCR model copied in by hand, a coder
  // whose download stopped halfway, and a folder left by an older Lectern.
  const withInstalled = () => {
    const base = RESPONSES["/api/models"] as { models: { id: string }[] };
    const on = (id: string, installed: object | null, extra: object = {}) =>
      ({ ...base.models.find((m) => m.id === id), installed, ...extra });
    overrides["/api/models"] = {
      ...base,
      models: [
        on("qwen3.5-9b", {
          revision: "3885219b6810b007914f3a7950a8d1b469d598a5",
          at: new Date(Date.now() - 3 * 24 * 3600 * 1000).toISOString(),
          bytes: 5.6 * 10 ** 9,
        }, { used_by: ["main"] }),
        on("paddleocr-vl-1.6", { revision: null, at: null, bytes: 1.8 * 10 ** 9 }, { used_by: ["ocr"] }),
        on("qwen3-vl-8b", null),
        on("coder-7b", { revision: null, at: null, bytes: 4 * 10 ** 8 },
           { download: download("coder-7b", "paused") }),
      ],
      unlisted: [{ name: "model-of-an-older-lectern", bytes: 3 * 10 ** 9 }],
    };
  };
  const installedRow = (text: string) =>
    screen.getByTestId("installed-models").querySelector(`[data-installed="${text}"]`) as HTMLElement;

  it("lists what's on disk with its version, size and the roles using it", async () => {
    withInstalled();
    renderPage();

    await screen.findByTestId("installed-models");
    const chat = installedRow("qwen3.5-9b").textContent;
    expect(chat).toContain("Qwen3.5 9B");
    expect(chat).toContain("version 3885219");
    expect(chat).toContain("installed 3 days ago");
    expect(chat).toContain("used by main");
    expect(chat).toContain("5.6 GB");
    expect(installedRow("paddleocr-vl-1.6").textContent).toContain("version not recorded");
    expect(installedRow("coder-7b").textContent).toContain("download not finished");
    const left = installedRow("model-of-an-older-lectern").textContent;
    expect(left).toContain("not in this Lectern's catalog");
    expect(left).toContain("3 GB");
    // Not on disk, not listed.
    expect(screen.getByTestId("installed-models").textContent).not.toContain("Qwen3-VL 8B");
  });

  it("shows no installed list when nothing is on disk", async () => {
    renderPage();

    await screen.findByTestId("status-main");
    expect(screen.queryByTestId("installed-models")).toBeNull();
  });

  it("removes a model only after a second click, and says what the role is left with", async () => {
    withInstalled();
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Remove Qwen3.5 9B" }));

    // Asked first, in place: nothing was sent.
    expect(removed).toEqual([]);
    expect(installedRow("qwen3.5-9b").textContent).toContain(
      "Remove 5.6 GB? main will have no model until it's downloaded again.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Keep" }));
    expect(screen.queryByRole("button", { name: "Keep" })).toBeNull();
    expect(removed).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Remove Qwen3.5 9B" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(removed).toEqual(["qwen3.5-9b"]));
    await waitFor(() => expect(installedRow("qwen3.5-9b")).toBeNull());
    expect(installedRow("paddleocr-vl-1.6")).toBeTruthy(); // the others stay
  });

  it("removes a folder no catalog model owns", async () => {
    withInstalled();
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Remove model-of-an-older-lectern" }));
    expect(installedRow("model-of-an-older-lectern").textContent).toContain("Remove 3 GB?");
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(removed).toEqual(["model-of-an-older-lectern"]));
    await waitFor(() => expect(installedRow("model-of-an-older-lectern")).toBeNull());
  });

  it("says why a model wasn't removed, and keeps it listed", async () => {
    withInstalled();
    refusal = "Qwen3.5 9B is in use. Remove it when the reply or the job using it is done.";
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Remove Qwen3.5 9B" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));

    expect(await screen.findByText(refusal)).toBeTruthy();
    expect(installedRow("qwen3.5-9b")).toBeTruthy();
  });

  // The chat model on disk at an older pin than this Lectern's catalog.
  const withUpdate = (chat: object = {}) => {
    withInstalled();
    const list = overrides["/api/models"] as { models: { id: string }[] };
    overrides["/api/models"] = {
      ...list,
      models: list.models.map((m) =>
        m.id === "qwen3.5-9b" ? { ...m, update_available: true, ...chat } : m),
    };
  };

  it("offers Update only for a model the catalog has a newer version of", async () => {
    withUpdate();
    renderPage();

    await screen.findByTestId("installed-models");
    expect(installedRow("qwen3.5-9b").textContent).toContain("a newer version is available");
    expect(screen.getByRole("button", { name: "Update Qwen3.5 9B" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Update PaddleOCR-VL 1.6" })).toBeNull();
    expect(installedRow("paddleocr-vl-1.6").textContent).not.toContain("newer version");
  });

  it("starts an update, shows how far it is, and the new version when it's done", async () => {
    withUpdate();
    afterUpdate = () => withUpdate({ update: updating(2 * 10 ** 9) });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Update Qwen3.5 9B" }));

    await waitFor(() => expect(posts).toEqual([{ url: "/api/models/qwen3.5-9b/update", body: null }]));
    await waitFor(() =>
      expect(installedRow("qwen3.5-9b").textContent).toContain("2 GB of 5 GB"));
    // It's on its way: nothing to press again. The model stays in use meanwhile.
    expect(screen.queryByRole("button", { name: "Update Qwen3.5 9B" })).toBeNull();
    expect(installedRow("qwen3.5-9b").textContent).toContain("stays in use");

    // The server finishes it. Nobody presses anything: the page asks again while one runs.
    withUpdate({
      update_available: false,
      installed: { revision: "b".repeat(40), at: new Date().toISOString(), bytes: 5 * 10 ** 9 },
    });
    await waitFor(() =>
      expect(installedRow("qwen3.5-9b").textContent).toContain("version bbbbbbb"));
    expect(installedRow("qwen3.5-9b").textContent).not.toContain("newer version");
  });

  it.each([
    ["checking", "comparing the files on disk with the new version"],
    ["waiting", "It takes the model's place as soon as the model is free"],
  ])("says what an update is doing while it is %s", async (status, words) => {
    withUpdate({ update: { ...updating(0), status } });
    renderPage();

    await screen.findByTestId("installed-models");
    expect(installedRow("qwen3.5-9b").textContent).toContain(words);
    // Under way: not offered again, and no bar that would sit at 0% or 100% saying nothing.
    expect(screen.queryByRole("button", { name: "Update Qwen3.5 9B" })).toBeNull();
    expect(installedRow("qwen3.5-9b").querySelector("progress")).toBeNull();
  });

  it("says why an update failed and offers it again", async () => {
    withUpdate({ update: updating(10 ** 9, "Connection problem (ReadError). Try again to resume.") });
    renderPage();

    await screen.findByTestId("installed-models");
    expect(installedRow("qwen3.5-9b").textContent).toContain("Connection problem (ReadError)");
    expect(screen.getByRole("button", { name: "Update Qwen3.5 9B" })).toBeTruthy();
  });

  it("says why an update couldn't start", async () => {
    withUpdate();
    refusal = "Qwen3.5 9B has no update. It isn't on disk, or it's at the catalog's version already.";
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Update Qwen3.5 9B" }));

    expect(await screen.findByText(refusal)).toBeTruthy();
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

describe("Models & memory, for someone who isn't an admin", () => {
  beforeEach(() => {
    overrides["/api/login"] = {
      accounts: true,
      user: { login: "olena", role: "member" },
      token: "t",
    };
    // The chat model on disk, with an update on offer: an admin would see Update and Remove.
    const base = RESPONSES["/api/models"] as { models: ReturnType<typeof model>[] };
    const onDisk = { revision: "a".repeat(40), at: new Date().toISOString(), bytes: 5 * 10 ** 9 };
    overrides["/api/models"] = {
      ...base,
      models: base.models.map((m) =>
        m.id === "qwen3.5-9b" ? { ...m, update_available: true, installed: onDisk } : m),
    };
  });

  it("shows the models, what is loaded and what is on disk", async () => {
    renderPage();

    expect(await screen.findByTestId("memory-used")).toBeTruthy();
    expect(screen.getByTestId("status-main").textContent).toBe("Loaded, in use");
    expect(screen.getByTestId("status-ocr.hand").textContent).toBe("Not downloaded");
    expect(screen.getByTestId("installed-models")).toBeTruthy();
  });

  it("has nothing to press: models and the GPU's settings are the admin's", async () => {
    renderPage();
    await screen.findByTestId("installed-models");

    expect(screen.queryAllByRole("button")).toEqual([]); // no Download, Update, Remove
    expect(screen.queryAllByRole("combobox")).toEqual([]); // no list of models to choose from
    expect(screen.queryAllByRole("radio")).toEqual([]); // no "Sharing the GPU"
    expect(screen.queryAllByRole("checkbox")).toEqual([]);
    expect(screen.getByText(/Only an admin can download, remove or choose models/)).toBeTruthy();
    expect(screen.queryByText(/or now with Download/)).toBeNull();
  });
});

describe("Models & memory, for an admin who signed in", () => {
  it("is the whole page", async () => {
    overrides["/api/login"] = {
      accounts: true,
      user: { login: "yevhen", role: "admin" },
      token: "t",
    };
    renderPage();

    expect(await screen.findByRole("button", { name: "Download Qwen3-VL 8B" })).toBeTruthy();
    expect(screen.getAllByRole("radio").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Only an admin can/)).toBeNull();
  });
});
