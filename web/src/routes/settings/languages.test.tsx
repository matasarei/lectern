import "../../i18n";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetSession, setSession } from "../../api/session";
import type {
  Language,
  LanguageOption,
  LanguagesResponse,
} from "../../api/types";
import { installFakeSockets } from "../../test/fake-socket";
import { LanguagesPage } from "./languages";

vi.mock("@tanstack/react-router", () => ({
  Link: ({ to, children }: { to: string; children: ReactNode }) => (
    <a href={to}>{children}</a>
  ),
}));

const GB = 1_000_000_000;

function option(
  id: string,
  extra: Partial<LanguageOption> = {},
): LanguageOption {
  return {
    id,
    title: id === "salamandrata-2b" ? "SalamandraTA 2B" : "TranslateGemma 4B",
    size: id === "salamandrata-2b" ? 1.5 * GB : 2.5 * GB,
    licence: "Apache-2.0",
    licence_notice: false,
    notes: null,
    best: false,
    open_licence: false,
    download: {
      model_id: id,
      status: "none",
      bytes_done: 0,
      bytes_total: GB,
      rate: 0,
      error: null,
    },
    ...extra,
  };
}

const SALAMANDRA = () => option("salamandrata-2b", { open_licence: true });
const GEMMA = () =>
  option("translategemma-4b", {
    best: true,
    licence: "Gemma Terms of Use",
    licence_notice: true,
    notes: "Read the Gemma terms before using it for work.",
  });

type Sent = { method: string; url: string; body: unknown };
let languages: Language[];
let sent: Sent[];
let refusal: [number, string] | null;

beforeEach(() => {
  languages = [
    { code: "de", added: null, options: [GEMMA(), SALAMANDRA()] },
    { code: "ja", added: null, options: [] },
    { code: "ko", added: null, options: [] },
    {
      code: "uk",
      added: { model: "salamandrata-2b", previous: null },
      options: [
        GEMMA(),
        option("salamandrata-2b", {
          open_licence: true,
          download: {
            model_id: "salamandrata-2b",
            status: "done",
            bytes_done: GB,
            bytes_total: GB,
            rate: 0,
            error: null,
          },
        }),
      ],
    },
  ];
  sent = [];
  refusal = null;
  installFakeSockets();
  window.sessionStorage.setItem("lectern.token", "t");
  forgetSession();
  setSession({ accounts: true, user: { login: "yevhen", role: "admin" } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      const answer = () =>
        new Response(
          JSON.stringify({ tier: "minimum", languages } as LanguagesResponse),
        );
      if (method === "GET") return answer();
      const body = init.body
        ? (JSON.parse(init.body as string) as Record<string, unknown>)
        : null;
      sent.push({ method, url, body });
      if (refusal)
        return new Response(JSON.stringify({ detail: refusal[1] }), {
          status: refusal[0],
        });
      const code = url.split("/").pop() as string;
      languages = languages.map((l) => {
        if (l.code !== code) return l;
        if (method === "DELETE") return { ...l, added: null };
        const model = String(body?.model_id);
        const previous =
          l.added && l.added.model !== model ? l.added.model : null;
        return { ...l, added: { model, previous } };
      });
      return answer();
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LanguagesPage />
    </QueryClientProvider>,
  );
}

/** A language's row, once the page has it. */
async function row(code: string) {
  const found = await waitFor(() => {
    const element = rowOf(code);
    if (!element) throw new Error(`no row for ${code}`);
    return element;
  });
  return within(found);
}

function rowOf(code: string): HTMLElement | null {
  return document.querySelector(`[data-language="${code}"]`);
}

describe("Languages", () => {
  it("shows each added language with its model, help for it, and where it stands", async () => {
    renderPage();
    const uk = await row("uk");

    expect(uk.getByText("Ukrainian")).toBeTruthy();
    const select = uk.getByLabelText(
      "Model for Ukrainian",
    ) as HTMLSelectElement;
    expect(select.value).toBe("salamandrata-2b");
    expect([...select.options].map((o) => o.text)).toEqual([
      "TranslateGemma 4B · 2.5 GB · best results",
      "SalamandraTA 2B · 1.5 GB · open licence",
    ]);
    expect(uk.getByTestId("option-help").textContent).toBe(
      "Open licence: use it for anything, work included.",
    );
    expect(uk.getByTestId("status-uk").textContent).toBe("Downloaded");
    expect(rowOf("de")).toBeNull(); // not added
  });

  it("lists the languages the chat model translates as well under the table", async () => {
    renderPage();
    expect((await screen.findByTestId("chat-only")).textContent).toBe(
      "Japanese, Korean: your chat model already translates these as well as any model this computer can run.",
    );
  });

  it("adds a language as a row on the open-licence model, and sends nothing before Download", async () => {
    renderPage();
    const add = await screen.findByLabelText("Add a language:");
    expect([...(add as HTMLSelectElement).options].map((o) => o.text)).toEqual([
      "Choose…",
      "German",
    ]);

    fireEvent.change(add, { target: { value: "de" } });

    const de = within(rowOf("de") as HTMLElement);
    expect(
      (de.getByLabelText("Model for German") as HTMLSelectElement).value,
    ).toBe("salamandrata-2b");
    expect(sent).toEqual([]);
    fireEvent.click(
      de.getByRole("button", { name: "Download SalamandraTA 2B" }),
    );

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      method: "PUT",
      url: "/api/languages/de",
      body: { model_id: "salamandrata-2b", accept_licence: false },
    });
    await waitFor(() =>
      expect(
        within(rowOf("de") as HTMLElement).getByText("Remove"),
      ).toBeTruthy(),
    );
  });

  it("asks for a restricted licence to be accepted, and says what it limits", async () => {
    renderPage();
    fireEvent.change(await screen.findByLabelText("Add a language:"), {
      target: { value: "de" },
    });
    const de = within(rowOf("de") as HTMLElement);

    fireEvent.change(de.getByLabelText("Model for German"), {
      target: { value: "translategemma-4b" },
    });

    expect(de.getByTestId("option-help").textContent).toBe(
      "Best results on this computer." +
        "Restricted licence (Gemma Terms of Use): read it before using the model for work. " +
        "Read the Gemma terms before using it for work.",
    );
    expect(sent).toEqual([]);
    fireEvent.click(
      de.getByRole("button", {
        name: "Accept the Gemma Terms of Use licence and download TranslateGemma 4B",
      }),
    );
    await waitFor(() =>
      expect(sent[0]?.body).toEqual({
        model_id: "translategemma-4b",
        accept_licence: true,
      }),
    );
  });

  it("a new row can be cancelled, and nothing is sent", async () => {
    renderPage();
    fireEvent.change(await screen.findByLabelText("Add a language:"), {
      target: { value: "de" },
    });
    fireEvent.click(
      within(rowOf("de") as HTMLElement).getByRole("button", {
        name: "Cancel",
      }),
    );
    expect(rowOf("de")).toBeNull();
    expect(sent).toEqual([]);
  });

  it("changing an added language to an open-licence model saves it at once", async () => {
    languages = languages.map((l) =>
      l.code === "uk"
        ? {
            ...l,
            added: { model: "translategemma-4b", previous: null },
            options: [GEMMA(), SALAMANDRA()],
          }
        : l,
    );
    renderPage();
    const uk = await row("uk");
    fireEvent.change(uk.getByLabelText("Model for Ukrainian"), {
      target: { value: "salamandrata-2b" },
    });
    await waitFor(() =>
      expect(sent).toEqual([
        {
          method: "PUT",
          url: "/api/languages/uk",
          body: { model_id: "salamandrata-2b", accept_licence: false },
        },
      ]),
    );
    await waitFor(() =>
      expect(
        within(rowOf("uk") as HTMLElement).getByText(
          "TranslateGemma 4B translates until this one is downloaded.",
        ),
      ).toBeTruthy(),
    );
  });

  it("a restricted model picked for an added language waits, and can be put back", async () => {
    renderPage();
    const uk = await row("uk");
    fireEvent.change(uk.getByLabelText("Model for Ukrainian"), {
      target: { value: "translategemma-4b" },
    });
    expect(sent).toEqual([]);
    fireEvent.click(uk.getByRole("button", { name: "Keep the current one" }));
    expect(
      (uk.getByLabelText("Model for Ukrainian") as HTMLSelectElement).value,
    ).toBe("salamandrata-2b");
    expect(sent).toEqual([]);
  });

  it("removes a language", async () => {
    renderPage();
    fireEvent.click(
      (await row("uk")).getByRole("button", { name: "Remove Ukrainian" }),
    );
    await waitFor(() =>
      expect(sent).toEqual([
        { method: "DELETE", url: "/api/languages/uk", body: null },
      ]),
    );
    await waitFor(() => expect(rowOf("uk")).toBeNull());
  });

  it("a failed download can be tried again", async () => {
    languages = languages.map((l) =>
      l.code === "uk"
        ? {
            ...l,
            options: [
              GEMMA(),
              option("salamandrata-2b", {
                open_licence: true,
                download: {
                  model_id: "salamandrata-2b",
                  status: "failed",
                  bytes_done: 0,
                  bytes_total: GB,
                  rate: 0,
                  error: "Not enough disk space.",
                },
              }),
            ],
          }
        : l,
    );
    renderPage();
    const uk = await row("uk");
    expect(uk.getByText("Not enough disk space.")).toBeTruthy();
    fireEvent.click(
      uk.getByRole("button", { name: "Download SalamandraTA 2B" }),
    );
    await waitFor(() =>
      expect(sent[0]?.body).toEqual({
        model_id: "salamandrata-2b",
        accept_licence: false,
      }),
    );
  });

  it("shows the server's refusal", async () => {
    refusal = [409, "SalamandraTA 2B isn't one of the models for German here."];
    renderPage();
    fireEvent.change(await screen.findByLabelText("Add a language:"), {
      target: { value: "de" },
    });
    fireEvent.click(
      within(rowOf("de") as HTMLElement).getByRole("button", {
        name: "Download SalamandraTA 2B",
      }),
    );
    expect((await screen.findByRole("alert")).textContent).toBe(
      "SalamandraTA 2B isn't one of the models for German here.",
    );
  });

  it("a member sees the languages and changes nothing", async () => {
    setSession({ accounts: true, user: { login: "olena", role: "member" } });
    renderPage();
    const uk = await row("uk");
    expect(uk.getByText("SalamandraTA 2B")).toBeTruthy();
    expect(uk.queryByRole("combobox")).toBeNull();
    expect(uk.queryByRole("button")).toBeNull();
    expect(screen.queryByLabelText("Add a language:")).toBeNull();
    expect(
      screen.getByText(
        "Only an admin can add or remove languages: adding one downloads a model.",
      ),
    ).toBeTruthy();
  });
});
