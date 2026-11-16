import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { VoiceLanguage, VoiceOption } from "../api/types";
import { VoicesCard } from "./voices-card";

const MB = 1_000_000;

function voice(id: string, extra: Partial<VoiceOption> = {}): VoiceOption {
  return {
    id,
    title: id,
    quant: "medium",
    size: 63 * MB,
    licence: "MIT; recordings CC0-1.0",
    licence_notice: false,
    notes: null,
    default: false,
    installed: false,
    download: { model_id: id, status: "none", bytes_done: 0, bytes_total: 0, rate: 0, error: null },
    ...extra,
  };
}

type Sent = { url: string; body: Record<string, unknown> };
let languages: VoiceLanguage[];
let sent: Sent[];

beforeEach(() => {
  languages = [
    {
      code: "ru",
      voice: "Denis",
      chosen: null,
      voices: [
        voice("Denis", { default: true, installed: true }),
        voice("Dmitri"),
        voice("Ruslan", {
          licence: "MIT; recordings CC-BY-NC-SA-4.0",
          licence_notice: true,
          notes: "Non-commercial.",
        }),
      ],
    },
    { code: "de", voice: "Thorsten", chosen: null, voices: [voice("Thorsten", { default: true })] },
  ];
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      if (init.method === "PUT") {
        const body = JSON.parse(init.body as string) as Record<string, unknown>;
        sent.push({ url, body });
        const code = url.split("/").pop();
        languages = languages.map((l) => (l.code === code ? { ...l, voice: String(body.model_id) } : l));
      }
      return new Response(JSON.stringify({ languages }));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderCard(admin = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <VoicesCard admin={admin} live={{}} />
    </QueryClientProvider>,
  );
}

describe("VoicesCard", () => {
  it("lists each language by name with its voice, size, licence and whether it is here", async () => {
    renderCard();
    const russian = await screen.findByTestId("voice-ru");
    expect(within(russian).getByText("Russian")).toBeTruthy();
    expect(within(russian).getByText("On this computer")).toBeTruthy();
    expect(within(russian).getByText("63 MB")).toBeTruthy();
    const german = screen.getByTestId("voice-de");
    expect(within(german).getByText("Thorsten")).toBeTruthy(); // one voice: nothing to choose
    expect(within(german).getByRole("button", { name: "Download" })).toBeTruthy();
    const rows = screen.getAllByTestId(/^voice-/).map((r) => r.dataset.testid);
    expect(rows).toEqual(["voice-de", "voice-ru"]); // German before Russian, by name
  });

  it("chooses another voice, which downloads it", async () => {
    renderCard();
    fireEvent.change(await screen.findByRole("combobox", { name: "Voice for Russian" }), {
      target: { value: "Dmitri" },
    });
    await waitFor(() => expect(sent).toEqual([{ url: "/api/voice/ru", body: { model_id: "Dmitri", accept_licence: false } }]));
  });

  it("shows a restricted voice's licence before choosing it, and can keep the one it has", async () => {
    renderCard();
    const select = await screen.findByRole("combobox", { name: "Voice for Russian" });

    fireEvent.change(select, { target: { value: "Ruslan" } });
    const dialog = screen.getByRole("dialog", { name: "Ruslan" });
    expect(dialog.textContent).toContain("CC-BY-NC-SA-4.0");
    expect(dialog.textContent).toContain("Non-commercial.");
    expect(sent).toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep the voice it has" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.change(select, { target: { value: "Ruslan" } });
    fireEvent.click(screen.getByRole("button", { name: "Accept the licence and use it" }));
    await waitFor(() =>
      expect(sent).toEqual([{ url: "/api/voice/ru", body: { model_id: "Ruslan", accept_licence: true } }]),
    );
  });

  it("downloads a language's voice from its row", async () => {
    renderCard();
    fireEvent.click(within(await screen.findByTestId("voice-de")).getByRole("button", { name: "Download" }));
    await waitFor(() => expect(sent).toEqual([{ url: "/api/voice/de", body: { model_id: "Thorsten", accept_licence: false } }]));
  });

  it("shows someone who isn't an admin how things stand, with nothing to change", async () => {
    renderCard(false);
    const russian = await screen.findByTestId("voice-ru");
    expect(within(russian).getByText("Denis")).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(within(screen.getByTestId("voice-de")).getByText("Not downloaded")).toBeTruthy();
    expect(screen.getByText("Only an admin can choose or download a voice.")).toBeTruthy();
  });
});
