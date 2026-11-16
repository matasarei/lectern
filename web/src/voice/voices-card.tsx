import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, ApiError } from "../api/client";
import type { DownloadState, VoiceLanguage, VoiceOption, VoicesResponse } from "../api/types";
import { Card, DownloadProgress, ErrorNote } from "../components";
import { formatBytes } from "../format";
import { useLanguageName } from "../hooks/useLanguageName";
import { useVoices, VOICES } from "./read-aloud";

type Choice = { code: string; voice: VoiceOption };

/** Models & memory's Voices: each language a voice speaks, the voice it reads with, and
 * whether that voice is on this computer. An admin picks a language's voice (a restricted
 * licence is read and accepted first) and downloads it; anyone else sees how things stand. */
export function VoicesCard({ admin, live }: { admin: boolean; live: Record<string, DownloadState> }) {
  const { t } = useTranslation();
  const name = useLanguageName();
  const queryClient = useQueryClient();
  const voices = useVoices();
  const [asking, setAsking] = useState<Choice | null>(null); // a restricted voice, licence shown
  const choose = useMutation({
    mutationFn: ({ code, voice }: Choice) =>
      apiFetch<VoicesResponse>(`/api/voice/${encodeURIComponent(code)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: voice.id, accept_licence: voice.licence_notice }),
      }),
    onSuccess: (body) => {
      setAsking(null);
      queryClient.setQueryData(VOICES, body);
    },
  });
  if (!voices.data) return null;

  function pick(language: VoiceLanguage, id: string) {
    const voice = language.voices.find((v) => v.id === id);
    if (!voice) return;
    if (voice.licence_notice) setAsking({ code: language.code, voice });
    else choose.mutate({ code: language.code, voice });
  }

  const languages = [...voices.data.languages].sort((a, b) => name(a.code).localeCompare(name(b.code)));
  return (
    <Card title={t("voice.cardTitle")}>
      <p className="text-sm text-muted">{t("voice.cardWhat")}</p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-xs uppercase tracking-wider text-muted">
            <tr>
              <th className="py-2 pr-4 font-semibold">{t("voice.languageColumn")}</th>
              <th className="py-2 pr-4 font-semibold">{t("voice.voiceColumn")}</th>
              <th className="py-2 pr-4 font-semibold">{t("models.size")}</th>
              <th className="py-2 pr-4 font-semibold">{t("models.licence")}</th>
              <th className="py-2 font-semibold">{t("models.status")}</th>
            </tr>
          </thead>
          <tbody>
            {languages.map((language) => {
              const voice = language.voices.find((v) => v.id === language.voice)!;
              const state = live[voice.id] ?? voice.download;
              const busy = ["queued", "downloading", "verifying"].includes(state.status);
              return (
                <tr key={language.code} className="border-t border-line align-top" data-testid={`voice-${language.code}`}>
                  <td className="py-2 pr-4">{name(language.code)}</td>
                  <td className="py-2 pr-4">
                    {admin && language.voices.length > 1 ? (
                      <select
                        className="rounded border border-line bg-surface px-1 py-0.5"
                        aria-label={t("voice.voiceFor", { language: name(language.code) })}
                        value={language.voice}
                        disabled={choose.isPending}
                        onChange={(event) => pick(language, event.target.value)}
                      >
                        {language.voices.map((v) => (
                          <option key={v.id} value={v.id}>
                            {v.default ? t("voice.recommended", { title: v.title }) : v.title}
                          </option>
                        ))}
                      </select>
                    ) : (
                      voice.title
                    )}
                  </td>
                  <td className="py-2 pr-4 whitespace-nowrap tabular-nums">{formatBytes(voice.size)}</td>
                  <td className="py-2 pr-4">{voice.licence}</td>
                  <td className="py-2">
                    {voice.installed ? (
                      t("voice.installed")
                    ) : busy ? (
                      <DownloadProgress state={state} />
                    ) : admin ? (
                      <button
                        className="btn-secondary px-2 py-0.5 text-xs"
                        disabled={choose.isPending}
                        onClick={() => choose.mutate({ code: language.code, voice })}
                      >
                        {t("modelsPage.download")}
                      </button>
                    ) : (
                      t("voice.notInstalled")
                    )}
                    {state.error && <ErrorNote message={state.error} />}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {asking && (
        <div className="grid gap-2 rounded-md border border-doubt bg-doubt/10 p-3 text-sm" role="dialog" aria-label={asking.voice.title}>
          <p>{t("voice.restricted", { title: asking.voice.title, licence: asking.voice.licence })}</p>
          {asking.voice.notes && <p className="text-muted">{asking.voice.notes}</p>}
          <div className="flex gap-2">
            <button className="btn-primary" disabled={choose.isPending} onClick={() => choose.mutate(asking)}>
              {t("voice.acceptAndUse")}
            </button>
            <button className="btn-secondary" onClick={() => setAsking(null)}>
              {t("voice.keep")}
            </button>
          </div>
        </div>
      )}
      {choose.error && (
        <ErrorNote message={choose.error instanceof ApiError ? choose.error.detail : t("common.loadFailed")} />
      )}
      {!admin && <p className="text-sm text-muted">{t("voice.adminOnly")}</p>}
    </Card>
  );
}
