import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import type { DocBlock, DocDoubt, OcrDocument } from "../api/types";
import { ErrorNote } from "../components";
import {
  answerDoubt,
  documentKey,
  nextPageWithQuestions,
  openQuestions,
  pageBlocks,
  pageQuestions,
  reopenDoubt,
  suggestion,
} from "./ocr-api";

const MARK = /\{(d\d+(?:\.\d+)?)\}/g; // where a doubt sits in a block's text: {d3.1}

/** One page of the Document as the person reviews it: its blocks with the questions in
 * them, the card for the current question under its block, the page's questions, and the bar
 * that answers them from the keyboard. The keys (Enter, E, Tab) act only while focus is in
 * here, so Tab keeps its usual meaning everywhere else. */
export function ReviewPage({
  projectId,
  sessionId,
  doc,
  page,
  onSelectPage,
}: {
  projectId: number;
  sessionId: number;
  doc: OcrDocument;
  page: number;
  onSelectPage: (page: number) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const questions = pageQuestions(doc, page);
  const open = questions.filter((q) => q.status === "open");
  const [currentId, setCurrentId] = useState<string | null>(open[0]?.id ?? null);
  const [editing, setEditing] = useState(false);
  const area = useRef<HTMLDivElement>(null);
  // Answered meanwhile (in another tab, say): the page's first open question instead.
  const current = open.find((q) => q.id === currentId) ?? (currentId ? (open[0] ?? null) : null);

  useEffect(() => {
    if (open.length > 0) area.current?.focus(); // a page with questions: the keys work at once
    // Only when the page changes, not on every answer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  /** The open question after `id` on this page (wrapping), leaving `id` out. */
  const after = (id: string): string | null => {
    const rest = open.filter((q) => q.id !== id);
    if (rest.length === 0) return null;
    const index = open.findIndex((q) => q.id === id);
    return (rest.find((q) => open.indexOf(q) > index) ?? rest[0]).id;
  };

  const refresh = () => queryClient.invalidateQueries({ queryKey: documentKey(projectId, sessionId) });
  const answer = useMutation({
    mutationFn: (a: { doubt: DocDoubt; value: string; action: "accept" | "edit" }) =>
      answerDoubt(projectId, sessionId, a.doubt.id, a.value, a.action),
    onSuccess: (_, a) => {
      setEditing(false);
      setCurrentId(after(a.doubt.id));
      area.current?.focus();
      void refresh();
    },
  });
  const reopen = useMutation({
    mutationFn: (doubt: DocDoubt) => reopenDoubt(projectId, sessionId, doubt.id),
    onSuccess: (_, doubt) => {
      setCurrentId(doubt.id);
      void refresh();
    },
  });

  const accept = () => current && answer.mutate({ doubt: current, value: suggestion(current), action: "accept" });
  const skip = () => current && setCurrentId(after(current.id) ?? current.id);
  const nextPage = nextPageWithQuestions(doc, page);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    if (editing || target.closest("input, textarea, select") || !current || answer.isPending) return;
    if (e.key === "Enter") {
      e.preventDefault();
      accept();
    } else if (e.key === "e" || e.key === "E") {
      e.preventDefault();
      setEditing(true);
    } else if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      skip();
    }
  };

  const card = current && (
    <QuestionCard
      doubt={current}
      index={open.indexOf(current) + 1}
      total={open.length}
      editing={editing}
      busy={answer.isPending}
      onAccept={accept}
      onEdit={() => setEditing(true)}
      onSkip={skip}
      onSave={(value) => answer.mutate({ doubt: current, value, action: "edit" })}
      onCancel={() => {
        setEditing(false);
        area.current?.focus();
      }}
    />
  );
  const error = answer.error ?? reopen.error;

  return (
    <div
      ref={area}
      tabIndex={-1}
      className="grid content-start gap-3 outline-none"
      data-testid="review"
      onKeyDown={onKeyDown}
    >
      {questions.length > 0 && (
        <QuestionPanel
          questions={questions}
          current={current?.id ?? null}
          onPick={(q) => {
            setEditing(false);
            setCurrentId(q.id);
            area.current?.focus();
          }}
          onReopen={(q) => reopen.mutate(q)}
        />
      )}
      <div className="grid gap-3 rounded-lg border border-line bg-surface p-4 text-sm">
        {pageBlocks(doc, page).map((block) => (
          <div key={block.id} className="grid gap-2">
            <BlockView block={block} current={current?.id ?? null} onPick={(id) => setCurrentId(id)} />
            {current && block.doubts.some((d) => d.id === current.id) && card}
          </div>
        ))}
      </div>
      {error && <ErrorNote message={error instanceof ApiError ? error.detail : t("common.loadFailed")} />}
      <div
        className="sticky bottom-0 flex flex-wrap items-center gap-3 rounded-lg border border-line bg-surface px-3 py-2 text-sm"
        data-testid="question-bar"
      >
        <span className="font-medium">
          {t("review.bar", { page, count: open.length, total: openQuestions(doc).length })}
        </span>
        {current && (
          <span className="text-muted">
            {t("review.accept", { value: suggestion(current) })} <kbd>{t("review.keys.accept")}</kbd> ·{" "}
            {t("review.edit")} <kbd>{t("review.keys.edit")}</kbd> · {t("review.skip")}{" "}
            <kbd>{t("review.keys.skip")}</kbd>
          </span>
        )}
        <span className="ml-auto" />
        {nextPage !== null && nextPage !== page && (
          <button className="text-accent hover:underline" onClick={() => onSelectPage(nextPage)}>
            {t("review.nextPage")}
          </button>
        )}
      </div>
    </div>
  );
}

function QuestionPanel({
  questions,
  current,
  onPick,
  onReopen,
}: {
  questions: DocDoubt[];
  current: string | null;
  onPick: (q: DocDoubt) => void;
  onReopen: (q: DocDoubt) => void;
}) {
  const { t } = useTranslation();
  const ordered = [...questions.filter((q) => q.status === "open"), ...questions.filter((q) => q.status !== "open")];
  return (
    <ul className="flex flex-wrap gap-2 text-xs" aria-label={t("review.questions")}>
      {ordered.map((q) => (
        <li key={q.id} className="flex items-center gap-1 rounded-full border border-line px-2 py-0.5">
          {q.status === "open" ? (
            <button aria-current={q.id === current} className={q.id === current ? "font-semibold text-accent" : ""} onClick={() => onPick(q)}>
              {q.raw}
            </button>
          ) : (
            <>
              <span className="text-muted">
                {q.raw} → {q.value}
              </span>
              <button className="text-accent hover:underline" onClick={() => onReopen(q)}>
                {t("review.reopen")}
              </button>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

function QuestionCard({
  doubt,
  index,
  total,
  editing,
  busy,
  onAccept,
  onEdit,
  onSkip,
  onSave,
  onCancel,
}: {
  doubt: DocDoubt;
  index: number;
  total: number;
  editing: boolean;
  busy: boolean;
  onAccept: () => void;
  onEdit: () => void;
  onSkip: () => void;
  onSave: (value: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState(suggestion(doubt));
  return (
    <div className="grid gap-2 rounded-lg border border-accent bg-paper p-3" data-testid="question-card">
      <p className="text-xs text-muted">
        {t("review.cardTitle", { index, total })} · {t(`review.source.${doubt.source}`)}
      </p>
      <p className="font-mono">
        {doubt.raw} → {suggestion(doubt)}
      </p>
      {doubt.candidates.length > 1 && (
        <p className="text-xs text-muted">{t("review.candidates", { list: doubt.candidates.join(" · ") })}</p>
      )}
      {doubt.reason && <p className="text-muted">{doubt.reason}</p>}
      {editing ? (
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (value.trim()) onSave(value.trim());
          }}
        >
          <input
            autoFocus
            aria-label={t("review.editLabel")}
            className="flex-1 rounded border border-line bg-surface px-2 py-1"
            value={value}
            maxLength={200}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") onCancel();
            }}
          />
          <button className="btn-primary" type="submit" disabled={busy || !value.trim()}>
            {t("review.save")}
          </button>
        </form>
      ) : (
        <div className="flex gap-2">
          <button className="btn-primary" disabled={busy} onClick={onAccept}>
            {t("review.accept", { value: suggestion(doubt) })}
          </button>
          <button className="btn-secondary" onClick={onEdit}>
            {t("review.edit")}
          </button>
          <button className="btn-secondary" onClick={onSkip}>
            {t("review.skip")}
          </button>
        </div>
      )}
    </div>
  );
}

/** A block of the Document. Text is always React text, never HTML: a page can hold "<b>". */
export function BlockView({
  block,
  current = null,
  onPick,
}: {
  block: DocBlock;
  current?: string | null;
  onPick?: (id: string) => void;
}) {
  const { t } = useTranslation();
  const show = (text: string) => withDoubts(text, block.doubts, current, onPick);
  switch (block.type) {
    case "heading":
      return <p className="text-base font-semibold">{show(block.text)}</p>;
    case "table":
      return (
        <table className="w-fit border-collapse text-sm">
          <tbody>
            {(block.rows ?? []).map((row, r) => (
              <tr key={r}>
                {row.map((cell, c) => (
                  <td key={c} className={`border border-line px-2 py-1 ${r === 0 ? "font-semibold" : ""}`}>
                    {show(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      );
    case "list":
      return (
        <ul className="list-disc pl-5">
          {block.text
            .split("\n")
            .filter((item) => item.trim())
            .map((item, i) => (
              <li key={i}>{show(item)}</li>
            ))}
        </ul>
      );
    case "figure":
      return <p className="text-muted">{t("review.figure", { label: block.label ?? "image" })}</p>;
    default:
      return <p className="whitespace-pre-wrap">{show(block.text)}</p>;
  }
}

function withDoubts(
  text: string,
  doubts: DocDoubt[],
  current: string | null,
  onPick?: (id: string) => void,
): ReactNode[] {
  const byId = new Map(doubts.map((d) => [d.id, d]));
  return text.split(MARK).map((part, i) => {
    if (i % 2 === 0) return part; // text between marks
    const doubt = byId.get(part);
    if (!doubt) return `{${part}}`;
    if (doubt.status === "resolved") {
      return (
        <span key={i} className="underline decoration-dotted decoration-accent" title={doubt.raw}>
          {doubt.value}
        </span>
      );
    }
    return (
      <mark
        key={i}
        data-doubt={doubt.id}
        className={`rounded px-0.5 ${doubt.id === current ? "bg-accent text-paper" : "bg-accent-soft"} ${onPick ? "cursor-pointer" : ""}`}
        onClick={onPick ? () => onPick(doubt.id) : undefined}
      >
        {doubt.raw}
      </mark>
    );
  });
}
