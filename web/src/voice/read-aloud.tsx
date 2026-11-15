import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, ApiError, apiPost } from "../api/client";
import { useIsAdmin } from "../api/session";
import type { VoiceLanguage, VoicesResponse } from "../api/types";
import { DownloadProgress, ErrorNote } from "../components";
import { formatBytes } from "../format";
import { useDownloadEvents } from "../hooks/useDownloadEvents";
import { useLanguageName } from "../hooks/useLanguageName";
import { player, useReading } from "./player";

export const VOICES = ["voice"];

export function useVoices() {
  return useQuery({ queryKey: VOICES, queryFn: () => apiFetch<VoicesResponse>("/api/voice") });
}

/** How much of a reply its language is told from: plenty for that, and well within the
 * 20,000 characters the server looks at, so a long reply can be read too. */
export const LANGUAGE_SAMPLE = 5_000;

/** The language of a reply, among the ones a voice speaks; null when it can't be told. */
export async function replyLanguage(parts: string[]): Promise<string | null> {
  const body = await apiPost<{ language: string | null }>("/api/voice/language", {
    text: parts.join(" ").slice(0, LANGUAGE_SAMPLE),
  });
  return body.language;
}

/** The reply's sentences; null until the Markdown parser has loaded. It is loaded on first use,
 * as the Markdown component is (lazy-markdown.tsx), so it stays out of the page's first load. */
function useSentences(markdown: string): string[] | null {
  const [parts, setParts] = useState<{ markdown: string; sentences: string[] } | null>(null);
  useEffect(() => {
    let current = true;
    void import("./speech-text").then(({ sentences }) => {
      if (current) setParts({ markdown, sentences: sentences(markdown) });
    });
    return () => {
      current = false;
    };
  }, [markdown]);
  return parts?.markdown === markdown ? parts.sentences : null;
}

function installed(language: VoiceLanguage): boolean {
  return language.voices.some((v) => v.id === language.voice && v.installed);
}

/** Read aloud, under a finished reply: finds the reply's language, reads it with that
 * language's voice, and offers the voice when it isn't on this computer (an admin downloads
 * it; anyone else is told to ask one). The language can be changed for this reply.
 * `autoStart`: read it now, unasked (Read replies aloud); `onAutoStarted` says it has begun. */
export function ReadAloud({
  readingKey,
  markdown,
  autoStart = false,
  onAutoStarted,
}: {
  readingKey: string;
  markdown: string;
  autoStart?: boolean;
  onAutoStarted?: () => void;
}) {
  const { t } = useTranslation();
  const name = useLanguageName();
  const parts = useSentences(markdown);
  const status = useReading(readingKey);
  const voices = useVoices();
  const [language, setLanguage] = useState<string | null>(null);
  const [missing, setMissing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function start(code: string | null) {
    if (parts === null || parts.length === 0) return;
    const said = parts;
    setError(null);
    setMissing(null);
    try {
      const found = code ?? (await replyLanguage(said));
      if (found === null) {
        setError(t("voice.noLanguage"));
        return;
      }
      setLanguage(found);
      const known = voices.data?.languages.find((l) => l.code === found);
      if (known === undefined) {
        setError(t("voice.noVoice", { language: name(found) }));
        return;
      }
      if (!installed(known)) {
        setMissing(found);
        return;
      }
      await player.read(readingKey, said, found);
    } catch (failure) {
      setError(
        t("voice.failed", {
          error: failure instanceof ApiError ? failure.detail : String(failure),
        }),
      );
    }
  }

  // Read replies aloud: start once the sentences and the voices are known. Through a ref, so
  // the effect runs when those change, not on every render that makes a new `start`.
  const startRef = useRef(start);
  useEffect(() => {
    startRef.current = start;
  });
  const ready = autoStart && parts !== null && voices.data !== undefined;
  useEffect(() => {
    if (!ready) return;
    onAutoStarted?.();
    void startRef.current(language);
  }, [ready, onAutoStarted, language]);

  if (parts === null || parts.length === 0) return null; // not parsed yet, or only code: nothing to say

  const missingLanguage = voices.data?.languages.find((l) => l.code === missing);
  return (
    <div className="grid gap-2" data-testid="read-aloud">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <button
          className="text-accent hover:underline disabled:text-muted"
          disabled={!voices.data}
          aria-pressed={status !== null}
          onClick={() => (status !== null ? player.stop() : void start(language))}
        >
          {status === "loading" ? t("voice.loading") : status === "playing" ? t("voice.stop") : t("voice.readAloud")}
        </button>
        {language !== null && voices.data && (
          <select
            className="rounded border border-line bg-surface px-1 py-0.5"
            aria-label={t("voice.language")}
            value={language}
            onChange={(event) => {
              player.stop();
              setLanguage(event.target.value);
              void start(event.target.value);
            }}
          >
            {voices.data.languages.map((l) => (
              <option key={l.code} value={l.code}>
                {name(l.code)}
              </option>
            ))}
          </select>
        )}
      </div>
      {missingLanguage && !installed(missingLanguage) && <MissingVoice language={missingLanguage} />}
      {error && <ErrorNote message={error} />}
    </div>
  );
}

/** A language whose voice isn't on this computer: an admin downloads it here, with its size
 * (and its licence, when that has to be accepted); anyone else is told to ask an admin. */
function MissingVoice({ language }: { language: VoiceLanguage }) {
  const { t } = useTranslation();
  const name = useLanguageName();
  const admin = useIsAdmin();
  const voice = language.voices.find((v) => v.id === language.voice)!;
  if (!admin) {
    return <p className="text-xs text-muted">{t("voice.askAdmin", { language: name(language.code) })}</p>;
  }
  return <DownloadVoice language={language} voiceId={voice.id} />;
}

function DownloadVoice({ language, voiceId }: { language: VoiceLanguage; voiceId: string }) {
  const { t } = useTranslation();
  const name = useLanguageName();
  const queryClient = useQueryClient();
  const live = useDownloadEvents();
  const voice = language.voices.find((v) => v.id === voiceId)!;
  const [accepted, setAccepted] = useState(!voice.licence_notice);
  const download = useMutation({
    mutationFn: () =>
      apiFetch<VoicesResponse>(`/api/voice/${encodeURIComponent(language.code)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: voice.id, accept_licence: accepted }),
      }),
    onSuccess: (body) => queryClient.setQueryData(VOICES, body),
  });
  const state = live[voice.id] ?? voice.download;
  const busy = state.status === "downloading" || state.status === "queued" || state.status === "verifying";
  return (
    <div className="grid gap-1 text-xs" data-testid="missing-voice">
      {busy ? (
        <DownloadProgress state={state} />
      ) : (
        <>
          {voice.licence_notice && (
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={accepted} onChange={(e) => setAccepted(e.target.checked)} />
              {t("voice.acceptLicence", { licence: voice.licence })}
            </label>
          )}
          <button
            className="btn-secondary w-fit px-2 py-0.5"
            disabled={!accepted || download.isPending}
            onClick={() => download.mutate()}
          >
            {t("voice.download", { language: name(language.code), size: formatBytes(voice.size) })}
          </button>
        </>
      )}
      {(state.error || download.error) && (
        <ErrorNote
          message={download.error instanceof ApiError ? download.error.detail : (state.error ?? t("common.loadFailed"))}
        />
      )}
    </div>
  );
}
