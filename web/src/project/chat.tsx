import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, ApiError } from "../api/client";
import type { Artifact, Chat, ChatMessage, Tab, ToolCall } from "../api/types";
import { uploadFile } from "../api/upload";
import { ErrorNote } from "../components";
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
  onOpen: (tab: Tab) => void;
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

  const send = useMutation({
    mutationFn: () =>
      apiPost<{ message_id: number }>(`${base}/messages`, {
        text,
        attachments: attached.map((a) => a.id),
      }),
    onSuccess: () => {
      setText("");
      setAttached([]);
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

  const busy = live.running || chat.data.state !== "idle";
  const canSend = !busy && text.trim().length > 0 && !send.isPending && !attach.isPending;
  const error = send.error ?? stop.error ?? decide.error ?? attach.error;

  return (
    <div className="mx-auto grid h-full max-w-3xl grid-rows-[1fr_auto] gap-3 p-4">
      <div className="grid content-start gap-3 overflow-y-auto" data-testid="transcript">
        {chat.data.messages.length === 0 && !live.running && (
          <p className="py-16 text-center text-xl text-muted">{t("chat.empty")}</p>
        )}
        <Transcript messages={chat.data.messages} live={live} />
        <LiveReply live={live} onDecide={(id, approve) => decide.mutate({ id, approve })} />
        {live.error && <ErrorNote message={live.error} />}
        {live.stopped && <p className="text-sm text-muted">{t("chat.stopped")}</p>}
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
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (canSend) send.mutate();
            }
          }}
        />
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn-secondary" onClick={() => picker.current?.click()}>
            {attach.isPending ? t("chat.attaching") : t("chat.attach")}
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

function Transcript({ messages, live }: { messages: ChatMessage[]; live: LiveTurn }) {
  const { t } = useTranslation();
  const results = new Map(
    messages.filter((m) => m.role === "tool").map((m) => [m.tool_call_id, m] as const),
  );
  // The marker goes where the model's view starts: after the messages it no longer sees.
  const leftOut = live.context?.left_out ?? 0;
  return (
    <>
      {messages.map((message, index) => (
        <div key={message.id} className="grid gap-2">
          {leftOut > 0 && index === leftOut && (
            <p className="border-y border-dashed border-line py-1 text-center text-xs text-muted" role="note">
              {t("chat.leftOut", { count: leftOut })}
            </p>
          )}
          {message.role === "user" && <UserMessage message={message} />}
          {message.role === "assistant" && (
            <>
              {message.tool_calls?.map((call) => (
                <ToolLine key={call.id} call={call} result={results.get(call.id)} />
              ))}
              {message.content && (
                <p className="whitespace-pre-wrap text-sm leading-relaxed">{message.content}</p>
              )}
            </>
          )}
        </div>
      ))}
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
    <div className="ml-auto grid max-w-[85%] gap-1 rounded-xl rounded-br-sm bg-paper px-3 py-2 text-sm">
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

function ToolLine({ call, result }: { call: ToolCall; result: ChatMessage | undefined }) {
  const { t } = useTranslation();
  const name = result?.tool_name ?? call.function.name;
  const args = shortArgs(call.function.arguments);
  const first = result?.content.split("\n")[0] ?? t("chat.noResult");
  return (
    <details className="text-xs text-muted" data-testid="tool-line">
      <summary className="cursor-pointer font-mono">
        {t("chat.toolCall", { name, args })} → {first.slice(0, RESULT_SHOWN)}
      </summary>
      {result && <pre className="mt-1 whitespace-pre-wrap rounded bg-paper p-2">{result.content}</pre>}
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
        <p className="whitespace-pre-wrap text-sm leading-relaxed" data-testid="streaming">
          {live.text}
        </p>
      )}
      {live.running && !live.text && live.cards.every((c) => c.approved !== null) && (
        <p className="text-sm text-muted" role="status">
          {live.model ? t("chat.working") : t("chat.waitingModel")}
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
