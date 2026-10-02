import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, ApiError } from "../api/client";
import type { Artifact, ProjectItems, SearchHit, SessionSummary, Tab } from "../api/types";
import { uploadFile } from "../api/upload";
import { ErrorNote } from "../components";
import { fileType, formatAgo, formatBytes } from "../format";
import { isPageSource, OpenAsOcr } from "./file";

type Filter = "all" | "sessions" | "imported" | "produced";
const FILTERS: Filter[] = ["all", "sessions", "imported", "produced"];

type Row =
  | { kind: "session"; modified: string; session: SessionSummary }
  | { kind: "artifact"; modified: string; artifact: Artifact }
  | { kind: "folder"; modified: string; folder: string; files: Artifact[]; ocr: SessionSummary | undefined };

const DOCUMENT_MD = "document.md";

/** A file's top folder ("Lease 1998" for "Lease 1998/pages/x.md"), or null when it has none. */
function topFolder(artifact: Artifact): string | null {
  const slash = artifact.name.indexOf("/");
  return slash > 0 ? artifact.name.slice(0, slash) : null;
}

/** Files in a folder go under one row for their top folder; an OCR session's output folder
 * says so. The folder's Modified is its newest file's. */
function groupByFolder(artifacts: Artifact[], sessions: SessionSummary[]): Row[] {
  const loose: Row[] = [];
  const folders = new Map<string, Artifact[]>();
  for (const artifact of artifacts) {
    const folder = topFolder(artifact);
    if (folder === null) loose.push({ kind: "artifact", modified: artifact.modified_at, artifact });
    else folders.set(folder, [...(folders.get(folder) ?? []), artifact]);
  }
  const grouped = [...folders].map(([folder, files]): Row => ({
    kind: "folder",
    folder,
    files: [...files].sort((a, b) => b.modified_at.localeCompare(a.modified_at)),
    modified: files.reduce((newest, f) => (f.modified_at > newest ? f.modified_at : newest), ""),
    ocr: sessions.find((s) => s.ocr?.folder === folder),
  }));
  return [...loose, ...grouped];
}

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

  const counts: Record<Filter, number> = {
    all: items.sessions.length + items.artifacts.length,
    sessions: items.sessions.length,
    imported: items.artifacts.filter((a) => a.kind === "imported").length,
    produced: items.artifacts.filter((a) => a.kind === "generated").length,
  };
  const artifactsShown = items.artifacts.filter(
    (a) =>
      filter === "all" ||
      (filter === "imported" && a.kind === "imported") ||
      (filter === "produced" && a.kind === "generated"),
  );
  const shown: Row[] = [
    ...(filter === "all" || filter === "sessions"
      ? items.sessions.map((s): Row => ({ kind: "session", modified: s.updated_at, session: s }))
      : []),
    ...groupByFolder(artifactsShown, items.sessions),
  ].sort((a, b) => b.modified.localeCompare(a.modified));
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
        <ItemsTable projectId={projectId} rows={shown} onOpen={onOpen} />
      )}

      <p className="flex justify-between text-xs text-muted">
        <span>{t("overview.dropHint")}</span>
        <span>{t("overview.sorted")}</span>
      </p>
    </div>
  );
}

function ItemsTable({
  projectId,
  rows,
  onOpen,
}: {
  projectId: number;
  rows: Row[];
  onOpen: (tab: Tab) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState<Set<string>>(new Set());
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
          {rows.map((row) => {
            if (row.kind === "session") {
              return (
                <tr
                  key={`s${row.session.id}`}
                  className="cursor-pointer border-t border-line hover:bg-accent-soft"
                  onClick={() => onOpen({ kind: "session", id: row.session.id })}
                >
                  <td className="py-2 pr-4 font-medium">{row.session.title}</td>
                  <td className="py-2 pr-4">
                    {t(row.session.ocr ? "overview.ocrKind" : "overview.chatKind")}
                  </td>
                  <td className="py-2 pr-4 text-muted">
                    {row.session.ocr
                      ? t("overview.ocrDetails", {
                          count: row.session.ocr.pages,
                          status: t(`ocr.status.${row.session.ocr.status}`),
                        })
                      : t("overview.messages", { count: row.session.messages })}
                  </td>
                  <td className="py-2 pr-4 text-muted">{t("overview.fromGone")}</td>
                  <td className="py-2 text-muted">{formatAgo(row.modified)}</td>
                </tr>
              );
            }
            if (row.kind === "artifact") {
              return <FileRow key={`a${row.artifact.id}`} projectId={projectId} artifact={row.artifact} onOpen={onOpen} />;
            }
            const expanded = open.has(row.folder);
            return [
              <FolderRow
                key={`f${row.folder}`}
                row={row}
                expanded={expanded}
                onToggle={() =>
                  setOpen((prev) => {
                    const next = new Set(prev);
                    if (next.has(row.folder)) next.delete(row.folder);
                    else next.add(row.folder);
                    return next;
                  })
                }
              />,
              ...(expanded
                ? row.files.map((file) =>
                    row.ocr?.ocr && file.name === `${row.folder}/${DOCUMENT_MD}` ? (
                      <DocumentRow key={`a${file.id}`} artifact={file} session={row.ocr} onOpen={onOpen} />
                    ) : (
                      <FileRow key={`a${file.id}`} projectId={projectId} artifact={file} onOpen={onOpen} nested />
                    ),
                  )
                : []),
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}

function FileRow({
  projectId,
  artifact,
  onOpen,
  nested = false,
}: {
  projectId: number;
  artifact: Artifact;
  onOpen: (tab: Tab) => void;
  nested?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <tr
      className="cursor-pointer border-t border-line hover:bg-accent-soft"
      onClick={() => onOpen({ kind: "artifact", id: artifact.id })}
    >
      <td className={`py-2 pr-4 font-medium ${nested ? "pl-6" : ""}`}>
        {nested ? artifact.name.slice(artifact.name.indexOf("/") + 1) : artifact.name}
      </td>
      <td className="py-2 pr-4">
        {t(artifact.kind === "imported" ? "overview.importedKind" : "overview.generatedKind", {
          type: fileType(artifact.name, artifact.mime),
        })}
      </td>
      <td className="py-2 pr-4 text-muted">
        {formatBytes(artifact.size)} ·{" "}
        {artifact.text === "indexed" ? t("overview.hasText") : (artifact.text_note ?? "")}
        {isPageSource(artifact) && (
          <>
            {" · "}
            <OpenAsOcr projectId={projectId} artifact={artifact} onOpen={onOpen} compact />
          </>
        )}
      </td>
      <td className="py-2 pr-4 text-muted" data-testid={`from-${artifact.id}`}>
        <From artifact={artifact} />
      </td>
      <td className="py-2 text-muted">{formatAgo(artifact.modified_at)}</td>
    </tr>
  );
}

/** A folder: one row, its files under it when expanded. An OCR session's output folder
 * reads "OCR output · folder" with its Document's pages and image regions. */
function FolderRow({
  row,
  expanded,
  onToggle,
}: {
  row: Extract<Row, { kind: "folder" }>;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const ocr = row.ocr?.ocr;
  return (
    <tr className="cursor-pointer border-t border-line hover:bg-accent-soft" aria-expanded={expanded} onClick={onToggle}>
      <td className="py-2 pr-4 font-medium">
        {expanded ? "▾ " : "▸ "}
        {t("overview.folderName", { name: row.folder })}
      </td>
      <td className="py-2 pr-4">{t(ocr ? "overview.ocrFolderKind" : "overview.folderKind")}</td>
      <td className="py-2 pr-4 text-muted">
        {ocr && ocr.document_version > 0
          ? t("overview.ocrFolderDetails", {
              pages: t("overview.pages", { count: ocr.pages }),
              figures: t("overview.figures", { count: ocr.figures }),
            })
          : t("overview.files", { count: row.files.length })}
      </td>
      <td className="py-2 pr-4 text-muted">
        {row.ocr ? t("overview.madeByOcr", { title: row.ocr.title }) : t("overview.fromGone")}
      </td>
      <td className="py-2 text-muted">{formatAgo(row.modified)}</td>
    </tr>
  );
}

/** An OCR session's Document, inside its folder: opens the Document's own tab. */
function DocumentRow({
  artifact,
  session,
  onOpen,
}: {
  artifact: Artifact;
  session: SessionSummary;
  onOpen: (tab: Tab) => void;
}) {
  const { t } = useTranslation();
  const ocr = session.ocr!;
  return (
    <tr
      className="cursor-pointer border-t border-line hover:bg-accent-soft"
      onClick={() => onOpen({ kind: "document", id: session.id })}
    >
      <td className="py-2 pr-4 pl-6 font-medium">{ocr.folder}</td>
      <td className="py-2 pr-4">{t("overview.documentKind")}</td>
      <td className="py-2 pr-4 text-muted">
        {t("overview.documentDetails", { version: ocr.document_version, open: ocr.open_doubts })}
      </td>
      <td className="py-2 pr-4 text-muted">{t("overview.insideFolder", { name: ocr.folder })}</td>
      <td className="py-2 text-muted">{formatAgo(artifact.modified_at)}</td>
    </tr>
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
