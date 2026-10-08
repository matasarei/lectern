import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, ApiError, apiPost } from "../api/client";
import type { Project, ProjectItems, SessionSummary, Tab, Tabs } from "../api/types";
import { ErrorNote } from "../components";
import { formatMemory } from "../format";
import { useRuntime } from "../hooks/useRuntime";
import { ChatTab } from "../project/chat";
import { DocumentTab } from "../project/document";
import { FileTab } from "../project/file";
import { OcrTab } from "../project/ocr";
import { Overview } from "../project/overview";
import { closeTab, openTab, sameTab, showOverview } from "../project/tabs";

/** A project: its name, the tab bar (Overview first, then whatever is open), and the
 * active tab. The open tabs are saved to the project on every change. */
export function ProjectPage() {
  const { projectId } = useParams({ from: "/projects/$projectId" });
  const id = Number(projectId);
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const project = useQuery({
    queryKey: ["project", id],
    queryFn: () => apiFetch<Project>(`/api/projects/${id}`),
  });
  const items = useQuery({
    queryKey: ["items", id],
    queryFn: () => apiFetch<ProjectItems>(`/api/projects/${id}/items`),
  });
  const saved = useQuery({
    queryKey: ["tabs", id],
    queryFn: () => apiFetch<Tabs>(`/api/projects/${id}/tabs`),
    staleTime: Infinity,
  });
  // What the person has changed since the page loaded; until then, the saved tabs.
  const [changed, setChanged] = useState<Tabs | null>(null);
  const tabs = changed ?? saved.data ?? null;

  const save = useMutation({
    mutationFn: (next: Tabs) =>
      apiFetch<Tabs>(`/api/projects/${id}/tabs`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(next),
      }),
  });
  const update = (next: Tabs) => {
    setChanged(next);
    queryClient.setQueryData(["tabs", id], next);
    save.mutate(next);
  };
  const open = (tab: Tab) => tabs && update(openTab(tabs, tab));
  const newChat = useMutation({
    mutationFn: () => apiPost<SessionSummary>(`/api/projects/${id}/sessions`, {}),
    onSuccess: (chat) => {
      void queryClient.invalidateQueries({ queryKey: ["items", id] });
      open({ kind: "session", id: chat.id });
    },
  });

  // A tab the chat just opened can name something the page hasn't loaded yet (a new OCR
  // session, an exported file): read the project's items again, once per tab, rather than
  // show it as gone or as the wrong kind of tab.
  const asked = useRef(new Set<string>());
  const [checked, setChecked] = useState<string[]>([]); // tabs whose items were read again
  const active = tabs?.active ?? null;
  const activeKey = active ? `${active.kind}-${active.id}` : "";
  const activeKnown = active === null || items.data === undefined || known(items.data, active);
  useEffect(() => {
    if (active === null || activeKnown || asked.current.has(activeKey)) return;
    asked.current.add(activeKey);
    void queryClient
      .invalidateQueries({ queryKey: ["items", id] })
      .finally(() => setChecked((prev) => [...prev, activeKey]));
  }, [active, activeKnown, activeKey, id, queryClient]);
  // Still unknown once read again: it's gone, and the tab says so as it always has.
  const loadingActive = !activeKnown && !checked.includes(activeKey);

  if (project.isError) {
    return (
      <Shell>
        <ErrorNote
          message={project.error instanceof ApiError ? project.error.detail : t("common.loadFailed")}
        />
      </Shell>
    );
  }
  if (!project.data || !items.data || !tabs) return <Shell>{t("common.loading")}</Shell>;

  const sessionOf = (id: number) => items.data.sessions.find((s) => s.id === id);
  const titleOf = (tab: Tab): string => {
    if (tab.kind === "artifact") {
      return items.data.artifacts.find((a) => a.id === tab.id)?.name ?? t("project.fileTab");
    }
    const session = sessionOf(tab.id);
    if (tab.kind === "document") return t("project.documentTab", { title: session?.ocr?.folder ?? "" });
    if (session?.kind === "ocr") return t("project.ocrTab", { title: session.title });
    return session?.title ?? t("project.chatTab");
  };
  const activeSession = tabs.active?.kind === "session" ? sessionOf(tabs.active.id) : undefined;

  return (
    <div className="grid h-screen grid-rows-[auto_auto_1fr]">
      <header className="flex flex-wrap items-center gap-3 border-b border-line bg-surface px-4 py-2">
        <Link to="/projects" className="text-sm text-muted hover:text-ink">
          {t("project.back")}
        </Link>
        <ProjectName project={project.data} />
        <span className="ml-auto" />
        <VramChip />
        <Link to="/settings/models" className="text-sm text-muted hover:text-ink">
          {t("project.settings")}
        </Link>
      </header>
      <nav role="tablist" className="flex gap-1 overflow-x-auto border-b border-line bg-paper px-2">
        <TabButton active={tabs.active === null} onClick={() => update(showOverview(tabs))}>
          {t("project.overview")}
        </TabButton>
        {tabs.open.map((tab) => (
          <TabButton
            key={`${tab.kind}-${tab.id}`}
            active={sameTab(tabs.active, tab)}
            onClick={() => update(openTab(tabs, tab))}
            onClose={() => update(closeTab(tabs, tab))}
            closeLabel={t("project.close", { title: titleOf(tab) })}
          >
            {titleOf(tab)}
          </TabButton>
        ))}
        <button
          aria-label={t("project.newChat")}
          title={t("project.newChat")}
          className="px-3 py-2 text-lg leading-none text-muted hover:text-ink disabled:opacity-50"
          disabled={newChat.isPending}
          onClick={() => newChat.mutate()}
        >
          +
        </button>
      </nav>
      <section className="min-h-0 overflow-y-auto">
        {newChat.error && (
          <div className="p-4">
            <ErrorNote
              message={newChat.error instanceof ApiError ? newChat.error.detail : t("common.loadFailed")}
            />
          </div>
        )}
        {tabs.active === null && <Overview projectId={id} items={items.data} onOpen={open} />}
        {loadingActive && <p className="p-4 text-muted">{t("common.loading")}</p>}
        {!loadingActive && tabs.active?.kind === "session" && activeSession?.kind === "ocr" && (
          <OcrTab
            key={tabs.active.id}
            projectId={id}
            sessionId={tabs.active.id}
            onOpen={open}
            initialPage={tabs.active.page}
          />
        )}
        {!loadingActive && tabs.active?.kind === "session" && activeSession?.kind !== "ocr" && (
          <ChatTab key={tabs.active.id} projectId={id} sessionId={tabs.active.id} onOpen={open} />
        )}
        {!loadingActive && tabs.active?.kind === "document" && (
          <DocumentTab key={tabs.active.id} projectId={id} sessionId={tabs.active.id} items={items.data} onOpen={open} />
        )}
        {!loadingActive && tabs.active?.kind === "artifact" && (
          <FileTab
            key={tabs.active.id}
            projectId={id}
            artifact={items.data.artifacts.find((a) => a.id === tabs.active?.id)}
            items={items.data}
            onOpen={open}
          />
        )}
      </section>
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto max-w-3xl px-4 py-12">{children}</main>;
}

function TabButton({
  active,
  onClick,
  onClose,
  closeLabel,
  children,
}: {
  active: boolean;
  onClick: () => void;
  onClose?: () => void;
  closeLabel?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`flex max-w-56 items-center gap-1 border-b-2 px-3 py-2 text-sm ${
        active ? "border-accent font-semibold text-ink" : "border-transparent text-muted"
      }`}
    >
      <button role="tab" aria-selected={active} className="truncate" onClick={onClick}>
        {children}
      </button>
      {onClose && (
        <button aria-label={closeLabel} className="text-muted hover:text-ink" onClick={onClose}>
          ×
        </button>
      )}
    </div>
  );
}

function ProjectName({ project }: { project: Project }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(project.name);
  const rename = useMutation({
    mutationFn: (value: string) =>
      apiFetch<Project>(`/api/projects/${project.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: value }),
      }),
    onSuccess: (renamed) => {
      queryClient.setQueryData(["project", project.id], renamed);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      setEditing(false);
    },
  });

  if (!editing) {
    return (
      <button
        className="truncate font-semibold"
        title={t("project.rename")}
        onClick={() => {
          setName(project.name);
          setEditing(true);
        }}
      >
        {project.name}
      </button>
    );
  }
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) rename.mutate(name.trim());
      }}
    >
      <input
        aria-label={t("project.renameLabel")}
        autoFocus
        className="rounded border border-line px-2 py-0.5"
        value={name}
        onChange={(e) => setName(e.target.value)}
        onBlur={() => setEditing(false)}
        onKeyDown={(e) => e.key === "Escape" && setEditing(false)}
      />
    </form>
  );
}

function VramChip() {
  const { t } = useTranslation();
  const runtime = useRuntime();
  if (!runtime.data) return null;
  const budget = formatMemory(runtime.data.budget);
  return (
    <span className="rounded-full bg-paper px-3 py-0.5 text-xs text-muted tabular-nums">
      {runtime.data.residents.length
        ? t("project.vram", { used: formatMemory(runtime.data.used), budget })
        : t("project.vramIdle", { budget })}
    </span>
  );
}

function known(items: ProjectItems, tab: Tab): boolean {
  return tab.kind === "artifact"
    ? items.artifacts.some((a) => a.id === tab.id)
    : items.sessions.some((s) => s.id === tab.id);
}
