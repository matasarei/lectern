import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, ApiError } from "../../api/client";
import { useIsAdmin } from "../../api/session";
import type {
  DownloadState,
  Language,
  LanguageOption,
  LanguagesResponse,
} from "../../api/types";
import {
  Card,
  DownloadProgress,
  ErrorNote,
  StatusText,
} from "../../components";
import { formatBytes } from "../../format";
import { useDownloadEvents } from "../../hooks/useDownloadEvents";
import { useLanguageName } from "../../hooks/useLanguageName";

const LIST = ["languages"];

/** The option a new row starts on: the best with an open licence, so a restricted model is
 * only ever taken with a click; the best of them when none has one. */
function startingOption(language: Language): LanguageOption | undefined {
  return (
    language.options.find((o) => o.open_licence) ??
    language.options.find((o) => o.offered)
  );
}

/** Settings › Languages: the languages added for translation, each with the model it
 * translates with, and adding another. Someone who isn't an admin sees it and changes none of
 * it: adding a language downloads a model. */
export function LanguagesPage() {
  const { t } = useTranslation();
  const admin = useIsAdmin();
  const live = useDownloadEvents();
  const nameOf = useLanguageName();
  // Languages picked in "Add a language" but not saved yet: by code, nothing downloads until
  // their row's Download.
  const [drafts, setDrafts] = useState<string[]>([]);
  const list = useQuery({
    queryKey: LIST,
    queryFn: () => apiFetch<LanguagesResponse>("/api/languages"),
  });

  if (list.isPending) return <Page>{t("common.loading")}</Page>;
  if (list.isError) {
    return (
      <Page>
        <ErrorNote
          message={
            list.error instanceof ApiError
              ? list.error.detail
              : t("common.loadFailed")
          }
        />
      </Page>
    );
  }

  const byName = (a: Language, b: Language) =>
    nameOf(a.code).localeCompare(nameOf(b.code));
  const languages = list.data.languages;
  const rows = languages
    .filter((l) => l.added || drafts.includes(l.code))
    .sort(byName);
  const addable = languages
    .filter((l) => !l.added && !drafts.includes(l.code) && l.options.length > 0)
    .sort(byName);
  // No option: measured, and the chat model is as good; or not measured on this computer's
  // chat model at all, which says nothing either way.
  const nothing = languages.filter((l) => !l.added && l.options.length === 0);
  const chatOnly = nothing.filter((l) => l.chat_score !== null).sort(byName);
  const untested = nothing.filter((l) => l.chat_score === null).sort(byName);
  const discard = (code: string) =>
    setDrafts((d) => d.filter((c) => c !== code));

  return (
    <Page>
      <Card title={t("languagesPage.added")}>
        <p className="text-sm text-muted">{t("languagesPage.what")}</p>
        {!admin && (
          <p className="text-sm text-muted">{t("languagesPage.adminOnly")}</p>
        )}
        {rows.length === 0 ? (
          <p className="text-sm">{t("languagesPage.none")}</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-wider text-muted">
                <tr>
                  <th className="py-2 pr-4 font-semibold">
                    {t("languagesPage.language")}
                  </th>
                  <th className="py-2 pr-4 font-semibold">
                    {t("models.model")}
                  </th>
                  <th className="py-2 font-semibold">{t("models.status")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((language) => (
                  <LanguageRow
                    key={language.code}
                    language={language}
                    name={nameOf(language.code)}
                    live={live}
                    onSaved={() => discard(language.code)}
                    onDiscard={() => discard(language.code)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
        {admin && addable.length > 0 && (
          <label className="flex flex-wrap items-center gap-2 text-sm">
            {t("languagesPage.add")}
            <select
              className="block w-48 max-w-full rounded border border-line bg-paper px-2 py-1"
              value=""
              onChange={(e) => {
                const code = e.target.value;
                if (code) setDrafts((d) => [...d, code]);
              }}
            >
              <option value="">{t("languagesPage.choose")}</option>
              {addable.map((l) => (
                <option key={l.code} value={l.code}>
                  {nameOf(l.code)}
                </option>
              ))}
            </select>
          </label>
        )}
        {chatOnly.length > 0 && (
          <p className="text-sm text-muted" data-testid="chat-only">
            {t("languagesPage.chatOnly", {
              languages: chatOnly.map((l) => nameOf(l.code)).join(", "),
            })}
          </p>
        )}
        {untested.length > 0 && (
          <p className="text-sm text-muted" data-testid="untested">
            {t("languagesPage.untested", {
              languages: untested.map((l) => nameOf(l.code)).join(", "),
            })}
          </p>
        )}
      </Card>
    </Page>
  );
}

function LanguageRow({
  language,
  name,
  live,
  onSaved,
  onDiscard,
}: {
  language: Language;
  name: string;
  live: Record<string, DownloadState>;
  onSaved: () => void;
  onDiscard: () => void;
}) {
  const { t } = useTranslation();
  const admin = useIsAdmin();
  const queryClient = useQueryClient();
  const saved = language.added;
  const byId = new Map(language.options.map((o) => [o.id, o]));
  // What the dropdown shows: the language's model, or for a new row the starting option.
  const [picked, setPicked] = useState(
    saved?.model ?? startingOption(language)?.id ?? "",
  );
  const option = byId.get(picked);
  const save = useMutation({
    mutationFn: ({ modelId, accept }: { modelId: string; accept: boolean }) =>
      apiFetch<LanguagesResponse>(
        `/api/languages/${encodeURIComponent(language.code)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model_id: modelId, accept_licence: accept }),
        },
      ),
    onSuccess: (data) => {
      queryClient.setQueryData(LIST, data);
      onSaved();
    },
  });
  const remove = useMutation({
    mutationFn: () =>
      apiFetch<LanguagesResponse>(
        `/api/languages/${encodeURIComponent(language.code)}`,
        {
          method: "DELETE",
        },
      ),
    onSuccess: (data) => queryClient.setQueryData(LIST, data),
  });
  const pending = save.isPending || remove.isPending;
  // A choice not saved yet: a new row, or another model picked for an added language.
  const unsaved = !saved || picked !== saved.model;
  const state = option ? (live[option.id] ?? option.download) : undefined;
  const previous = saved?.previous ? byId.get(saved.previous) : undefined;
  const failure = save.error ?? remove.error;

  return (
    <tr
      className="border-t border-line align-top"
      data-language={language.code}
    >
      <td className="py-2 pr-4">{name}</td>
      <td className="py-2 pr-4">
        {admin && language.options.length > 1 ? (
          // A fixed width, as on Models & memory: a list is as wide as its longest option
          // otherwise, and pushes the Status column off the card.
          <select
            aria-label={t("languagesPage.modelFor", { language: name })}
            className="block w-48 max-w-full rounded border border-line bg-paper px-2 py-1"
            value={picked}
            disabled={pending}
            onChange={(e) => {
              const next = byId.get(e.target.value);
              setPicked(e.target.value);
              // An open licence saves at once; a restricted one waits for its licence to be
              // accepted below.
              if (
                saved &&
                next &&
                !next.licence_notice &&
                next.id !== saved.model
              ) {
                save.mutate({ modelId: next.id, accept: false });
              }
            }}
          >
            {language.options.map((o) => (
              <option key={o.id} value={o.id}>
                {t(
                  o.best
                    ? "languagesPage.optionBest"
                    : o.open_licence
                      ? "languagesPage.optionOpen"
                      : "modelsPage.option",
                  {
                    title: o.title,
                    size: formatBytes(o.size),
                  },
                )}
              </option>
            ))}
          </select>
        ) : (
          <span>
            {option?.title ?? saved?.model}
            {option && (
              <span className="text-muted"> · {formatBytes(option.size)}</span>
            )}
          </span>
        )}
        {option && <Help option={option} chatScore={language.chat_score} />}
        {previous && state?.status !== "done" && (
          <span className="block text-muted">
            {t("languagesPage.previousTranslates", { title: previous.title })}
          </span>
        )}
      </td>
      <td className="py-2">
        {admin && unsaved && option ? (
          <span className="flex flex-wrap gap-2">
            <button
              className="btn-secondary w-fit"
              disabled={pending}
              aria-label={
                option.licence_notice
                  ? t("modelsPage.acceptAndDownloadModel", {
                      licence: option.licence,
                      title: option.title,
                    })
                  : t("modelsPage.downloadModel", { title: option.title })
              }
              onClick={() =>
                save.mutate({
                  modelId: option.id,
                  accept: option.licence_notice,
                })
              }
            >
              {option.licence_notice
                ? t("modelsPage.acceptAndDownload")
                : t("modelsPage.download")}
            </button>
            {!saved && (
              <button
                className="btn-secondary w-fit"
                disabled={pending}
                onClick={onDiscard}
              >
                {t("languagesPage.cancel")}
              </button>
            )}
            {saved && (
              <button
                className="btn-secondary w-fit"
                disabled={pending}
                onClick={() => setPicked(saved.model)}
              >
                {t("languagesPage.keep")}
              </button>
            )}
          </span>
        ) : (
          state && (
            <span className="grid gap-1">
              <span data-testid={`status-${language.code}`}>
                {state.status === "done" ? (
                  t("languagesPage.downloaded")
                ) : (
                  <StatusText state={state} />
                )}
              </span>
              {state.status === "downloading" && (
                <DownloadProgress state={state} />
              )}
              {state.error && <ErrorNote message={state.error} />}
              {admin &&
                ["none", "failed", "paused"].includes(state.status) &&
                option && (
                  <button
                    className="btn-secondary w-fit"
                    disabled={pending}
                    aria-label={t("modelsPage.downloadModel", {
                      title: option.title,
                    })}
                    onClick={() =>
                      save.mutate({
                        modelId: option.id,
                        accept: option.licence_notice,
                      })
                    }
                  >
                    {state.status === "failed"
                      ? t("languagesPage.tryAgain")
                      : state.bytes_done > 0
                        ? t("modelsPage.resume")
                        : t("modelsPage.download")}
                  </button>
                )}
            </span>
          )
        )}
        {admin && saved && (
          <button
            className="mt-1 block w-fit font-semibold text-accent underline-offset-4 hover:underline"
            disabled={pending}
            aria-label={t("languagesPage.removeLanguage", { language: name })}
            onClick={() => remove.mutate()}
          >
            {t("modelsPage.remove")}
          </button>
        )}
        {failure && (
          <ErrorNote
            message={
              failure instanceof ApiError
                ? failure.detail
                : t("common.loadFailed")
            }
          />
        )}
      </td>
    </tr>
  );
}

/** Under the dropdown: what the option picked is, and why one would pick it. */
function Help({
  option,
  chatScore,
}: {
  option: LanguageOption;
  chatScore: number | null;
}) {
  const { t } = useTranslation();
  return (
    <span className="mt-1 block max-w-sm text-muted" data-testid="option-help">
      {option.best && (
        <span className="block">{t("languagesPage.bestWhy")}</span>
      )}
      {!option.offered && (
        <span className="block">{t("languagesPage.notOffered")}</span>
      )}
      {option.score !== null && (
        <span className="block">
          {chatScore === null
            ? t("languagesPage.score", { score: option.score })
            : t("languagesPage.scoreVsChat", { score: option.score, chat: chatScore })}
        </span>
      )}
      {option.licence_notice ? (
        <span className="block">
          {t("languagesPage.restrictedWhy", { licence: option.licence })}
          {option.notes && ` ${option.notes}`}
        </span>
      ) : (
        <span className="block">{t("languagesPage.openWhy")}</span>
      )}
    </span>
  );
}

function Page({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  return (
    <main className="mx-auto grid max-w-3xl gap-5 px-4 py-12">
      <header className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">{t("languagesPage.title")}</h1>
        <Link to="/settings/models" className="link-back">
          {t("languagesPage.back")}
        </Link>
      </header>
      {children}
    </main>
  );
}
