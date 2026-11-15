import "../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetSession, setSession } from "../api/session";
import type { VoiceLanguage, VoiceOption } from "../api/types";
import { installFakeSockets } from "../test/fake-socket";
import { player } from "./player";
import { ReadAloud } from "./read-aloud";

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
    default: true,
    installed: true,
    download: { model_id: id, status: "done", bytes_done: 0, bytes_total: 0, rate: 0, error: null },
    ...extra,
  };
}

type Sent = { method: string; url: string; body: Record<string, unknown> | null };
let languages: VoiceLanguage[];
let detected: string | null;
let sent: Sent[];
let sources: { stopped: boolean }[];

class FakeAudioContext {
  destination = {};
  async resume() {}
  async decodeAudioData() {
    return {};
  }
  createBufferSource() {
    const source = { buffer: null, onended: null, stopped: false, connect() {}, start() {}, stop() {} };
    source.stop = () => {
      source.stopped = true;
    };
    sources.push(source);
    return source;
  }
}

beforeEach(() => {
  languages = [
    { code: "en", voice: "piper-en", chosen: null, voices: [voice("piper-en")] },
    {
      code: "ru",
      voice: "piper-ru",
      chosen: null,
      voices: [voice("piper-ru", { installed: false, download: { ...voice("piper-ru").download, status: "none" } })],
    },
  ];
  detected = "en";
  sent = [];
  sources = [];
  installFakeSockets();
  window.sessionStorage.setItem("lectern.token", "t");
  forgetSession();
  setSession({ accounts: true, user: { login: "yevhen", role: "admin" } });
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      const body = init.body ? (JSON.parse(init.body as string) as Record<string, unknown>) : null;
      if (method !== "GET") sent.push({ method, url, body });
      if (url === "/api/voice/language") return new Response(JSON.stringify({ language: detected }));
      if (url === "/api/voice/speak") return new Response(new Uint8Array([82, 73, 70, 70]));
      return new Response(JSON.stringify({ languages }));
    }),
  );
});

afterEach(() => {
  player.stop();
  cleanup();
  vi.unstubAllGlobals();
});

function renderReply(markdown: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ReadAloud readingKey="1:5" markdown={markdown} />
    </QueryClientProvider>,
  );
}

async function readAloud() {
  const button = await screen.findByRole("button", { name: "Read aloud" });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(button);
}

describe("ReadAloud", () => {
  it("finds the reply's language, reads it sentence by sentence, and stops", async () => {
    renderReply("The rent is 900.\n\n```\nignored\n```\n\nIt is due in May.");
    await readAloud();

    const stop = await screen.findByRole("button", { name: "Stop reading" });
    expect(sent[0]).toEqual({
      method: "POST",
      url: "/api/voice/language",
      body: { text: expect.stringContaining("The rent is 900.") },
    });
    const spoken = sent.filter((s) => s.url === "/api/voice/speak").map((s) => s.body);
    expect(spoken[0]).toEqual({ text: "The rent is 900.", language: "en" });
    expect((screen.getByRole("combobox", { name: "Language to read in" }) as HTMLSelectElement).value).toBe("en");

    fireEvent.click(stop);
    expect(await screen.findByRole("button", { name: "Read aloud" })).toBeTruthy();
    expect(sources[0].stopped).toBe(true);
  });

  it("offers an admin the download of a language's missing voice", async () => {
    detected = "ru";
    renderReply("Аренда 900 рублей.");
    await readAloud();

    fireEvent.click(await screen.findByRole("button", { name: "Download the Russian voice (63 MB)" }));

    await waitFor(() => expect(sent.at(-1)?.method).toBe("PUT"));
    expect(sent.at(-1)).toEqual({
      method: "PUT",
      url: "/api/voice/ru",
      body: { model_id: "piper-ru", accept_licence: true },
    });
    expect(sent.some((s) => s.url === "/api/voice/speak")).toBe(false);
  });

  it("asks for the licence first when the voice's has to be accepted", async () => {
    detected = "ru";
    languages[1].voices[0].licence_notice = true;
    languages[1].voices[0].licence = "CC BY-NC-SA 4.0";
    renderReply("Аренда 900 рублей.");
    await readAloud();

    const download = await screen.findByRole("button", { name: "Download the Russian voice (63 MB)" });
    expect((download as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: "I've read its licence (CC BY-NC-SA 4.0) and accept it" }));
    expect((download as HTMLButtonElement).disabled).toBe(false);
  });

  it("tells someone who isn't an admin to ask one", async () => {
    setSession({ accounts: true, user: { login: "olena", role: "member" } });
    detected = "ru";
    renderReply("Аренда 900 рублей.");
    await readAloud();

    expect(
      await screen.findByText("There's no Russian voice on this computer yet. Ask an admin to add it."),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Download/ })).toBeNull();
  });

  it("says so when the language can't be told, and reads in the one picked", async () => {
    detected = null;
    renderReply("Hmm, 42.");
    await readAloud();
    expect((await screen.findByRole("alert")).textContent).toContain("Can't tell which language");
    expect(sent.some((s) => s.url === "/api/voice/speak")).toBe(false);
  });

  it("reads again in another language when one is picked", async () => {
    renderReply("Ciao, come stai?");
    await readAloud();
    await screen.findByRole("button", { name: "Stop reading" });

    fireEvent.change(screen.getByRole("combobox", { name: "Language to read in" }), {
      target: { value: "ru" },
    });

    expect(await screen.findByRole("button", { name: "Download the Russian voice (63 MB)" })).toBeTruthy();
    expect(sent.filter((s) => s.url === "/api/voice/language")).toHaveLength(1); // not asked again
  });

  it("has no button for a reply that is only code", async () => {
    renderReply("```\nx = 1\n```");
    await act(async () => {
      await import("./speech-text"); // the parser the button waits for has loaded
    });
    expect(screen.queryByTestId("read-aloud")).toBeNull();
  });
});
