import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, ApiError } from "../api/client";
import type { Artifact, ProjectItems, SearchHit, SessionSummary, Tab } from "../api/types";
import { uploadFile } from "../api/upload";
import { ErrorNote } from "../components";
import { fileType, formatAgo, formatBytes } from "../format";

type Filter = "all" | "sessions" | "imported" | "produced";
const FILTERS: Filter[] = ["all", "sessions", "imported", "produced"];

type Row =
  | { kind: "session"; modified: string; session: SessionSummary }
  | { kind: "artifact"; modified: string; artifact: Artifact };

/** Overview: everything in the project, like a folder, newest first. Drop files anywhere
 * on it to import them. */
export function Overview({
  projectId,
  items,
  onOpen,
}: {
  projectId: number;
  items: ProjectItems;
  onOpen: (tab: Tab) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [dragging, setDragging] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["items", projectId] });
  const importFiles = useMutation({
    mutationFn: async (files: File[]) => {
      for (const file of files) await uploadFile(projectId, file);
    },
    onSettled: () => void refresh(),
  });
  const newChat = useMutation({
    mutationFn: () => apiPost<SessionSummary>(`/api/projects/${projectId}/sessions`, {}),
    onSuccess: (chat) => {
      void refresh();
      onOpen({ kind: "session", id: chat.id });
    },
  });
  const search = useQuery({
    queryKey: ["search", projectId, query.trim()],
    queryFn: () =>
      apiFetch<SearchHit[]>(
        `/api/projects/${projectId}/search?q=${encodeURIComponent(query.trim())}`,
      ),
    enabled: query.trim().length > 1,
  });

  const rows: Row[] = [
    ...items.sessions.map((s): Row => ({ kind: "session", modified: s.updated_at, session: s })),
    ...items.artifacts.map((a): Row => ({ kind: "artifact", modified: a.modified_at, artifact: a })),
  ].sort((a, b) => b.modified.localeCompare(a.modified));
  const counts: Record<Filter, number> = {
    all: rows.length,
    sessions: items.sessions.length,
    imported: items.artifacts.filter((a) => a.kind === "imported").length,
    produced: items.artifacts.filter((a) => a.kind === "generated").length,
  };
  const shown = rows.filter(
    (row) =>
      filter === "all" ||
      (filter === "sessions" && row.kind === "session") ||
      (row.kind === "artifact" &&
        ((filter === "imported" && row.artifact.kind === "imported") ||
          (filter === "produced" && row.artifact.kind === "generated"))),
  );
  const error = importFiles.error ?? newChat.error;

  return (
    <div
      className={`grid gap-4 p-4 ${dragging ? "outline-2 outline-dashed outline-accent" : ""}`}
      data-testid="overview"
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        const files = Array.from(e.dataTransfer.files);
        if (files.length) importFiles.mutate(files);
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" className="flex gap-1">
          {FILTERS.map((f) => (
            <button
              key={f}
              aria-pressed={filter === f}
              className={`rounded-full px-3 py-1 text-sm ${
                filter === f ? "bg-accent-soft font-semibold text-accent" : "text-muted"
              }`}
              onClick={() => setFilter(f)}
            >
              {t("overview.filterCount", { label: t(`overview.filters.${f}`), count: counts[f] })}
            </button>
          ))}
        </div>
        <input
          type="search"
          aria-label={t("overview.searchLabel")}
          placeholder={t("overview.search")}
          className="min-w-48 flex-1 rounded-md border border-line bg-surface px-3 py-1.5 text-sm"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button className="btn-secondary" onClick={() => picker.current?.click()}>
          {t("overview.import")}
        </button>
        <input
          ref={picker}
          type="file"
          multiple
          hidden
          data-testid="file-picker"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            if (files.length) importFiles.mutate(files);
            e.target.value = "";
          }}
        />
        <button className="btn-primary" disabled={newChat.isPending} onClick={() => newChat.mutate()}>
          {t("overview.newChat")}
        </button>
      </div>

      {error && (
        <ErrorNote message={error instanceof ApiError ? error.detail : t("common.loadFailed")} />
      )}
      {importFiles.isPending && (
        <p className="text-sm text-muted" role="status">
          {t("overview.importing")}
        </p>
      )}

      {query.trim().length > 1 ? (
        <SearchResults hits={search.data} onOpen={onOpen} />
      ) : (
        <ItemsTable rows={shown} onOpen={onOpen} />
      )}

      <p className="flex justify-between text-xs text-muted">
        <span>{t("overview.dropHint")}</span>
        <span>{t("overview.sorted")}</span>
      </p>
    </div>
  );
}

function ItemsTable({ rows, onOpen }: { rows: Row[]; onOpen: (tab: Tab) => void }) {
  const { t } = useTranslation();
  if (rows.length === 0) return <p className="text-sm text-muted">{t("overview.empty")}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase tracking-wider text-muted">
          <tr>
            {(["name", "kind", "details", "from", "modified"] as const).map((column) => (
              <th key={column} className="py-2 pr-4 font-semibold">
                {t(`overview.columns.${column}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) =>
            row.kind === "session" ? (
              <tr
                key={`s${row.session.id}`}
                className="cursor-pointer border-t border-line hover:bg-accent-soft"
                onClick={() => onOpen({ kind: "session", id: row.session.id })}
              >
                <td className="py-2 pr-4 font-medium">{row.session.title}</td>
                <td className="py-2 pr-4">{t("overview.chatKind")}</td>
                <td className="py-2 pr-4 text-muted">
                  {t("overview.messages", { count: row.session.messages })}
                </td>
                <td className="py-2 pr-4 text-muted">{t("overview.fromGone")}</td>
                <td className="py-2 text-muted">{formatAgo(row.modified)}</td>
              </tr>
            ) : (
              <tr
                key={`a${row.artifact.id}`}
                className="cursor-pointer border-t border-line hover:bg-accent-soft"
                onClick={() => onOpen({ kind: "artifact", id: row.artifact.id })}
              >
                <td className="py-2 pr-4 font-medium">{row.artifact.name}</td>
                <td className="py-2 pr-4">
                  {t(row.artifact.kind === "imported" ? "overview.importedKind" : "overview.generatedKind", {
                    type: fileType(row.artifact.name, row.artifact.mime),
                  })}
                </td>
                <td className="py-2 pr-4 text-muted">
                  {formatBytes(row.artifact.size)} ·{" "}
                  {row.artifact.text === "indexed"
                    ? t("overview.hasText")
                    : (row.artifact.text_note ?? "")}
                </td>
                <td className="py-2 pr-4 text-muted" data-testid={`from-${row.artifact.id}`}>
                  <From artifact={row.artifact} />
                </td>
                <td className="py-2 text-muted">{formatAgo(row.modified)}</td>
              </tr>
            ),
          )}
        </tbody>
      </table>
    </div>
  );
}

function From({ artifact }: { artifact: Artifact }) {
  const { t } = useTranslation();
  const { kind, title } = artifact.from;
  if (kind === "import") return <>{t("overview.fromImport")}</>;
  if (!title) return <>{t("overview.fromGone")}</>;
  return <>{t(kind === "session" ? "overview.fromSession" : "overview.fromArtifact", { title })}</>;
}

function SearchResults({ hits, onOpen }: { hits: SearchHit[] | undefined; onOpen: (tab: Tab) => void }) {
  const { t } = useTranslation();
  if (!hits) return <p className="text-sm text-muted">{t("common.loading")}</p>;
  if (hits.length === 0) return <p className="text-sm text-muted">{t("overview.noHits")}</p>;
  return (
    <ul className="grid gap-2">
      {hits.map((hit) => (
        <li key={`${hit.artifact_id}${hit.ref}`}>
          <button
            className="w-full rounded-md border border-line bg-surface px-3 py-2 text-left text-sm hover:bg-accent-soft"
            onClick={() => onOpen({ kind: "artifact", id: hit.artifact_id })}
          >
            <span className="font-medium">{hit.name}</span>{" "}
            <span className="font-mono text-xs text-muted">
              {hit.ref}
              {hit.heading ? ` · ${hit.heading}` : ""}
            </span>
            <span className="block text-muted">{hit.snippet}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
