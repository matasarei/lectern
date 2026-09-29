import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, ApiError } from "../api/client";
import type { Project } from "../api/types";
import { ErrorNote } from "../components";

/** The list of projects, and the button that starts a new one. */
export function ProjectsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
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
          <li key={project.id}>
            <Link
              to="/projects/$projectId"
              params={{ projectId: String(project.id) }}
              className="block rounded-lg border border-line bg-surface px-4 py-3 hover:bg-accent-soft"
            >
              <span className="font-semibold">{project.name}</span>
              <span className="block truncate text-xs text-muted">
                {t("projects.folder", { folder: project.folder })}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}
