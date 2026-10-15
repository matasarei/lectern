import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, ApiError } from "../api/client";
import type { Project } from "../api/types";
import { ErrorNote } from "../components";
import { AccountBar } from "./login";

const FIELD = "rounded-md border border-line bg-surface px-3 py-1.5";

/** The list of projects, the button that starts a new one, and a × on each to remove it. */
export function ProjectsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [removing, setRemoving] = useState<Project | null>(null);
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => apiFetch<Project[]>("/api/projects"),
  });
  const create = useMutation({
    mutationFn: () => apiPost<Project>("/api/projects", {}),
    onSuccess: (project) =>
      void navigate({ to: "/projects/$projectId", params: { projectId: String(project.id) } }),
  });

  return (
    <main className="mx-auto grid max-w-3xl gap-5 px-4 py-12">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">{t("projects.title")}</h1>
        <div className="flex gap-2">
          <Link to="/settings/models" className="btn-secondary">
            {t("projects.settings")}
          </Link>
          <button className="btn-primary" disabled={create.isPending} onClick={() => create.mutate()}>
            {t("projects.new")}
          </button>
        </div>
      </header>
      <AccountBar />
      {projects.isPending && <p>{t("common.loading")}</p>}
      {projects.isError && (
        <ErrorNote
          message={projects.error instanceof ApiError ? projects.error.detail : t("common.loadFailed")}
        />
      )}
      {create.error && (
        <ErrorNote
          message={create.error instanceof ApiError ? create.error.detail : t("common.loadFailed")}
        />
      )}
      {projects.data?.length === 0 && <p className="text-muted">{t("projects.empty")}</p>}
      <ul className="grid gap-2">
        {projects.data?.map((project) => (
          <li key={project.id} className="flex min-w-0 items-stretch gap-2">
            <Link
              to="/projects/$projectId"
              params={{ projectId: String(project.id) }}
              className="block min-w-0 flex-1 rounded-lg border border-line bg-surface px-4 py-3 hover:bg-accent-soft"
            >
              <span className="font-semibold">{project.name}</span>
              <span className="block truncate text-xs text-muted">
                {t("projects.folder", { folder: project.folder })}
              </span>
            </Link>
            <button
              className="btn-secondary px-3"
              aria-label={t("projects.remove", { name: project.name })}
              title={t("projects.remove", { name: project.name })}
              onClick={() => setRemoving(project)}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      {removing && <RemoveProject project={removing} onClose={() => setRemoving(null)} />}
    </main>
  );
}

/** Asks before a project goes: Remove stays off until its name is typed exactly, case and
 * spaces too (the server checks the same). Its folder waits in the trash for 30 days. */
function RemoveProject({ project, onClose }: { project: Project; onClose: () => void }) {
  const { t } = useTranslation();
  const queries = useQueryClient();
  const [typed, setTyped] = useState("");
  const remove = useMutation({
    mutationFn: () =>
      apiFetch<{ removed: number }>(`/api/projects/${project.id}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: typed }),
      }),
    onSuccess: async () => {
      await queries.invalidateQueries({ queryKey: ["projects"] });
      onClose();
    },
  });

  return (
    <div
      className="fixed inset-0 z-10 grid place-items-center bg-ink/40 px-4"
      onKeyDown={(event) => event.key === "Escape" && onClose()}
    >
      <form
        role="dialog"
        aria-modal="true"
        aria-labelledby="remove-project-title"
        className="grid w-full max-w-md gap-3 rounded-lg border border-line bg-surface p-5"
        onSubmit={(event) => {
          event.preventDefault();
          if (typed === project.name) remove.mutate();
        }}
      >
        <h2 id="remove-project-title" className="text-lg font-semibold">
          {t("projects.removeTitle", { name: project.name })}
        </h2>
        <p className="text-sm">{t("projects.removeWhat")}</p>
        <label className="grid gap-1 text-sm">
          {t("projects.removeType", { name: project.name })}
          <input
            className={FIELD}
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            autoFocus
          />
        </label>
        {remove.error && (
          <ErrorNote
            message={remove.error instanceof ApiError ? remove.error.detail : t("common.loadFailed")}
          />
        )}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose}>
            {t("projects.removeCancel")}
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={typed !== project.name || remove.isPending}
          >
            {t("projects.removeConfirm")}
          </button>
        </div>
      </form>
    </div>
  );
}
