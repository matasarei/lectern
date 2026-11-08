import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, apiRaw, ApiError } from "../api/client";
import type { Artifact, ProjectItems, Tab } from "../api/types";
import { ErrorNote, Row } from "../components";
import { fileType, formatBytes } from "../format";
import { Markdown } from "../lazy-markdown";
import { startOcr } from "./ocr-api";

/** Text shown in the page up to this size; bigger files download. */
export const TEXT_PREVIEW_LIMIT = 2 * 1024 * 1024;
/** A PDF is fetched whole into the page to be shown, so only up to this size. */
export const PDF_PREVIEW_LIMIT = 100 * 1024 * 1024;
/** An HTML file's one-use link lives 60 s on the server (`VIEW_TICKET_SECONDS`): renewed before. */
const VIEW_LINK_RENEW_MS = 45_000;
/** What an OCR session can read (the server decides by the name's extension too). */
const PAGE_SOURCES = [".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"];

export function isPageSource(artifact: Artifact): boolean {
  const name = artifact.name.toLowerCase();
  return PAGE_SOURCES.some((suffix) => name.endsWith(suffix));
}

/** A file in a tab, shown the way the server's preview kind says: Markdown as Markdown and
 * other text (code, an HTML file's source) as text, neither ever as HTML; images as images;
 * and the details with where the file came from. */
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
  const base = `/api/projects/${projectId}/artifacts/${artifact.id}`;
  const url = `${base}/content`;
  return (
    <div className="grid gap-4 p-4 lg:grid-cols-[1fr_18rem]">
      <div className="min-w-0">
        <Preview base={base} artifact={artifact} keepLines={publishedByOcr(artifact, items)} />
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

/** `base`: the file's API path; its content is `${base}/content`. */
function Preview({ base, artifact, keepLines }: { base: string; artifact: Artifact; keepLines: boolean }) {
  const { t } = useTranslation();
  const url = `${base}/content`;
  switch (artifact.preview) {
    case "markdown":
    case "text":
      if (artifact.size > TEXT_PREVIEW_LIMIT) return <p className="text-muted">{t("file.tooLarge")}</p>;
      return <TextPreview url={url} markdown={artifact.preview === "markdown"} keepLines={keepLines} />;
    case "html":
      return (
        <div className="grid gap-2">
          <OpenHtml base={base} />
          {artifact.size > TEXT_PREVIEW_LIMIT ? (
            <p className="text-muted">{t("file.tooLarge")}</p>
          ) : (
            <TextPreview url={url} markdown={false} keepLines={false} />
          )}
        </div>
      );
    case "image":
      return <ImagePreview url={url} name={artifact.name} />;
    case "pdf":
      if (artifact.size > PDF_PREVIEW_LIMIT) return <p className="text-muted">{t("file.tooLarge")}</p>;
      return <PdfPreview url={url} name={artifact.name} />;
    case "document":
      return <DocumentPreview url={`${base}/markdown`} />;
    case "legacy":
      return <p className="text-muted">{t("file.legacy")}</p>;
    default:
      return <p className="text-muted">{t("file.noPreview")}</p>;
  }
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

/** Opens an HTML file as a page in a new tab, where the server serves it sandboxed, with the
 * files it links to inline (`lectern.api.view`). A real link with rel="noopener": a tab opened
 * that way starts with empty session storage, where `window.open` would copy this tab's, token
 * and all. The one-use link is asked for before the click, renewed before it expires, when the
 * window comes back into focus, and as soon as it has been used. */
function OpenHtml({ base }: { base: string }) {
  const { t } = useTranslation();
  const link = useQuery({
    queryKey: ["file-view", base],
    queryFn: () => apiPost<{ url: string }>(`${base}/view`),
    gcTime: 0, // a used link is no good to the next one to show this file
    refetchInterval: VIEW_LINK_RENEW_MS,
    refetchOnWindowFocus: "always",
  });
  if (link.isError) {
    return <ErrorNote message={link.error instanceof ApiError ? link.error.detail : t("common.loadFailed")} />;
  }
  if (link.data === undefined) return <p className="text-muted">{t("common.loading")}</p>;
  return (
    <a
      className="btn-secondary w-fit"
      href={link.data.url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={() => void link.refetch()}
    >
      {t("file.openInTab")}
    </a>
  );
}

/** A DOCX or ODT, converted to Markdown by the server and shown like any Markdown file. */
function DocumentPreview({ url }: { url: string }) {
  const { t } = useTranslation();
  const doc = useQuery({
    queryKey: ["file-markdown", url],
    queryFn: () => apiFetch<{ markdown: string; cut: boolean }>(url),
  });
  if (doc.isError) {
    return <ErrorNote message={doc.error instanceof ApiError ? doc.error.detail : t("common.loadFailed")} />;
  }
  if (doc.data === undefined) return <p className="text-muted">{t("common.loading")}</p>;
  return (
    <div className="grid gap-2">
      {doc.data.cut && <p className="text-muted">{t("file.documentCut")}</p>}
      <div className="rounded-lg border border-line bg-surface p-4" data-testid="document-preview">
        <Markdown text={doc.data.markdown} />
      </div>
    </div>
  );
}

/** A PDF in the browser's own viewer. It needs the token too, so it's fetched and framed from
 * a blob URL, which Lectern types as a PDF itself (the CSP allows frames from blob: for this). */
function PdfPreview({ url, name }: { url: string; name: string }) {
  const { t } = useTranslation();
  const blob = useQuery({
    queryKey: ["file-pdf", url],
    queryFn: async () => new Blob([await (await apiRaw(url)).blob()], { type: "application/pdf" }),
  });
  const src = useObjectUrl(blob.data);
  if (blob.isError) return <ErrorNote message={t("common.loadFailed")} />;
  if (!src) return <p className="text-muted">{t("common.loading")}</p>;
  return (
    <iframe
      src={src}
      title={t("file.pdfTitle", { name })}
      className="h-[80vh] w-full rounded-lg border border-line"
    />
  );
}

/** An object URL for a blob while it's shown, revoked after. */
function useObjectUrl(blob: Blob | undefined): string | null {
  const src = useMemo(() => (blob ? URL.createObjectURL(blob) : null), [blob]);
  useEffect(() => {
    return () => {
      if (src) URL.revokeObjectURL(src);
    };
  }, [src]);
  return src;
}

/** Images need the token, so they're fetched and shown from a blob URL. */
export function ImagePreview({ url, name }: { url: string; name: string }) {
  const { t } = useTranslation();
  const blob = useQuery({
    queryKey: ["file-blob", url],
    queryFn: async () => (await apiRaw(url)).blob(),
  });
  const src = useObjectUrl(blob.data);
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
  const web = webSource(artifact);
  if (web) {
    return (
      <div className="grid gap-1 text-sm">
        <span className="text-muted">{t("file.from")}</span>
        <span>{t("file.fromWeb", { host: web.host })}</span>
        <a className="w-fit break-all text-accent underline" href={web.url} target="_blank" rel="noopener noreferrer">
          {web.url}
        </a>
      </div>
    );
  }
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

/** A page a chat opened from the web keeps its address as the file's note: the address,
 * and its host. Only an http(s) address counts, so nothing else becomes a link. */
export function webSource(artifact: Artifact): { url: string; host: string } | null {
  const { kind, note } = artifact.from;
  if (kind !== "import" || !note) return null;
  try {
    const url = new URL(note);
    return url.protocol === "https:" || url.protocol === "http:"
      ? { url: url.href, host: url.hostname }
      : null;
  } catch {
    return null; // a note that isn't an address
  }
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
