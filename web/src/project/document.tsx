import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiFetch, ApiError } from "../api/client";
import type { OcrSession, ProjectItems, Tab } from "../api/types";
import { ErrorNote, Row } from "../components";
import { Download } from "./file";
import { ExportButtons } from "./export-buttons";
import { doubtPage, ocrPath, openQuestions, pageBlocks, useOcrDocument } from "./ocr-api";
import { BlockView } from "./review";

/** An OCR session's Document in its own tab: the whole text with its open questions marked,
 * and beside it where it came from. A question opens the OCR tab at its page, where it's
 * answered. */
export function DocumentTab({
  projectId,
  sessionId,
  items,
  onOpen,
}: {
  projectId: number;
  sessionId: number;
  items: ProjectItems;
  onOpen: (tab: Tab) => void;
}) {
  const { t } = useTranslation();
  const session = useQuery({
    queryKey: ["ocr", projectId, sessionId],
    queryFn: () => apiFetch<OcrSession>(ocrPath(projectId, sessionId)),
  });
  const doc = useOcrDocument(projectId, sessionId, session.data?.document?.version ?? null);
  const error = session.error ?? doc.error;
  if (error) return <ErrorNote message={error instanceof ApiError ? error.detail : t("common.loadFailed")} />;
  if (session.data && !session.data.document) return <p className="p-4 text-muted">{t("document.none")}</p>;
  if (!session.data || !doc.data) return <p className="p-4 text-muted">{t("common.loading")}</p>;

  const document = doc.data;
  const source = items.artifacts.find((a) => a.id === session.data.source_artifact_id);
  const notes = document.pages.filter((p) => p.note.trim());
  const open = openQuestions(document);
  const toQuestion = (id: string) => {
    const doubt = open.find((d) => d.id === id);
    if (doubt) onOpen({ kind: "session", id: sessionId, page: doubtPage(doubt) });
  };
  const artifactId = session.data.document?.artifact_id;

  return (
    <div className="grid gap-4 p-4 lg:grid-cols-[1fr_18rem]">
      <article className="grid min-w-0 content-start gap-3 rounded-lg border border-line bg-surface p-6 text-sm">
        {document.pages.map((page) =>
          page.failed ? (
            <p key={page.n} className="text-muted">
              {t("document.pageFailed", { page: page.n, error: page.failed })}
            </p>
          ) : (
            pageBlocks(document, page.n)
              .filter((b) => b.page === page.n) // a block running on is shown once, where it starts
              .map((block) => <BlockView key={block.id} block={block} onPick={toQuestion} />)
          ),
        )}
      </article>
      <aside className="grid content-start gap-2 rounded-lg border border-line bg-surface p-4">
        <h2 className="font-semibold break-all">{document.title}</h2>
        <Row label={t("document.version")} value={document.version} />
        <Row label={t("document.pages")} value={document.source.pages} />
        <Row label={t("document.openQuestions")} value={open.length} />
        <div className="grid gap-1 text-sm">
          <span className="text-muted">{t("file.from")}</span>
          {source ? (
            <button className="w-fit text-left text-accent underline" onClick={() => onOpen({ kind: "artifact", id: source.id })}>
              {source.name}
            </button>
          ) : (
            <span>{document.source.artifact}</span>
          )}
          <button className="w-fit text-left text-accent underline" onClick={() => onOpen({ kind: "session", id: sessionId })}>
            {t("document.openSession", { title: session.data.title })}
          </button>
        </div>
        {notes.length > 0 && (
          <div className="grid gap-1 text-sm">
            <span className="text-muted">{t("document.notes")}</span>
            {notes.map((p) => (
              <p key={p.n}>{t("document.note", { page: p.n, note: p.note })}</p>
            ))}
          </div>
        )}
        {artifactId !== undefined && (
          <Download url={`/api/projects/${projectId}/artifacts/${artifactId}/content`} name="document.md" />
        )}
        {artifactId !== undefined && <ExportButtons projectId={projectId} artifactId={artifactId} onOpen={onOpen} />}
      </aside>
    </div>
  );
}
