import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, ApiError, apiPost } from "../../api/client";
import type {
  CatalogModel,
  ModelsResponse,
  Resident,
  Role,
  RuntimeSettings,
  RuntimeState,
  RuntimeSettingsValues,
  SystemReport,
} from "../../api/types";
import {
  Card,
  DownloadProgress,
  ErrorNote,
  HardwareRows,
  StatusText,
  TierBadge,
} from "../../components";
import { formatAgo, formatBytes, formatMemory, percent } from "../../format";
import { useDownloadEvents } from "../../hooks/useDownloadEvents";
import { useRuntime } from "../../hooks/useRuntime";
import { formatCountdown, modelWaitText, useCountdown } from "../../status";
import { UPDATE_POLL_MS } from "./polling";

const ROLES: Role[] = ["main", "ocr", "ocr.hand", "code"];
const ROLE_COLOURS: Record<Role, string> = {
  main: "bg-accent",
  ocr: "bg-amber-600",
  "ocr.hand": "bg-violet-600",
  code: "bg-slate-500",
};

/** The role a loaded model serves. The runtime also loads a model by name
 * ("main@qwen3.5-9b": OCR cleanup's copy of the chat model while the chat runs on MLX). */
const roleOf = (resident: Resident) => resident.role.split("@")[0] as Role;

/** Models & memory: the machine, live memory use, the model for each role, and how the
 * runtime shares the GPU. */
export function ModelsPage() {
  const { t } = useTranslation();
  const live = useDownloadEvents();
  const runtime = useRuntime();
  const system = useQuery({
    queryKey: ["system"],
    queryFn: () => apiFetch<SystemReport>("/api/system"),
  });
  const models = useQuery({
    queryKey: ["models"],
    queryFn: () => apiFetch<ModelsResponse>("/api/models"),
    // An update's progress is in the list, not in the download events: ask again while one
    // runs, to show how far it is and the new version when it's done.
    refetchInterval: (query) =>
      query.state.data?.models.some((m) => m.update && m.update.status !== "failed")
        ? UPDATE_POLL_MS
        : false,
  });
  const mlxOn = useRuntimeSettings().data?.values.mlx_enabled ?? false;
  const queryClient = useQueryClient();
  // The answer is the list again, with the role on the chosen model.
  const choose = useMutation({
    mutationFn: ({ role, modelId }: { role: Role; modelId: string | null }) =>
      apiFetch<ModelsResponse>(`/api/models/roles/${encodeURIComponent(role)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId }),
      }),
    onSuccess: (data) => queryClient.setQueryData(["models"], data),
  });

  if (system.isPending || models.isPending) {
    return <Page>{t("common.loading")}</Page>;
  }
  if (system.isError || models.isError) {
    const error = system.error ?? models.error;
    return (
      <Page>
        <ErrorNote message={error instanceof ApiError ? error.detail : t("common.loadFailed")} />
      </Page>
    );
  }

  const byId = new Map(models.data.models.map((m) => [m.id, m]));
  const loaded = (role: Role, modelId: string) =>
    runtime.data?.residents.find((r) => roleOf(r) === role && r.model_id === modelId);

  return (
    <Page>
      <header className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">{t("modelsPage.title")}</h1>
        <Link to="/projects" className="link-back">
          {t("project.back")}
        </Link>
      </header>
      <Card title={t("modelsPage.machine")}>
        <TierBadge tier={system.data.tier} />
        <HardwareRows system={system.data} />
      </Card>

      {runtime.data && (
        <Card title={t("memory.title")}>
          <MemoryGauge
            budget={runtime.data.budget}
            unified={runtime.data.unified}
            residents={runtime.data.residents}
          />
          <p className="text-sm text-muted">
            {t("memory.swaps", { count: runtime.data.swaps })}
            {runtime.data.hold ? (
              <>
                {" · "}
                <HeldFor hold={runtime.data.hold} since={runtime.dataUpdatedAt} />
              </>
            ) : runtime.data.activity ? (
              ` · ${modelWaitText(runtime.data, t)}`
            ) : (
              runtime.data.waiting > 0 && ` · ${t("memory.waiting", { count: runtime.data.waiting })}`
            )}
          </p>
        </Card>
      )}

      <Card title={t("modelsPage.roles")}>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase tracking-wider text-muted">
              <tr>
                <th className="py-2 pr-4 font-semibold">{t("models.role")}</th>
                <th className="py-2 pr-4 font-semibold">{t("models.model")}</th>
                <th className="py-2 pr-4 font-semibold">{t("models.size")}</th>
                <th className="py-2 pr-4 font-semibold">{t("models.licence")}</th>
                <th className="py-2 font-semibold">{t("models.status")}</th>
              </tr>
            </thead>
            <tbody>
              {ROLES.map((role) => {
                // The role's model: the one recommended for this computer, unless another
                // was chosen here.
                const id = models.data.chosen[role];
                const model = id ? byId.get(id) : undefined;
                if (!model) return null;
                const recommendedId = models.data.defaults[role];
                const choice: RoleChoice = {
                  // An MLX build isn't offered: it comes with its model.
                  options: models.data.models.filter((m) => m.roles.includes(role) && !m.replaces),
                  recommended: recommendedId ? byId.get(recommendedId) : undefined,
                  pending: choose.isPending,
                  onChoose: (modelId) => choose.mutate({ role, modelId }),
                };
                // With MLX switched on, a model's MLX build is listed under it. The runtime
                // uses the build once it's on disk, and the model until then.
                const build = mlxOn
                  ? models.data.models.find((m) => m.replaces === model.id)
                  : undefined;
                if (!build) {
                  return (
                    <RoleRow
                      key={role}
                      role={role}
                      model={model}
                      choice={choice}
                      // Its own loaded copy: right after a change the role's old model can
                      // still be loaded, finishing a reply.
                      resident={loaded(role, model.id)}
                      live={live[model.id]}
                    />
                  );
                }
                const buildOnDisk = (live[build.id] ?? build.download).status === "done";
                return (
                  <Fragment key={role}>
                    <RoleRow
                      role={role}
                      model={model}
                      choice={choice}
                      resident={loaded(role, model.id)}
                      live={live[model.id]}
                      chosen={!buildOnDisk}
                    />
                    <RoleRow
                      role={role}
                      model={build}
                      mlxBuild
                      resident={loaded(role, build.id)}
                      live={live[build.id]}
                      chosen={buildOnDisk}
                    />
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
        {choose.error && (
          <ErrorNote
            message={
              choose.error instanceof ApiError ? choose.error.detail : t("common.loadFailed")
            }
          />
        )}
        <p className="text-sm text-muted">{t("modelsPage.onFirstUse")}</p>
      </Card>

      <InstalledCard models={models.data} />

      <SettingsCard />
    </Page>
  );
}

/** Choosing a role's model: what it can be given, which one is recommended for this
 * computer, and how to change it (null: back to the recommended one). */
interface RoleChoice {
  options: CatalogModel[];
  recommended?: CatalogModel;
  pending: boolean;
  onChoose: (modelId: string | null) => void;
}

function RoleRow({
  role,
  model,
  choice,
  resident,
  live,
  mlxBuild = false,
  chosen = false,
}: {
  role: Role;
  model: CatalogModel;
  /** On a role's own row; an MLX build's row has nothing to choose. */
  choice?: RoleChoice;
  resident?: Resident;
  live?: CatalogModel["download"];
  /** The row of a model's MLX build, under the model's own. */
  mlxBuild?: boolean;
  /** Of a model and its MLX build, the one the runtime uses. */
  chosen?: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const state = live ?? model.download;
  // Shown for what isn't on disk and isn't on its way: "none", a stopped or paused download.
  const canStart = !resident && ["none", "failed", "paused"].includes(state.status);
  const start = useMutation({
    mutationFn: () =>
      apiPost(`/api/models/${model.id}/download`, { accept_licence: model.licence_notice }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["models"] }),
  });
  return (
    <tr className="border-t border-line align-top" data-role={role}>
      <td className="py-2 pr-4 font-mono text-xs">
        {mlxBuild ? <span className="text-muted">{t("modelsPage.mlxBuild")}</span> : role}
      </td>
      <td className="py-2 pr-4">
        {choice && choice.options.length > 1 ? (
          <>
            {/* A fixed width: a list is as wide as its longest option otherwise, and pushes
                the Status column off the card. The closed list shows the title; the rest
                is on the line under it. */}
            <select
              aria-label={t("modelsPage.modelFor", { role })}
              className="block w-48 max-w-full rounded border border-line bg-paper px-2 py-1"
              value={model.id}
              disabled={choice.pending}
              onChange={(e) => choice.onChoose(e.target.value)}
            >
              {choice.options.map((option) => (
                <option key={option.id} value={option.id}>
                  {t(
                    option.id === choice.recommended?.id
                      ? "modelsPage.optionRecommended"
                      : option.suits_tier
                        ? "modelsPage.option"
                        : "modelsPage.optionTooBig",
                    { title: option.title, size: formatBytes(option.size) },
                  )}
                </option>
              ))}
            </select>
            <span className="text-muted">{model.quant}</span>
          </>
        ) : (
          <>
            {model.title}
            <span className="text-muted"> · {model.quant}</span>
          </>
        )}
        {model.id === choice?.recommended?.id && (
          <span className="text-muted"> · {t("modelsPage.recommended")}</span>
        )}
        {choice && !model.suits_tier && (
          <span className="block text-muted">{t("modelsPage.tooBigWhy")}</span>
        )}
        {choice?.recommended && choice.recommended.id !== model.id && (
          <span className="block text-muted">
            {t("modelsPage.recommendedIs", { title: choice.recommended.title })}{" "}
            <button
              className="font-semibold text-accent underline-offset-4 hover:underline"
              aria-label={t("modelsPage.useRecommendedFor", {
                title: choice.recommended.title,
                role,
              })}
              disabled={choice.pending}
              onClick={() => choice.onChoose(null)}
            >
              {t("modelsPage.useRecommended")}
            </button>
          </span>
        )}
      </td>
      <td className="py-2 pr-4 tabular-nums">{formatBytes(model.size)}</td>
      <td className="py-2 pr-4">{model.licence}</td>
      <td className="py-2">
        <span data-testid={mlxBuild ? `status-${role}-mlx` : `status-${role}`}>
          {resident ? (
            <span className={resident.status === "crashed" ? "text-red-700" : "text-accent"}>
              {resident.status === "crashed"
                ? t("roleState.crashed")
                : resident.leases > 0
                  ? t("roleState.inUse")
                  : t("roleState.loaded")}
            </span>
          ) : state.status === "done" ? (
            t("roleState.onDisk")
          ) : (
            <StatusText state={state} />
          )}
        </span>
        {chosen && (
          <span className="text-muted" data-testid={`chosen-${role}`}>
            {" · "}
            {t("modelsPage.chosen")}
          </span>
        )}
        {state.status === "downloading" && <DownloadProgress state={state} />}
        {canStart && state.error && <ErrorNote message={state.error} />}
        {canStart && (
          <button
            className="btn-secondary mt-1 block w-fit"
            disabled={start.isPending}
            aria-label={
              model.licence_notice
                ? t("modelsPage.acceptAndDownloadModel", {
                    licence: model.licence,
                    title: model.title,
                  })
                : t("modelsPage.downloadModel", { title: model.title })
            }
            onClick={() => start.mutate()}
          >
            {model.licence_notice
              ? t("modelsPage.acceptAndDownload")
              : state.bytes_done > 0
                ? t("modelsPage.resume")
                : t("modelsPage.download")}
          </button>
        )}
        {start.error && (
          <ErrorNote
            message={start.error instanceof ApiError ? start.error.detail : t("common.loadFailed")}
          />
        )}
      </td>
    </tr>
  );
}

/** What's on disk: every model with files there and every folder no catalog model owns, each
 * with its version, its size and Remove. Nothing on disk: no card. */
function InstalledCard({ models }: { models: ModelsResponse }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // The one Remove was pressed for: it's asked about in place before anything is deleted.
  const [asking, setAsking] = useState<string | null>(null);
  // The answer is the list again, without what was removed.
  const remove = useMutation({
    mutationFn: (name: string) =>
      apiFetch<ModelsResponse>(`/api/models/${encodeURIComponent(name)}`, { method: "DELETE" }),
    onSuccess: (data) => queryClient.setQueryData(["models"], data),
    onSettled: () => setAsking(null),
  });
  // It runs on the server from here; the list says how far it is.
  const update = useMutation({
    mutationFn: (id: string) => apiPost(`/api/models/${encodeURIComponent(id)}/update`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["models"] }),
  });
  const rows = [
    ...models.models.flatMap((m) => {
      if (!m.installed) return [];
      const version = m.installed.revision
        ? t("modelsPage.version", { revision: m.installed.revision.slice(0, 7) })
        : m.download.status === "done"
          ? t("modelsPage.noVersion")
          : t("modelsPage.downloadNotFinished");
      const facts = [
        version,
        m.installed.at && t("modelsPage.installedAgo", { ago: formatAgo(m.installed.at) }),
        m.used_by.length > 0 && t("modelsPage.usedBy", { roles: m.used_by.join(", ") }),
        m.update_available && t("modelsPage.updateAvailable"),
      ];
      return [
        {
          name: m.id,
          title: m.title,
          quant: m.quant,
          facts: facts.filter((fact): fact is string => Boolean(fact)),
          bytes: m.installed.bytes,
          roles: m.used_by,
          update: m.update,
          // Offered again after a failure; not while one runs.
          canUpdate: m.update_available && (!m.update || m.update.status === "failed"),
        },
      ];
    }),
    ...models.unlisted.map((folder) => ({
      name: folder.name,
      title: folder.name,
      quant: null,
      facts: [t("modelsPage.notInCatalog")],
      bytes: folder.bytes,
      roles: [] as Role[],
      update: null,
      canUpdate: false,
    })),
  ];
  if (rows.length === 0) return null;
  return (
    <Card title={t("modelsPage.installed")}>
      <ul className="grid text-sm" data-testid="installed-models">
        {rows.map((row) => (
          <li
            key={row.name}
            className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 border-t border-line py-2 first:border-t-0"
            data-installed={row.name}
          >
            <span>
              {row.title}
              {row.quant && <span className="text-muted"> · {row.quant}</span>}
              <span className="block text-muted">{row.facts.join(" · ")}</span>
            </span>
            <span className="flex items-center gap-3">
              <span className="tabular-nums">{formatBytes(row.bytes)}</span>
              {row.canUpdate && (
                <button
                  className="btn-secondary"
                  aria-label={t("modelsPage.updateModel", { title: row.title })}
                  disabled={update.isPending}
                  onClick={() => update.mutate(row.name)}
                >
                  {t("modelsPage.update")}
                </button>
              )}
              {asking !== row.name && (
                <button
                  className="btn-secondary"
                  aria-label={t("modelsPage.removeModel", { title: row.title })}
                  onClick={() => setAsking(row.name)}
                >
                  {t("modelsPage.remove")}
                </button>
              )}
            </span>
            {row.update?.status === "checking" && (
              <span className="w-full text-xs text-muted">{t("modelsPage.updateChecking")}</span>
            )}
            {row.update?.status === "updating" && (
              <span className="grid w-full gap-1">
                <DownloadProgress state={{ ...row.update, status: "downloading" }} />
                <span className="text-xs text-muted">{t("modelsPage.updating")}</span>
              </span>
            )}
            {row.update?.status === "waiting" && (
              <span className="w-full text-xs text-muted">{t("modelsPage.updateWaiting")}</span>
            )}
            {row.update?.status === "failed" && row.update.error && (
              <span className="w-full">
                <ErrorNote message={row.update.error} />
              </span>
            )}
            {asking === row.name && (
              <span className="flex w-full flex-wrap items-center justify-end gap-2">
                <span>
                  {row.roles.length > 0
                    ? t("modelsPage.removeAskUsed", {
                        size: formatBytes(row.bytes),
                        roles: row.roles.join(", "),
                      })
                    : t("modelsPage.removeAsk", { size: formatBytes(row.bytes) })}
                </span>
                <button
                  className="btn-secondary"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(row.name)}
                >
                  {t("modelsPage.remove")}
                </button>
                <button
                  className="btn-secondary"
                  disabled={remove.isPending}
                  onClick={() => setAsking(null)}
                >
                  {t("modelsPage.keep")}
                </button>
              </span>
            )}
          </li>
        ))}
      </ul>
      {remove.error && (
        <ErrorNote
          message={remove.error instanceof ApiError ? remove.error.detail : t("common.loadFailed")}
        />
      )}
      {update.error && (
        <ErrorNote
          message={update.error instanceof ApiError ? update.error.detail : t("common.loadFailed")}
        />
      )}
      <p className="text-sm text-muted">{t("modelsPage.installedWhy")}</p>
    </Card>
  );
}

function MemoryGauge({
  budget,
  unified,
  residents,
}: {
  budget: number;
  unified: boolean;
  residents: Resident[];
}) {
  const { t } = useTranslation();
  const used = residents.reduce((sum, r) => sum + r.bytes, 0);
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap justify-between gap-2 text-sm">
        <span className="font-semibold">
          {unified ? t("memory.unifiedBudget") : t("memory.vramBudget")}
        </span>
        <span className="tabular-nums" data-testid="memory-used">
          {t("memory.used", { used: formatMemory(used), budget: formatMemory(budget) })}
        </span>
      </div>
      <div
        className="flex h-4 overflow-hidden rounded border border-line bg-paper"
        role="img"
        aria-label={t("memory.gaugeLabel", { pct: percent(used, budget) })}
      >
        {residents.map((r) => (
          <div
            key={r.role}
            className={`${ROLE_COLOURS[roleOf(r)]} h-full`}
            // Set through the element's style object: the CSP forbids inline style
            // attributes, not this.
            ref={(el) => {
              if (el) el.style.width = `${Math.max(1, percent(r.bytes, budget))}%`;
            }}
            title={`${r.role} · ${formatMemory(r.bytes)}`}
          />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        {residents.map((r) => (
          <li key={r.role}>
            <span className={`mr-1 inline-block size-2 rounded-full ${ROLE_COLOURS[roleOf(r)]}`} />
            {t("memory.legend", {
              role: r.role,
              model: r.model_id,
              size: formatMemory(r.bytes),
              ctx: r.ctx,
            })}
          </li>
        ))}
        {residents.length === 0 && <li>{t("memory.nothingLoaded")}</li>}
      </ul>
    </div>
  );
}

function useRuntimeSettings() {
  return useQuery({
    queryKey: ["runtime-settings"],
    queryFn: () => apiFetch<RuntimeSettings>("/api/runtime/settings"),
  });
}

function SettingsCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const current = useRuntimeSettings();
  const save = useMutation({
    mutationFn: (values: RuntimeSettingsValues) =>
      apiFetch<RuntimeSettings>("/api/runtime/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      }),
    onSuccess: (data) => queryClient.setQueryData(["runtime-settings"], data),
  });
  if (!current.data) return null;
  const { values, implemented, mlx_supported, mlx_available } = current.data;
  const set = (patch: Partial<RuntimeSettingsValues>) => save.mutate({ ...values, ...patch });

  const group = (key: "when_models_dont_fit" | "chat_during_job", options: string[]) => (
    <fieldset className="grid gap-2">
      <legend className="text-sm font-semibold">{t(`policy.${key}.title`)}</legend>
      {options.map((option) => {
        const available = implemented[key].includes(option);
        return (
          <label key={option} className={`flex gap-2 text-sm ${available ? "" : "opacity-50"}`}>
            <input
              type="radio"
              name={key}
              value={option}
              checked={values[key] === option}
              disabled={!available || save.isPending}
              onChange={() => set({ [key]: option } as Partial<RuntimeSettingsValues>)}
            />
            <span>
              {t(`policy.${key}.${option}`)}
              {!available && <span className="text-muted"> · {t("policy.later")}</span>}
            </span>
          </label>
        );
      })}
    </fieldset>
  );

  return (
    <Card title={t("policy.title")}>
      <div className="grid gap-4 sm:grid-cols-2">
        {group("when_models_dont_fit", ["swap_between_phases", "keep_one_loaded"])}
        {group("chat_during_job", ["pause_job_after_current_item", "chat_waits"])}
      </div>
      {mlx_supported && (
        <label className="flex gap-2 text-sm">
          <input
            type="checkbox"
            checked={values.mlx_enabled}
            disabled={!mlx_available || save.isPending}
            onChange={(e) => set({ mlx_enabled: e.target.checked })}
          />
          <span>
            {t("policy.mlx")}
            <span className="block text-muted">
              {t(mlx_available ? "policy.mlxWhy" : "policy.mlxMissing")}
            </span>
          </span>
        </label>
      )}
      {save.error && (
        <ErrorNote
          message={save.error instanceof ApiError ? save.error.detail : t("common.loadFailed")}
        />
      )}
    </Card>
  );
}

function Page({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto grid max-w-3xl gap-5 px-4 py-12">{children}</main>;
}

/** A job waiting for the chat: until its reply ends, or for the rest of the quiet window. */
function HeldFor({ hold, since }: { hold: NonNullable<RuntimeState["hold"]>; since: number }) {
  const { t } = useTranslation();
  const left = useCountdown(hold.resume_in, since);
  return (
    <>{left === null ? t("memory.heldReply") : t("memory.heldFor", { time: formatCountdown(left) })}</>
  );
}
