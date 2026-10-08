import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, ApiError } from "../api/client";
import type { Artifact, Tab } from "../api/types";
import { ErrorNote } from "../components";

interface Format {
  format: string;
  extension: string;
  capabilities: string[];
}

/** One click from a Document to a file: DOCX, ODT, MD… whatever export modules are
 * installed. The new file opens in its tab. Where a format loses something (MD has no
 * comments), it says what and asks first, as the chat's card does. */
export function ExportButtons({
  projectId,
  artifactId,
  onOpen,
}: {
  projectId: number;
  artifactId: number; // what to export: a session's document.md, a Markdown file…
  onOpen: (tab: Tab) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const formats = useQuery({
    queryKey: ["exporters"],
    queryFn: () => apiFetch<Format[]>("/api/exporters"),
    staleTime: Infinity, // modules don't change while the page is open
  });
  // A format that loses something: what it loses, until the person says go on or not
  const [asking, setAsking] = useState<{ format: string; detail: string } | null>(null);
  const run = useMutation({
    mutationFn: ({ format, accept }: { format: string; accept: boolean }) =>
      apiPost<Artifact>(`/api/projects/${projectId}/export`, {
        artifact_id: artifactId,
        format,
        accept_loss: accept,
      }),
    onSuccess: (file) => {
      setAsking(null);
      void queryClient.invalidateQueries({ queryKey: ["items", projectId] });
      onOpen({ kind: "artifact", id: file.id });
    },
    onError: (error, { format }) => {
      if (error instanceof ApiError && error.status === 409) setAsking({ format, detail: error.detail });
    },
  });
  if (!formats.data?.length) return null;
  const failed = run.error && !(run.error instanceof ApiError && run.error.status === 409);

  return (
    <div className="grid gap-2 text-sm" data-testid="export">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted">{t("export.label")}</span>
        {formats.data.map((f) => (
          <button
            key={f.format}
            className="btn-secondary px-2 py-0.5 text-xs"
            disabled={run.isPending}
            onClick={() => run.mutate({ format: f.format, accept: false })}
          >
            {f.format.toUpperCase()}
          </button>
        ))}
      </div>
      {asking && (
        <div className="flex flex-wrap items-center gap-2" role="alert">
          <span>{asking.detail}</span>
          <button
            className="btn-primary px-2 py-0.5 text-xs"
            disabled={run.isPending}
            onClick={() => run.mutate({ format: asking.format, accept: true })}
          >
            {t("export.anyway")}
          </button>
          <button className="btn-secondary px-2 py-0.5 text-xs" onClick={() => setAsking(null)}>
            {t("export.cancel")}
          </button>
        </div>
      )}
      {failed && (
        <ErrorNote message={run.error instanceof ApiError ? run.error.detail : t("common.loadFailed")} />
      )}
    </div>
  );
}
