import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import type { DocBlock, DocDoubt, OcrDocument } from "../api/types";
import { ErrorNote } from "../components";
import {
  answerDoubt,
  answerOthers,
  coversBlock,
  documentKey,
  doubtPage,
  nextPageWithQuestions,
  openQuestions,
  pageBlocks,
  pageQuestions,
  reopenDoubt,
  sameOpenQuestions,
  suggestion,
  suggests,
  type Answer,
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
  asked = null,
  onSelectPage,
  onBack,
}: {
  projectId: number;
  sessionId: number;
  doc: OcrDocument;
  page: number;
  asked?: { id: string } | null; // a question asked for from outside: each new object selects it
  onSelectPage: (page: number) => void;
  onBack?: () => void; // to the chat that started the session, once every question is answered
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const questions = pageQuestions(doc, page);
  const open = questions.filter((q) => q.status === "open");
  // Every question the Document had is answered (one with none never had any to answer).
  const answeredAll = openQuestions(doc).length === 0 && doc.blocks.some((b) => b.doubts.length > 0);
  const notCleaned = doc.pages.filter((p) => p.failed).map((p) => p.n);
  const [currentId, setCurrentId] = useState<string | null>(asked?.id ?? open[0]?.id ?? null);
  const [editing, setEditing] = useState(false);
  const [alone, setAlone] = useState<string | null>(null); // Later on the document's only question
  const area = useRef<HTMLDivElement>(null);
  const [seenAsked, setSeenAsked] = useState(asked);
  if (asked !== seenAsked) {
    setSeenAsked(asked);
    setCurrentId(asked?.id ?? null);
    setEditing(false);
  }
  // The question just answered, for offering its answer to the others on the same text; on
  // another page the offer would be about something no longer in view.
  const [answeredId, setAnsweredId] = useState<string | null>(null);
  const [seenPage, setSeenPage] = useState(page);
  if (page !== seenPage) {
    setSeenPage(page);
    setAnsweredId(null);
  }
  // Answered meanwhile (in another tab, say): the page's first open question instead.
  const current = open.find((q) => q.id === currentId) ?? (currentId ? (open[0] ?? null) : null);

  useEffect(() => {
    if (open.length > 0) area.current?.focus(); // a page with questions: the keys work at once
    // Only when the page changes or a question is asked for, not on every answer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, asked]);

  /** The open question after `id` on this page (wrapping), leaving `id` out. */
  const after = (id: string): string | null => {
    const rest = open.filter((q) => q.id !== id);
    if (rest.length === 0) return null;
    const index = open.findIndex((q) => q.id === id);
    return (rest.find((q) => open.indexOf(q) > index) ?? rest[0]).id;
  };

  const refresh = () => queryClient.invalidateQueries({ queryKey: documentKey(projectId, sessionId) });
  const answer = useMutation({
    mutationFn: (a: { doubt: DocDoubt; answer: Answer }) => answerDoubt(projectId, sessionId, a.doubt.id, a.answer),
    onSuccess: (_, a) => {
      setEditing(false);
      setCurrentId(after(a.doubt.id));
      // A removal is about one block, not a reading to give the others.
      setAnsweredId(a.answer.action === "remove" ? null : a.doubt.id);
      area.current?.focus();
      void refresh();
    },
  });
  const reopen = useMutation({
    mutationFn: (doubt: DocDoubt) => reopenDoubt(projectId, sessionId, doubt.id),
    onSuccess: (_, doubt) => {
      setCurrentId(doubt.id);
      setAnsweredId(null);
      void refresh();
    },
  });
  const repeat = useMutation({
    mutationFn: (doubt: DocDoubt) => answerOthers(projectId, sessionId, doubt.id),
    onSuccess: () => {
      setAnsweredId(null);
      area.current?.focus();
      void refresh();
    },
  });
  // Offered once the Document says it's answered, so never from one that hasn't caught up.
  const answered = doc.blocks
    .flatMap((b) => b.doubts)
    .find((d) => d.id === answeredId && d.status === "resolved" && d.value !== "");
  const others = answered ? sameOpenQuestions(doc, answered) : [];
  const answerAll = () => answered && others.length > 0 && !repeat.isPending && repeat.mutate(answered);

  const reply = (a: Answer) => current && answer.mutate({ doubt: current, answer: a });
  const accept = (value: string) => reply({ action: "accept", value });
  const keep = () => reply({ action: "keep" });
  // Enter takes the suggestion, or the reading itself when nothing else is suggested.
  const primary = () => current && (suggests(current) ? accept(suggestion(current)) : keep());
  const nextPage = nextPageWithQuestions(doc, page);
  // The page's next open question; after its last, the next page's (it opens on its first).
  const later = () => {
    if (!current) return;
    const next = after(current.id);
    if (next) setCurrentId(next);
    else if (nextPage !== null && nextPage !== page) onSelectPage(nextPage);
    else setAlone(current.id);
  };
  const block = current && pageBlocks(doc, page).find((b) => b.doubts.some((d) => d.id === current.id));

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    // On a control (the card's buttons, a question chip, the edit box) the keys keep their
    // usual meaning: Enter presses it, Tab moves on. They answer only from the review itself.
    if (editing || target.closest("input, textarea, select, button") || answer.isPending) return;
    // Before the check for a current question: after a page's last answer there's none.
    if ((e.key === "a" || e.key === "A") && others.length > 0) {
      e.preventDefault();
      answerAll();
      return;
    }
    if (!current) return;
    if (e.key === "Enter") {
      e.preventDefault();
      primary();
    } else if (e.key === "k" || e.key === "K") {
      e.preventDefault();
      keep();
    } else if (e.key === "e" || e.key === "E") {
      e.preventDefault();
      setEditing(true);
    } else if (e.key === "Tab" && !e.shiftKey) {
      e.preventDefault();
      later();
    }
  };

  const card = current && (
    <QuestionCard
      doubt={current}
      index={open.indexOf(current) + 1}
      total={open.length}
      editing={editing}
      busy={answer.isPending}
      removable={!!block && coversBlock(block, current.id)}
      alone={alone === current.id}
      onAccept={accept}
      onKeep={keep}
      onRemove={() => reply({ action: "remove" })}
      onEdit={() => setEditing(true)}
      onLater={later}
      onSave={(value) => reply({ action: "edit", value })}
      onCancel={() => {
        setEditing(false);
        area.current?.focus();
      }}
    />
  );
  const error = answer.error ?? reopen.error ?? repeat.error;
  const otherPages = [...new Set(others.map(doubtPage))].sort((a, b) => a - b);

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
      {/* Answers whose words aren't in the page's text any more (it was cleaned again):
          kept, and said, rather than lost without a word. */}
      {doc.unplaced
        .filter((u) => u.page === page)
        .map((u) => (
          <p key={u.doubt_id} className="text-xs text-muted" data-testid="unplaced">
            {u.action === "remove"
              ? t("review.unplacedRemoval", { raw: u.raw })
              : t("review.unplaced", { raw: u.raw, value: u.value })}
          </p>
        ))}
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
        {/* The answer just given, offered to the same reading elsewhere: in the bar, so it's in
            view however far the page is scrolled. Ignored, it changes nothing. */}
        {answered && others.length > 0 && (
          <div className="flex basis-full flex-wrap items-center gap-2" data-testid="same-answer">
            <span>
              {answered.value === answered.raw
                ? t("review.sameKept", { raw: answered.raw, pages: otherPages.join(", "), count: otherPages.length })
                : t("review.same", {
                    raw: answered.raw,
                    value: answered.value,
                    pages: otherPages.join(", "),
                    count: otherPages.length,
                  })}
            </span>
            <button className="btn-secondary" disabled={repeat.isPending} onClick={answerAll}>
              {t("review.sameAll", { count: others.length })} <kbd>{t("review.keys.same")}</kbd>
            </button>
          </div>
        )}
        {/* The last one answered: say so, and the way back to the chat that started it,
            where whatever it queued (an export) goes on. */}
        {answeredAll ? (
          <>
            <span className="font-medium" data-testid="all-answered">
              {/* Not final yet while a page isn't cleaned: said here too, not only above. */}
              {notCleaned.length > 0
                ? t("review.allAnsweredBut", {
                    what: t("ocrSteps.notCleaned", { count: notCleaned.length, pages: notCleaned.join(", ") }),
                  })
                : t("review.allAnswered")}
            </span>
            {onBack && (
              <button className="text-accent hover:underline" onClick={onBack}>
                {t("review.backToChat")}
              </button>
            )}
          </>
        ) : (
          <span className="font-medium">
            {t("review.bar", { page, count: open.length, total: openQuestions(doc).length })}
          </span>
        )}
        {current && (
          <span className="text-muted">
            {suggests(current) && (
              <>
                {t("review.accept", { value: suggestion(current) })} <kbd>{t("review.keys.accept")}</kbd> ·{" "}
              </>
            )}
            {t("review.keep", { value: current.raw })}{" "}
            {!suggests(current) && <kbd>{t("review.keys.accept")}</kbd>} <kbd>{t("review.keys.keep")}</kbd> ·{" "}
            {t("review.edit")} <kbd>{t("review.keys.edit")}</kbd> · {t("review.later")} <kbd>{t("review.keys.later")}</kbd>
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
                {q.value === ""
                  ? t("review.removed", { raw: q.raw })
                  : q.value === q.raw
                    ? t("review.kept", { raw: q.raw })
                    : `${q.raw} → ${q.value}`}
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
  removable,
  alone,
  onAccept,
  onKeep,
  onRemove,
  onEdit,
  onLater,
  onSave,
  onCancel,
}: {
  doubt: DocDoubt;
  index: number;
  total: number;
  editing: boolean;
  busy: boolean;
  removable: boolean; // the question is all of its block
  alone: boolean; // Later was pressed on the document's only open question
  onAccept: (value: string) => void;
  onKeep: () => void;
  onRemove: () => void;
  onEdit: () => void;
  onLater: () => void;
  onSave: (value: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState(suggestion(doubt));
  const offered = suggests(doubt);
  // The other candidates: the suggestion has its own Accept.
  const choices = [...new Set(doubt.candidates.filter((c) => c.trim() && c !== doubt.raw && c !== suggestion(doubt)))];
  return (
    <div className="grid gap-2 rounded-lg border border-accent bg-paper p-3" data-testid="question-card">
      <p className="text-xs text-muted">
        {t("review.cardTitle", { index, total })} · {t(`review.source.${doubt.source}`)}
      </p>
      <p className="font-mono">{offered ? `${doubt.raw} → ${suggestion(doubt)}` : doubt.raw}</p>
      {choices.length > 0 && (
        <p className="flex flex-wrap items-center gap-2 text-xs text-muted">
          {t("review.couldBe")}
          {choices.map((c) => (
            <button
              key={c}
              className="rounded border border-line px-1.5 font-mono hover:border-accent"
              disabled={busy}
              aria-label={t("review.accept", { value: c })}
              onClick={() => onAccept(c)}
            >
              {c}
            </button>
          ))}
        </p>
      )}
      {alone && <p className="text-muted">{t("review.alone")}</p>}
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
        <div className="flex flex-wrap gap-2">
          {/* No Accept when the suggestion is the reading itself: it would change nothing. */}
          {offered && (
            <button className="btn-primary" disabled={busy} onClick={() => onAccept(suggestion(doubt))}>
              {t("review.accept", { value: suggestion(doubt) })}
            </button>
          )}
          <button className={offered ? "btn-secondary" : "btn-primary"} disabled={busy} onClick={onKeep}>
            {t("review.keep", { value: doubt.raw })}
          </button>
          <button className="btn-secondary" onClick={onEdit}>
            {t("review.edit")}
          </button>
          {removable && (
            <button className="btn-secondary" disabled={busy} onClick={onRemove}>
              {t("review.remove")}
            </button>
          )}
          <button className="btn-secondary" onClick={onLater}>
            {t("review.later")}
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
    if (doubt.status === "resolved" && doubt.value === "") {
      return (
        <s key={i} className="text-muted">
          {doubt.raw}
        </s>
      ); // removed: struck out here, left out of the Document's text and its exports
    }
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
