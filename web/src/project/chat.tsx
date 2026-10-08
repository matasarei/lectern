import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, apiRaw, ApiError } from "../api/client";
import type { Artifact, Chat, ChatMessage, ProjectItems, Tab, ToolCall } from "../api/types";
import { uploadFile } from "../api/upload";
import { ErrorNote } from "../components";
import { FileList, listKey, matchingFiles, optionId } from "./file-picker";
import { formatTokens } from "../format";
import { useRuntime } from "../hooks/useRuntime";
import { Markdown } from "../markdown";
import { OcrLine } from "./ocr-line";
import { modelWaitText } from "../status";
import { type LiveTurn, useSessionEvents } from "../hooks/useSessionEvents";

const ARGS_SHOWN = 80;
const RESULT_SHOWN = 120;

/** A chat: the transcript, the reply streaming in, tool calls as grey lines, cards that ask
 * before something is deleted, and the message box. */
export function ChatTab({
  projectId,
  sessionId,
  onOpen,
}: {
  projectId: number;
  sessionId: number;
  onOpen: (tab: Tab, focus?: boolean) => void; // focus false: add the tab without showing it
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const base = `/api/projects/${projectId}/sessions/${sessionId}`;
  const chat = useQuery({
    queryKey: ["chat", projectId, sessionId],
    queryFn: () => apiFetch<Chat>(base),
  });
  const live = useSessionEvents(projectId, sessionId, onOpen);

  const [text, setText] = useState("");
  const [attached, setAttached] = useState<Artifact[]>([]);
  const picker = useRef<HTMLInputElement>(null);
  // Choosing a file already in the project: from the button, or "@name" in the box (`start`:
  // where the "@" is, so the "@name" text goes when a file is chosen).
  const [pick, setPick] = useState<{ query: string; start: number | null } | null>(null);
  const [active, setActive] = useState(0);
  const listId = useId();
  const items = useQuery({
    queryKey: ["items", projectId],
    queryFn: () => apiFetch<ProjectItems>(`/api/projects/${projectId}/items`),
    enabled: pick !== null,
  });

  const send = useMutation({
    mutationFn: () =>
      apiPost<{ message_id: number }>(`${base}/messages`, {
        text,
        attachments: attached.map((a) => a.id),
      }),
    onSuccess: () => {
      setText("");
      setAttached([]);
      setPick(null); // a list left open belongs to the text that just went
      void queryClient.invalidateQueries({ queryKey: ["chat", projectId, sessionId] });
    },
  });
  const stop = useMutation({ mutationFn: () => apiPost(`${base}/stop`) });
  const decide = useMutation({
    mutationFn: ({ id, approve }: { id: number; approve: boolean }) =>
      apiPost(`${base}/confirmations/${id}`, { approve }),
  });
  const attach = useMutation({
    mutationFn: async (files: File[]) => {
      const added: Artifact[] = [];
      for (const file of files) added.push(await uploadFile(projectId, file));
      return added;
    },
    onSuccess: (added) => {
      setAttached((prev) => [...prev, ...added]);
      void queryClient.invalidateQueries({ queryKey: ["items", projectId] });
    },
  });

  if (chat.isError) {
    return (
      <ErrorNote message={chat.error instanceof ApiError ? chat.error.detail : t("common.loadFailed")} />
    );
  }
  if (!chat.data) return <p className="p-4 text-muted">{t("common.loading")}</p>;

  const choices = pick
    ? matchingFiles(items.data?.artifacts ?? [], pick.query, attached.map((a) => a.id))
    : [];
  const choose = (file: Artifact) => {
    setAttached((prev) => [...prev, file]);
    if (pick?.start != null) {
      setText(text.slice(0, pick.start) + text.slice(pick.start + 1 + pick.query.length));
    }
    setPick(null);
  };
  // The field the list belongs to names it and its active option, for screen readers.
  const listAria = (open: boolean) =>
    open
      ? {
          "aria-controls": listId,
          "aria-expanded": true,
          "aria-autocomplete": "list" as const,
          "aria-activedescendant": choices[active] ? optionId(listId, choices[active]) : undefined,
        }
      : {};
  const onListKey = (key: string) =>
    pick !== null && listKey(key, choices, active, setActive, choose, () => setPick(null));

  const busy = live.running || chat.data.state !== "idle";
  const canSend = !busy && text.trim().length > 0 && !send.isPending && !attach.isPending;
  const error = send.error ?? stop.error ?? decide.error ?? attach.error;

  return (
    <div className="mx-auto grid h-full max-h-full max-w-3xl grid-rows-[minmax(0,1fr)_auto] gap-3 p-4">
      <div className="grid content-start gap-3 overflow-y-auto" data-testid="transcript">
        {chat.data.messages.length === 0 && !live.running && (
          <p className="py-16 text-center text-xl text-muted">{t("chat.empty")}</p>
        )}
        <Transcript
          projectId={projectId}
          messages={chat.data.messages}
          summarised={chat.data.summary.messages}
          live={live}
          followups={chat.data.followups}
          onOpen={onOpen}
          liveReply={
            hasLiveReply(live) && (
              <LiveReply live={live} onDecide={(id, approve) => decide.mutate({ id, approve })} />
            )
          }
        />
        {live.error && <ErrorNote message={live.error} />}
        {live.stopped && <p className="text-sm text-muted">{t("chat.stopped")}</p>}
        <CompactionNote state={live.compaction} />
        {error && (
          <ErrorNote message={error instanceof ApiError ? error.detail : t("common.loadFailed")} />
        )}
      </div>

      <form
        className="grid gap-2 rounded-lg border border-line bg-surface p-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSend) send.mutate();
        }}
      >
        <PinnedStrip projectId={projectId} sessionId={sessionId} pinned={chat.data.pinned} />
        {chat.data.followups.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-xs" data-testid="followups">
            <span className="font-semibold text-muted">{t("chat.followups")}</span>
            {chat.data.followups.map((f) => (
              <span key={f.id} className="rounded-full bg-paper px-3 py-0.5">
                {f.label}
              </span>
            ))}
          </div>
        )}
        {attached.length > 0 && (
          <ul className="flex flex-wrap gap-2">
            {attached.map((a) => (
              <li key={a.id} className="flex items-center gap-1 rounded-full bg-paper px-3 py-0.5 text-xs">
                {a.name}
                <button
                  type="button"
                  aria-label={t("chat.removeAttachment", { name: a.name })}
                  onClick={() => setAttached((prev) => prev.filter((p) => p.id !== a.id))}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
        <textarea
          aria-label={t("chat.inputLabel")}
          placeholder={t("chat.placeholder")}
          rows={2}
          className="w-full resize-none bg-transparent text-sm outline-none"
          value={text}
          {...listAria(pick?.start != null)}
          onChange={(e) => {
            setText(e.target.value);
            // "@" at the start or after a space opens the list; what follows filters it.
            const caret = e.target.selectionStart ?? e.target.value.length;
            const at = /(^|\s)@([^\s@]*)$/.exec(e.target.value.slice(0, caret));
            if (at) {
              setPick({ query: at[2], start: caret - at[2].length - 1 });
              setActive(0);
            } else if (pick?.start != null) setPick(null);
          }}
          onKeyDown={(e) => {
            if (pick?.start != null && onListKey(e.key)) {
              e.preventDefault();
              return;
            }
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (canSend) send.mutate();
            }
          }}
        />
        {pick && (
          <div className="grid gap-1">
            {pick.start === null && (
              <input
                type="search"
                autoFocus
                aria-label={t("chat.pickerSearch")}
                placeholder={t("chat.pickerSearch")}
                className="rounded-md border border-line bg-paper px-2 py-1 text-sm"
                value={pick.query}
                {...listAria(true)}
                onChange={(e) => {
                  setPick({ query: e.target.value, start: null });
                  setActive(0);
                }}
                onKeyDown={(e) => {
                  if (onListKey(e.key)) e.preventDefault();
                }}
              />
            )}
            <FileList
              id={listId}
              files={choices}
              active={active}
              onPick={choose}
              loading={items.isPending}
            />
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn-secondary" onClick={() => picker.current?.click()}>
            {attach.isPending ? t("chat.attaching") : t("chat.attach")}
          </button>
          <button
            type="button"
            className="btn-secondary"
            aria-label={t("chat.mention")}
            title={t("chat.mention")}
            onClick={() => {
              setPick(pick?.start === null ? null : { query: "", start: null });
              setActive(0);
            }}
          >
            @
          </button>
          <input
            ref={picker}
            type="file"
            multiple
            hidden
            data-testid="attach-picker"
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []);
              if (files.length) attach.mutate(files);
              e.target.value = "";
            }}
          />
          <span className="text-xs text-muted" data-testid="model-line">
            {live.model &&
              t("chat.model", { model: live.model.model_id, ctx: live.model.ctx.toLocaleString("en") })}
          </span>
          {live.usage && <UsageLine usage={live.usage} />}
          <span className="ml-auto" />
          {busy ? (
            <button type="button" className="btn-secondary" onClick={() => stop.mutate()}>
              {t("chat.stop")}
            </button>
          ) : (
            <button type="submit" className="btn-primary" disabled={!canSend}>
              {t("chat.send")}
            </button>
          )}
        </div>
      </form>
    </div>
  );
}

type Group = { kind: "user" | "reply"; start: number; messages: ChatMessage[] };

/** The transcript as the person's messages and the model's replies: a reply is the run of
 * assistant and tool messages up to the next message from the person. A run also breaks at
 * each of `breaks` (message indexes where a marker goes), so the marker sits between cards. */
export function groupReplies(messages: ChatMessage[], breaks: number[]): Group[] {
  const groups: Group[] = [];
  messages.forEach((message, index) => {
    const kind = message.role === "user" ? "user" : "reply";
    const open = groups.at(-1);
    if (kind === "reply" && open?.kind === "reply" && !breaks.includes(index)) {
      open.messages.push(message);
    } else {
      groups.push({ kind, start: index, messages: [message] });
    }
  });
  return groups;
}

/** Whether a reply has anything of its own to show (tool results only show under their call). */
function shows(group: Group): boolean {
  return group.messages.some((m) => m.role === "assistant" && (m.content || m.tool_calls?.length));
}

/** One reply from the model: its tool lines, its text, and, while it runs, the live part. */
function Reply({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="mr-auto grid w-fit max-w-full min-w-0 gap-2 rounded-xl rounded-bl-sm border border-line bg-surface px-3 py-2"
      data-testid="reply"
    >
      {children}
    </div>
  );
}

function Transcript({
  projectId,
  messages,
  summarised,
  live,
  liveReply,
  followups,
  onOpen,
}: {
  projectId: number;
  messages: ChatMessage[];
  summarised: number; // how many of the first messages the summary stands in for
  live: LiveTurn;
  liveReply: React.ReactNode; // the reply as it streams in; it joins the last reply's card
  followups: Chat["followups"];
  onOpen: (tab: Tab) => void;
}) {
  const { t } = useTranslation();
  const results = new Map(
    messages.filter((m) => m.role === "tool").map((m) => [m.tool_call_id, m] as const),
  );
  // The markers go where the model's view starts: after the messages covered by the summary,
  // and after any it had to leave out of the last call (counted from the summary on).
  const leftOut = live.context?.left_out ?? 0;
  const viewStarts = summarised + leftOut;
  const groups = groupReplies(messages, [summarised, viewStarts]);
  const last = groups.at(-1);
  // The live reply joins the last card only when that card answers this turn's message: until
  // the transcript is read again, the last card is still the previous turn's. A follow-up's
  // turn has no message of its own and continues the last reply.
  const liveJoins =
    last?.kind === "reply" &&
    (live.messageId === null || groups.at(-2)?.messages[0].id === live.messageId);
  return (
    <>
      {groups.map((group) => (
        <div key={group.messages[0].id} className="grid gap-2">
          {summarised > 0 && group.start === summarised && (
            <p className="border-y border-dashed border-line py-1 text-center text-xs text-muted" role="note">
              {t("chat.summarised", { count: summarised })}
            </p>
          )}
          {leftOut > 0 && group.start === viewStarts && (
            <p className="border-y border-dashed border-line py-1 text-center text-xs text-muted" role="note">
              {t("chat.leftOut", { count: leftOut })}
            </p>
          )}
          {group.kind === "user" ? (
            <UserMessage message={group.messages[0]} />
          ) : (
            (shows(group) || (group === last && liveJoins && liveReply)) && (
              <Reply>
                {group.messages
                  .filter((message) => message.role === "assistant") // results show under their call
                  .map((message) => (
                    <Fragment key={message.id}>
                      {message.tool_calls?.map((call) => (
                        <ToolLine
                          key={call.id}
                          projectId={projectId}
                          call={call}
                          result={results.get(call.id)}
                          progress={live.progress[call.id]}
                          followups={followups}
                          onOpen={onOpen}
                        />
                      ))}
                      {message.content && <Markdown text={message.content} />}
                    </Fragment>
                  ))}
                {group === last && liveJoins && liveReply}
              </Reply>
            )
          )}
        </div>
      ))}
      {!liveJoins && liveReply && <Reply>{liveReply}</Reply>}
      {live.context && live.context.released > 0 && (
        <p className="text-xs text-muted" role="note">
          {t("chat.released", { count: live.context.released })}
        </p>
      )}
      {live.context && live.context.stubbed_files.length > 0 && (
        <p className="text-xs text-muted" role="note">
          {t("chat.tooLong", { names: live.context.stubbed_files.join(", ") })}
        </p>
      )}
    </>
  );
}

function UserMessage({ message }: { message: ChatMessage }) {
  const { t } = useTranslation();
  return (
    <div
      className="ml-auto grid max-w-[85%] gap-1 rounded-xl rounded-br-sm bg-accent-soft px-3 py-2 text-sm"
      data-testid="user-message"
    >
      <p className="whitespace-pre-wrap">{message.content}</p>
      {message.attachments.length > 0 && (
        <p className="text-xs text-muted">
          {t("chat.attachments", {
            names: message.attachments.map((a) => a.name ?? "?").join(", "),
          })}
        </p>
      )}
    </div>
  );
}

function ToolLine({
  projectId,
  call,
  result,
  progress,
  followups,
  onOpen,
}: {
  projectId: number;
  call: ToolCall;
  result: ChatMessage | undefined;
  progress: LiveTurn["progress"][string] | undefined;
  followups: Chat["followups"];
  onOpen: (tab: Tab) => void;
}) {
  const { t } = useTranslation();
  const name = result?.tool_name ?? call.function.name;
  const args = shortArgs(call.function.arguments);
  const first = result?.content.split("\n")[0] ?? t("chat.noResult");
  const script = result && name === "code.write" ? writtenScript(result.content) : null;
  // The session ocr.start began: its progress shows under the line
  const ocr = name === "ocr.start" ? result?.refs.find((r) => r.kind === "session") : undefined;
  return (
    <div className="grid gap-1">
      <details className="text-xs text-muted" data-testid="tool-line">
        <summary className="cursor-pointer font-mono">
          {t("chat.toolCall", { name, args })} → {first.slice(0, RESULT_SHOWN)}
        </summary>
        {result && <pre className="mt-1 whitespace-pre-wrap rounded bg-paper p-2">{result.content}</pre>}
      </details>
      {!result && progress && <Progress progress={progress} />}
      {script && <ScriptCode projectId={projectId} path={script} />}
      {ocr && (
        <OcrLine
          projectId={projectId}
          sessionId={ocr.id}
          then={followups.filter((f) => f.ocr_session_id === ocr.id).map((f) => f.label)}
          onOpen={onOpen}
        />
      )}
    </div>
  );
}

function Progress({ progress }: { progress: LiveTurn["progress"][string] }) {
  const { t } = useTranslation();
  const { label, done, total, line, seconds } = progress;
  if (done !== undefined && total) {
    const pct = Math.min(100, Math.floor((done / total) * 100));
    return (
      <div className="grid gap-1 text-xs text-muted" data-testid="progress">
        <span>{t("chat.progressBar", { label, pct })}</span>
        <progress className="h-1.5 w-full" max={100} value={pct} />
      </div>
    );
  }
  return (
    <p className="text-xs text-muted" data-testid="progress">
      {seconds !== undefined ? t("chat.progressSeconds", { label, seconds }) : label}
      {line ? <span className="block truncate font-mono">{line}</span> : null}
    </p>
  );
}

/** "Wrote scripts/penalty.py (3 lines) …" → "scripts/penalty.py". */
export function writtenScript(text: string): string | null {
  return /^Wrote (scripts\/.+?\.py) \(/.exec(text)?.[1] ?? null;
}

/** The script code.write saved, shown in the chat so the person sees what computed the answer. */
function ScriptCode({ projectId, path }: { projectId: number; path: string }) {
  const { t } = useTranslation();
  const items = useQuery({
    queryKey: ["items", projectId],
    queryFn: () => apiFetch<ProjectItems>(`/api/projects/${projectId}/items`),
  });
  const id = items.data?.artifacts.find((a) => a.name === path)?.id;
  const code = useQuery({
    queryKey: ["file-text", projectId, id],
    queryFn: async () => (await apiRaw(`/api/projects/${projectId}/artifacts/${id}/content`)).text(),
    enabled: id !== undefined,
  });
  return (
    <details className="text-xs" data-testid="script">
      <summary className="cursor-pointer text-muted">
        {t("chat.script")} · <span className="font-mono">{path}</span>
      </summary>
      <pre className="mt-1 overflow-x-auto rounded bg-paper p-2 font-mono">
        {code.data ?? t("chat.scriptLoading")}
      </pre>
    </details>
  );
}

/** `{"query": "lease"}` → `"lease"`; several arguments → `query="lease", kind="files"`. */
export function shortArgs(json: string): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(json || "{}");
  } catch {
    return json.slice(0, ARGS_SHOWN);
  }
  if (!parsed || typeof parsed !== "object") return "";
  const entries = Object.entries(parsed as Record<string, unknown>);
  const text =
    entries.length === 1
      ? JSON.stringify(entries[0][1])
      : entries.map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
  return text.length > ARGS_SHOWN ? `${text.slice(0, ARGS_SHOWN)}…` : text;
}

/** Whether the live reply has anything to show: it runs, asks, streams, or reports its speed. */
function hasLiveReply(live: LiveTurn): boolean {
  return live.running || live.cards.length > 0 || live.text !== "" || Boolean(live.stats?.tps);
}

function LiveReply({
  live,
  onDecide,
}: {
  live: LiveTurn;
  onDecide: (confirmationId: number, approve: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      {live.cards.map((card) => (
        <div
          key={card.confirmation_id}
          className="grid gap-2 rounded-lg border border-accent bg-accent-soft p-3 text-sm"
          role="alertdialog"
          aria-label={t("chat.confirmTitle")}
        >
          <p className="font-semibold">{card.text}</p>
          {card.approved === null ? (
            <div className="flex gap-2">
              <button className="btn-primary" onClick={() => onDecide(card.confirmation_id, true)}>
                {t("chat.allow")}
              </button>
              <button className="btn-secondary" onClick={() => onDecide(card.confirmation_id, false)}>
                {t("chat.deny")}
              </button>
            </div>
          ) : (
            <p className="text-muted">{card.approved ? t("chat.allowed") : t("chat.denied")}</p>
          )}
        </div>
      ))}
      {live.text && (
        <div data-testid="streaming">
          <Markdown text={live.text} />
        </div>
      )}
      {live.running && live.followup && (
        <p className="text-xs text-muted" data-testid="followup-turn">
          {t("chat.followupRunning", { label: live.followup })}
        </p>
      )}
      {live.running && !live.text && live.cards.every((c) => c.approved !== null) && (
        <p className="text-sm text-muted" role="status">
          {live.model ? t("chat.working") : <ModelWait />}
        </p>
      )}
      {!live.running && live.stats?.tps && (
        <p className="text-xs text-muted">
          {t("chat.stats", { tps: live.stats.tps, ttft: live.stats.ttft ?? "?" })}
        </p>
      )}
    </>
  );
}

function PinnedStrip({
  projectId,
  sessionId,
  pinned,
}: {
  projectId: number;
  sessionId: number;
  pinned: Chat["pinned"];
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const unpin = useMutation({
    mutationFn: (id: string) =>
      apiFetch(`/api/projects/${projectId}/sessions/${sessionId}/pins/${id}`, { method: "DELETE" }),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["chat", projectId, sessionId] }),
  });
  if (pinned.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" data-testid="pinned">
      <span className="font-semibold text-muted">{t("chat.pinned")}</span>
      {pinned.map((pin) => (
        <span key={pin.id} className="flex items-center gap-1 rounded-full bg-accent-soft px-3 py-0.5">
          <span className="text-muted">{t(`chat.pinnedKind.${pin.kind}`)}</span> {pin.text}
          <button
            type="button"
            aria-label={t("chat.unpin", { text: pin.text })}
            disabled={unpin.isPending}
            onClick={() => unpin.mutate(pin.id)}
          >
            ×
          </button>
        </span>
      ))}
    </div>
  );
}

const USAGE_PARTS = ["pinned", "files", "summary", "turns"] as const;

function UsageLine({ usage }: { usage: NonNullable<LiveTurn["usage"]> }) {
  const { t } = useTranslation();
  const used = Object.values(usage.parts).reduce((sum, n) => sum + n, 0);
  const parts = USAGE_PARTS.filter((name) => (usage.parts[name] ?? 0) > 0).map((name) =>
    t("chat.usagePart", { name: t(`chat.usageParts.${name}`), tokens: formatTokens(usage.parts[name]) }),
  );
  return (
    <span className="text-xs text-muted" data-testid="usage">
      {[t("chat.usage", { used: formatTokens(used), budget: formatTokens(usage.budget) }), ...parts].join(" · ")}
    </span>
  );
}

function CompactionNote({ state }: { state: LiveTurn["compaction"] }) {
  const { t } = useTranslation();
  if (state === null) return null;
  const text =
    state === "working"
      ? t("chat.compacting")
      : "failed" in state
        ? t("chat.compactionFailed", { detail: state.failed })
        : t("chat.compacted", { count: state.summarised });
  return (
    <p className="text-xs text-muted" role="status" data-testid="compaction">
      {text}
    </p>
  );
}

/** Before the model answers: what it's waiting for (the engine, a load, an OCR page). */
function ModelWait() {
  const { t } = useTranslation();
  const runtime = useRuntime();
  return <>{modelWaitText(runtime.data, t)}</>;
}
