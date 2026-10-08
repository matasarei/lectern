import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, ApiError } from "../api/client";
import type { OcrDocument, OcrModel, OcrPage, OcrSession, Tab } from "../api/types";
import { ErrorNote } from "../components";
import { useRuntime } from "../hooks/useRuntime";
import { HoldNote, ocrLoadingText } from "../status";
import { ImagePreview } from "./file";
import { cleanPageAgain, ocrPath, ocrProgress, useOcrDocument, useOcrEvents, type OcrLive } from "./ocr-api";
import { ReviewPage } from "./review";

type View = "split" | "scan" | "text" | "raw";
const VIEWS: View[] = ["split", "scan", "text", "raw"];

/** An OCR session in a tab: where its job stands, the pages on the left (30%), and the
 * selected page on the right (70%), its scan beside the text the model read. */
export function OcrTab({
  projectId,
  sessionId,
  onOpen,
  initialPage = 1,
}: {
  projectId: number;
  sessionId: number;
  onOpen: (tab: Tab) => void;
  initialPage?: number;
}) {
  const { t } = useTranslation();
  const base = ocrPath(projectId, sessionId);
  const session = useQuery({
    queryKey: ["ocr", projectId, sessionId],
    queryFn: () => apiFetch<OcrSession>(base),
  });
  const live = useOcrEvents(projectId, sessionId);
  const doc = useOcrDocument(projectId, sessionId, session.data?.document?.version ?? null);
  const [selected, setSelected] = useState(initialPage);

  if (session.isError) {
    return (
      <ErrorNote
        message={session.error instanceof ApiError ? session.error.detail : t("common.loadFailed")}
      />
    );
  }
  if (!session.data) return <p className="p-4 text-muted">{t("common.loading")}</p>;
  const page = session.data.pages.find((p) => p.number === selected) ?? session.data.pages[0];

  return (
    <div className="grid h-full grid-rows-[auto_minmax(0,1fr)]">
      <StatusStrip base={base} session={session.data} live={live} onOpen={onOpen} />
      {session.data.pages.length === 0 ? (
        <p className="p-4 text-muted">{t("ocr.noPagesYet")}</p>
      ) : (
        <div className="grid min-h-0 grid-cols-[30%_70%]">
          <PageList pages={session.data.pages} selected={page?.number} onSelect={setSelected} />
          {page && (
            <PageView
              key={page.number}
              projectId={projectId}
              sessionId={sessionId}
              base={base}
              page={page}
              doc={doc.data}
              onSelectPage={setSelected}
              jobRunning={session.data.status === "running" || session.data.status === "queued"}
            />
          )}
        </div>
      )}
    </div>
  );
}

function StatusStrip({
  base,
  session,
  live,
  onOpen,
}: {
  base: string;
  session: OcrSession;
  live: OcrLive;
  onOpen: (tab: Tab) => void;
}) {
  const { t } = useTranslation();
  const pause = useMutation({ mutationFn: () => apiPost(`${base}/pause`) });
  const resume = useMutation({ mutationFn: () => apiPost(`${base}/resume`) });
  const startNow = useMutation({ mutationFn: () => apiPost(`${base}/start-now`) });
  const runtime = useRuntime();
  // The runtime's hold is the live one (it changes when the chat's reply ends); the event's
  // is what the job saw when it began to wait.
  const hold = runtime.data?.hold;
  const { read, cleaned } = ocrProgress(session);
  const loading = ocrLoadingText(live.phase, runtime.data, t); // the gap between phases
  const document = session.document;
  const working = session.status === "running" || session.status === "queued";
  const error = pause.error ?? resume.error ?? startNow.error;
  return (
    <div className="grid gap-1 border-b border-line bg-surface px-4 py-2">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span className="font-semibold">{t(`ocr.status.${session.status}`)}</span>
        <span className={live.phase === "rasterise" ? "text-accent" : "text-muted"}>
          {t("ocr.phase.rasterise")}
        </span>
        <span className={live.phase === "recognise" ? "text-accent" : "text-muted"}>
          {t("ocr.phase.recognise", { done: read, total: session.pages_total })}
        </span>
        <span className={live.phase === "clean" ? "text-accent" : "text-muted"}>
          {t("ocr.phase.clean", { done: cleaned, total: session.pages_total })}
        </span>
        <span className={live.phase === "assemble" ? "text-accent" : "text-muted"}>
          {t("ocr.phase.assemble")}
        </span>
        {document && (
          <button
            className="text-accent hover:underline"
            onClick={() => onOpen({ kind: "document", id: session.id })}
          >
            {t("ocr.openDocument", { version: document.version })}
          </button>
        )}
        {document && document.open_doubts > 0 && (
          <span className="text-muted">{t("ocr.openQuestions", { count: document.open_doubts })}</span>
        )}
        {loading && (
          <span className="text-muted" role="status">
            {loading}
          </span>
        )}
        {live.waitingForChat && (
          <HoldNote
            seconds={hold ? hold.resume_in : live.resumeIn}
            since={hold ? runtime.dataUpdatedAt : live.waitingSince}
            onStartNow={() => startNow.mutate()}
            starting={startNow.isPending}
          />
        )}
        {session.status === "paused" && session.paused_reason && (
          <span className="text-muted">({session.paused_reason})</span>
        )}
        <span className="ml-auto" />
        {working ? (
          <button className="btn-secondary" disabled={pause.isPending} onClick={() => pause.mutate()}>
            {t("ocr.pause")}
          </button>
        ) : (
          session.status !== "done" && (
            <button className="btn-primary" disabled={resume.isPending} onClick={() => resume.mutate()}>
              {t("ocr.resume")}
            </button>
          )
        )}
      </div>
      {session.error && <ErrorNote message={session.error} />}
      {error && <ErrorNote message={error instanceof ApiError ? error.detail : t("common.loadFailed")} />}
    </div>
  );
}

function PageList({
  pages,
  selected,
  onSelect,
}: {
  pages: OcrPage[];
  selected: number | undefined;
  onSelect: (n: number) => void;
}) {
  const { t } = useTranslation();
  return (
    <ul className="overflow-y-auto border-r border-line" aria-label={t("ocr.pages")}>
      {pages.map((page) => (
        <li key={page.number}>
          <button
            className={`grid w-full gap-0.5 px-4 py-2 text-left text-sm hover:bg-accent-soft ${
              page.number === selected ? "bg-accent-soft" : ""
            }`}
            aria-current={page.number === selected}
            onClick={() => onSelect(page.number)}
          >
            <span className="font-medium">{t("ocr.pageTitle", { number: page.number })}</span>
            <span className="text-xs text-muted">
              {[
                t(`ocr.pageStatus.${page.status}`),
                page.status === "done" && t(`ocr.cleanStatus.${page.clean_status}`),
                page.looped && t("ocr.flags.looped"),
                page.cut_off && t("ocr.flags.cutOff"),
                page.handwritten && t("ocr.flags.handwritten"),
                page.note && t("ocr.flags.note"),
              ]
                .filter(Boolean)
                .join(" · ")}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function PageView({
  projectId,
  sessionId,
  base,
  page,
  doc,
  onSelectPage,
  jobRunning,
}: {
  projectId: number;
  sessionId: number;
  base: string;
  page: OcrPage;
  doc: OcrDocument | undefined;
  onSelectPage: (page: number) => void;
  jobRunning: boolean;
}) {
  const { t } = useTranslation();
  const [view, setView] = useState<View>("split");
  return (
    <div className="grid min-h-0 grid-rows-[auto_auto_minmax(0,1fr)] gap-3 overflow-y-auto p-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-semibold">{t("ocr.pageTitle", { number: page.number })}</span>
        {page.model_id && (
          <span className="text-muted">
            {t("ocr.readBy", { model: page.model_id, seconds: page.seconds ?? 0 })}
          </span>
        )}
        <span className="ml-auto" />
        <div role="group" aria-label={t("ocr.view")} className="flex gap-1">
          {VIEWS.map((v) => (
            <button
              key={v}
              className={v === view ? "btn-primary" : "btn-secondary"}
              aria-pressed={v === view}
              onClick={() => setView(v)}
            >
              {t(`ocr.views.${v}`)}
            </button>
          ))}
        </div>
      </div>
      {page.error && <ErrorNote message={page.error} />}
      <PageTools projectId={projectId} sessionId={sessionId} base={base} page={page} jobRunning={jobRunning} />
      <div className={`grid min-h-0 gap-3 ${view === "split" ? "grid-cols-2" : ""}`}>
        {(view === "split" || view === "scan") && (
          <ImagePreview url={`${base}/pages/${page.number}/image`} name={t("ocr.pageTitle", { number: page.number })} />
        )}
        {view !== "scan" &&
          (view !== "raw" && page.clean_status === "done" && doc && doc.pages.some((p) => p.n === page.number && !p.failed) ? (
            <ReviewPage projectId={projectId} sessionId={sessionId} doc={doc} page={page.number} onSelectPage={onSelectPage} />
          ) : (
            <PageText base={base} page={page} raw={view === "raw"} />
          ))}
      </div>
    </div>
  );
}

/** The raw OCR text: the Raw OCR view, and the page's text until it's cleaned and in the
 * Document (then the review shows it). */
function PageText({ base, page, raw }: { base: string; page: OcrPage; raw: boolean }) {
  const { t } = useTranslation();
  // Read again whenever the page's reading or cleanup changes.
  const text = useQuery({
    queryKey: ["ocr-text", base, page.number, page.status, page.model_id, page.seconds, page.clean_status],
    queryFn: () =>
      apiFetch<{ text: string | null }>(`${base}/pages/${page.number}/text`),
  });
  if (text.isError) {
    return <ErrorNote message={text.error instanceof ApiError ? text.error.detail : t("common.loadFailed")} />;
  }
  if (!text.data) return <p className="text-muted">{t("common.loading")}</p>;
  if (text.data.text === null) return <p className="text-muted">{t("ocr.notReadYet")}</p>;
  return (
    <div className="grid content-start gap-2">
      {!raw && page.clean_status === "failed" && (
        <ErrorNote message={t("ocr.cleanFailed", { error: page.clean_error ?? "" })} />
      )}
      {!raw && page.clean_status !== "failed" && <p className="text-xs text-muted">{t("ocr.notCleanedYet")}</p>}
      <pre className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-line bg-surface p-4 text-sm">
        {text.data.text}
      </pre>
    </div>
  );
}

/** The page's note (used when it's cleaned up, in M3), whether it's handwritten (its next
 * reading goes to the handwriting model), and reading it again with another model. */
function PageTools({
  projectId,
  sessionId,
  base,
  page,
  jobRunning,
}: {
  projectId: number;
  sessionId: number;
  base: string;
  page: OcrPage;
  jobRunning: boolean;
}) {
  const { t } = useTranslation();
  const [note, setNote] = useState(page.note);
  const [model, setModel] = useState("");
  const models = useQuery({
    queryKey: ["ocr-models", projectId],
    queryFn: () => apiFetch<OcrModel[]>(`${ocrPath(projectId)}/models`),
  });
  const pagePath = `${base}/pages/${page.number}`;
  const change = useMutation({
    mutationFn: (body: { note?: string; handwritten?: boolean }) =>
      apiFetch<OcrPage>(pagePath, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
  });
  const rerun = useMutation({
    mutationFn: () => apiPost(`${pagePath}/rerun`, { model_id: model || null }),
  });
  // A note changed on a page that's already cleaned: offer to clean it again with the note.
  const [offerClean, setOfferClean] = useState(false);
  const cleanAgain = useMutation({
    mutationFn: () => cleanPageAgain(projectId, sessionId, page.number),
    onSuccess: () => setOfferClean(false),
  });
  const error = change.error ?? rerun.error ?? cleanAgain.error;
  // Off while the page is being read, or is next in a running job anyway. A page not read yet
  // in a paused session can be read on its own, with the model chosen here.
  const reading = page.status === "recognising" || (page.status === "pending" && jobRunning);

  return (
    <div className="grid gap-2 rounded-lg border border-line bg-surface p-3 text-sm">
      <label className="grid gap-1">
        <span className="font-medium">{t("ocr.note.label")}</span>
        <textarea
          className="min-h-16 rounded border border-line bg-paper p-2"
          value={note}
          maxLength={5000}
          placeholder={t("ocr.note.placeholder")}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <div className="flex flex-wrap items-center gap-3">
        <button
          className="btn-secondary"
          disabled={note === page.note || change.isPending}
          onClick={() =>
            change.mutate({ note }, { onSuccess: () => setOfferClean(page.clean_status === "done") })
          }
        >
          {t("ocr.note.save")}
        </button>
        {note === page.note && page.note && <span className="text-muted">{t("ocr.note.saved")}</span>}
        {offerClean && (
          <button className="btn-primary" disabled={cleanAgain.isPending} onClick={() => cleanAgain.mutate()}>
            {t("ocr.note.cleanAgain")}
          </button>
        )}
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={page.handwritten}
            onChange={(e) => change.mutate({ handwritten: e.target.checked })}
          />
          {t("ocr.handwritten")}
        </label>
        <span className="ml-auto" />
        <select
          aria-label={t("ocr.rerun.model")}
          className="rounded border border-line bg-paper px-2 py-1"
          value={model}
          onChange={(e) => setModel(e.target.value)}
        >
          <option value="">{t("ocr.rerun.usual")}</option>
          {(models.data ?? []).map((m) => (
            <option key={m.id} value={m.id} disabled={!m.downloaded}>
              {m.downloaded ? m.title : t("ocr.rerun.notDownloaded", { title: m.title })}
            </option>
          ))}
        </select>
        <button className="btn-secondary" disabled={reading || rerun.isPending} onClick={() => rerun.mutate()}>
          {t("ocr.rerun.button")}
        </button>
      </div>
      {error && <ErrorNote message={error instanceof ApiError ? error.detail : t("common.loadFailed")} />}
    </div>
  );
}
