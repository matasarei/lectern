import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, ApiError, apiPost } from "../../api/client";
import type {
  CatalogModel,
  ChoiceRole,
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

// The rows of "Models by role": the roles, and cleanup of scanned pages after the chat,
// whose models it shares.
const CHOICES: ChoiceRole[] = ["main", "clean", "ocr", "ocr.hand", "code"];
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
    mutationFn: ({ role, modelId }: { role: ChoiceRole; modelId: string | null }) =>
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
  // Cleanup's copy is leased as the chat role's: "main" when it is the chat's own loaded
  // model, "main@<model>" when it has one to itself.
  const loaded = (role: ChoiceRole, modelId: string) =>
    runtime.data?.residents.find(
      (r) => roleOf(r) === (role === "clean" ? "main" : role) && r.model_id === modelId,
    );

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
              {CHOICES.map((role) => {
                // The role's model: the one recommended for this computer, unless another
                // was chosen here.
                const id = models.data.chosen[role];
                const model = id ? byId.get(id) : undefined;
                if (!model) return null;
                const recommendedId = models.data.defaults[role];
                const choice: RoleChoice = {
                  // What the server says it can be given. An MLX build never is: it comes
                  // with its model, and can't clean pages.
                  options: (models.data.choices[role] ?? []).flatMap((option) => byId.get(option) ?? []),
                  recommended: recommendedId ? byId.get(recommendedId) : undefined,
                  pending: choose.isPending,
                  onChoose: (modelId) => choose.mutate({ role, modelId }),
                };
                // With MLX switched on, a model's MLX build is listed under it. The runtime
                // uses the build once it's on disk, and the model until then. Not for
                // cleanup, which a build can't do.
                const build =
                  mlxOn && role !== "clean"
                    ? models.data.models.find((m) => m.replaces === model.id)
                    : undefined;
                if (role === "clean") {
                  return (
                    <RoleRow
                      key={role}
                      role={role}
                      model={model}
                      choice={choice}
                      resident={loaded(role, model.id)}
                      live={live[model.id]}
                      sharesTheChats={cleansWithTheChats(models.data, mlxOn, live)}
                    />
                  );
                }
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
                if ((live[build.id] ?? build.download).status === "done") {
                  // The build is what runs: the role's one row is the build's. The usual
                  // file isn't offered here; what still needs one (cleanup) has its own row.
                  return (
                    <RoleRow
                      key={role}
                      role={role}
                      model={model}
                      choice={choice}
                      runsOn={build}
                      resident={loaded(role, build.id)}
                      live={live[build.id]}
                    />
                  );
                }
                return (
                  <Fragment key={role}>
                    <RoleRow
                      role={role}
                      model={model}
                      choice={choice}
                      resident={loaded(role, model.id)}
                      live={live[model.id]}
                      chosen
                    />
                    <RoleRow
                      role={role}
                      model={build}
                      mlxBuild
                      resident={loaded(role, build.id)}
                      live={live[build.id]}
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

/** Cleanup uses the copy of the model the chat itself runs on: the chat's own model, with
 * the chat on llama.cpp (on its MLX build the chat's copy can't clean, so cleanup loads one). */
function cleansWithTheChats(
  list: ModelsResponse,
  mlxOn: boolean,
  live: Record<string, CatalogModel["download"]>,
): boolean {
  if (list.chosen.clean !== list.chosen.main) return false;
  const build = list.models.find((m) => m.replaces === list.chosen.main);
  const buildInUse = mlxOn && build && (live[build.id] ?? build.download).status === "done";
  return !buildInUse;
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
  runsOn,
  sharesTheChats = false,
}: {
  role: ChoiceRole;
  model: CatalogModel;
  /** On a role's own row; an MLX build's row has nothing to choose. */
  choice?: RoleChoice;
  resident?: Resident;
  live?: CatalogModel["download"];
  /** The row of a model's MLX build, under the model's own. */
  mlxBuild?: boolean;
  /** Of a model and its MLX build not yet on disk, the one the runtime uses: the model. */
  chosen?: boolean;
  /** The model's MLX build, when that is what the role runs on: the row's size, licence,
   * status and `resident`/`live` are the build's, under the model's name and choice. */
  runsOn?: CatalogModel;
  /** Cleanup's row: its model is the copy the chat runs on, so nothing more is loaded. */
  sharesTheChats?: boolean;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // What the row's facts are about: the model, or the MLX build the role runs on.
  const shown = runsOn ?? model;
  const state = live ?? shown.download;
  // Shown for what isn't on disk and isn't on its way: "none", a stopped or paused download.
  // Not on cleanup's row for the chat's own copy: the chat's row, just above, has it.
  const canStart =
    !resident && !sharesTheChats && ["none", "failed", "paused"].includes(state.status);
  const start = useMutation({
    mutationFn: () =>
      apiPost(`/api/models/${shown.id}/download`, { accept_licence: shown.licence_notice }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["models"] }),
  });
  const detail = runsOn ? t("modelsPage.runsOnBuild", { quant: runsOn.quant }) : model.quant;
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
                    {
                      title: option.title,
                      // Of the model in use, what runs: its MLX build's, as the Size column.
                      size: formatBytes(option.id === model.id ? shown.size : option.size),
                    },
                  )}
                </option>
              ))}
            </select>
            <span className="text-muted">{detail}</span>
          </>
        ) : (
          <>
            {model.title}
            <span className="text-muted"> · {detail}</span>
          </>
        )}
        {model.id === choice?.recommended?.id && (
          <span className="text-muted"> · {t("modelsPage.recommended")}</span>
        )}
        {choice && !model.suits_tier && (
          <span className="block text-muted">{t("modelsPage.tooBigWhy")}</span>
        )}
        {role === "clean" && <span className="block text-muted">{t("modelsPage.cleanWhat")}</span>}
        {sharesTheChats && (
          <span className="block text-muted">{t("modelsPage.cleanShares")}</span>
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
      <td className="py-2 pr-4 tabular-nums">{formatBytes(shown.size)}</td>
      <td className="py-2 pr-4">{shown.licence}</td>
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
              shown.licence_notice
                ? t("modelsPage.acceptAndDownloadModel", {
                    licence: shown.licence,
                    title: shown.title,
                  })
                : t("modelsPage.downloadModel", { title: shown.title })
            }
            onClick={() => start.mutate()}
          >
            {shown.licence_notice
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
            {/* Takes the room that's left and wraps inside it: a long line of facts doesn't
                push the size and the buttons onto a row of their own. */}
            <span className="min-w-0 flex-1">
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
