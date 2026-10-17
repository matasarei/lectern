import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiRaw, ApiError } from "../api/client";
import type { Artifact, ProjectItems, Tab } from "../api/types";
import { ErrorNote, Row } from "../components";
import { fileType, formatBytes } from "../format";
import { Markdown } from "../lazy-markdown";
import { startOcr } from "./ocr-api";

/** Text shown in the page up to this size; bigger files download. */
export const TEXT_PREVIEW_LIMIT = 2 * 1024 * 1024;
const TEXT_MIMES = ["text/plain", "text/markdown", "text/csv"];
const IMAGE_MIMES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
/** What an OCR session can read (the server decides by the name's extension too). */
const PAGE_SOURCES = [".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"];

export function isPageSource(artifact: Artifact): boolean {
  const name = artifact.name.toLowerCase();
  return PAGE_SOURCES.some((suffix) => name.endsWith(suffix));
}

/** A file in a tab: Markdown as Markdown and other text as text (neither ever as HTML),
 * images as images, and the details with where the file came from. */
export function FileTab({
  projectId,
  artifact,
  items,
  onOpen,
}: {
  projectId: number;
  artifact: Artifact | undefined;
  items: ProjectItems;
  onOpen: (tab: Tab) => void;
}) {
  const { t } = useTranslation();
  if (!artifact) return <p className="p-4 text-muted">{t("file.gone")}</p>;
  const url = `/api/projects/${projectId}/artifacts/${artifact.id}/content`;
  return (
    <div className="grid gap-4 p-4 lg:grid-cols-[1fr_18rem]">
      <div className="min-w-0">
        <Preview url={url} artifact={artifact} keepLines={publishedByOcr(artifact, items)} />
      </div>
      <aside className="grid content-start gap-2 rounded-lg border border-line bg-surface p-4">
        <h2 className="font-semibold break-all">{artifact.name}</h2>
        <Row label={t("file.type")} value={fileType(artifact.name, artifact.mime)} />
        <Row label={t("file.size")} value={formatBytes(artifact.size)} />
        <Row
          label={t("file.text")}
          value={artifact.text === "indexed" ? t("file.textIndexed") : (artifact.text_note ?? "—")}
        />
        <Origin artifact={artifact} items={items} onOpen={onOpen} />
        {isPageSource(artifact) && (
          <OpenAsOcr projectId={projectId} artifact={artifact} onOpen={onOpen} />
        )}
        <Download url={url} name={artifact.name} />
      </aside>
    </div>
  );
}

/** An OCR session's published Document (its document.md): its paragraphs' lines are the
 * page's own, kept as written for the chat to read, and shown as lines here. */
function publishedByOcr(artifact: Artifact, items: ProjectItems): boolean {
  if (artifact.mime !== "text/markdown" || artifact.from.kind !== "session") return false;
  return items.sessions.some((s) => s.id === artifact.from.id && s.kind === "ocr");
}

function Preview({ url, artifact, keepLines }: { url: string; artifact: Artifact; keepLines: boolean }) {
  const { t } = useTranslation();
  if (TEXT_MIMES.includes(artifact.mime)) {
    if (artifact.size > TEXT_PREVIEW_LIMIT) return <p className="text-muted">{t("file.tooLarge")}</p>;
    return <TextPreview url={url} markdown={artifact.mime === "text/markdown"} keepLines={keepLines} />;
  }
  if (IMAGE_MIMES.includes(artifact.mime)) return <ImagePreview url={url} name={artifact.name} />;
  return <p className="text-muted">{t("file.noPreview")}</p>;
}

function TextPreview({ url, markdown, keepLines }: { url: string; markdown: boolean; keepLines: boolean }) {
  const { t } = useTranslation();
  const text = useQuery({
    queryKey: ["file-text", url],
    queryFn: async () => (await apiRaw(url)).text(),
  });
  if (text.isError) {
    return <ErrorNote message={text.error instanceof ApiError ? text.error.detail : t("common.loadFailed")} />;
  }
  if (text.data === undefined) return <p className="text-muted">{t("common.loading")}</p>;
  if (markdown) {
    return (
      <div className="rounded-lg border border-line bg-surface p-4" data-testid="markdown-preview">
        <Markdown text={text.data} breaks={keepLines} />
      </div>
    );
  }
  return (
    <pre className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-line bg-surface p-4 text-sm">
      {text.data}
    </pre>
  );
}

/** Images need the token, so they're fetched and shown from a blob URL. */
export function ImagePreview({ url, name }: { url: string; name: string }) {
  const { t } = useTranslation();
  const blob = useQuery({
    queryKey: ["file-blob", url],
    queryFn: async () => (await apiRaw(url)).blob(),
  });
  const src = useMemo(() => (blob.data ? URL.createObjectURL(blob.data) : null), [blob.data]);
  useEffect(() => {
    return () => {
      if (src) URL.revokeObjectURL(src);
    };
  }, [src]);
  if (blob.isError) return <ErrorNote message={t("common.loadFailed")} />;
  if (!src) return <p className="text-muted">{t("common.loading")}</p>;
  return <img src={src} alt={t("file.imageAlt", { name })} className="max-h-[80vh] rounded-lg border border-line" />;
}

function Origin({
  artifact,
  items,
  onOpen,
}: {
  artifact: Artifact;
  items: ProjectItems;
  onOpen: (tab: Tab) => void;
}) {
  const { t } = useTranslation();
  const { kind, id, title } = artifact.from;
  const exists =
    id !== null &&
    (kind === "session"
      ? items.sessions.some((s) => s.id === id)
      : items.artifacts.some((a) => a.id === id));
  const label =
    kind === "import"
      ? t("overview.fromImport")
      : title
        ? t(kind === "session" ? "overview.fromSession" : "overview.fromArtifact", { title })
        : t("overview.fromGone");
  return (
    <div className="grid gap-1 text-sm">
      <span className="text-muted">{t("file.from")}</span>
      <span>{label}</span>
      {exists && title && id !== null && kind !== "import" && (
        <button
          className="w-fit text-left text-accent underline"
          onClick={() => onOpen({ kind: kind === "session" ? "session" : "artifact", id })}
        >
          {t("file.openSource", { title })}
        </button>
      )}
    </div>
  );
}

/** Downloads through a blob, because a plain link can't carry the token. */
export function Download({ url, name }: { url: string; name: string }) {
  const { t } = useTranslation();
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <button
        className="btn-secondary mt-2 w-fit"
        onClick={async () => {
          try {
            const blob = await (await apiRaw(url)).blob();
            const objectUrl = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = objectUrl;
            link.download = name;
            link.click();
            setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
            setError(null);
          } catch (e) {
            setError(e instanceof ApiError ? e.detail : t("common.loadFailed"));
          }
        }}
      >
        {t("file.download")}
      </button>
      {error && <ErrorNote message={error} />}
    </>
  );
}

/** Starts an OCR session on a PDF or image and opens it in a new tab. The server returns at
 * once; the pages are read in the background. */
export function OpenAsOcr({
  projectId,
  artifact,
  onOpen,
  compact = false,
}: {
  projectId: number;
  artifact: Artifact;
  onOpen: (tab: Tab) => void;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const start = useMutation({
    mutationFn: () => startOcr(projectId, artifact.id),
    onSuccess: (session) => {
      void queryClient.invalidateQueries({ queryKey: ["items", projectId] });
      onOpen({ kind: "session", id: session.id });
    },
  });
  return (
    <>
      <button
        className={compact ? "text-xs text-accent hover:underline" : "btn-primary mt-2 w-fit"}
        disabled={start.isPending}
        onClick={(e) => {
          e.stopPropagation(); // on an Overview row: don't also open the file
          start.mutate();
        }}
      >
        {t("ocr.open")}
      </button>
      {start.error && (
        <ErrorNote
          message={start.error instanceof ApiError ? start.error.detail : t("common.loadFailed")}
        />
      )}
    </>
  );
}
