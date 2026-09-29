import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiFetch, ApiError } from "../../api/client";
import type {
  CatalogModel,
  ModelsResponse,
  Resident,
  Role,
  RuntimeSettings,
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
import { formatBytes, formatMemory, percent } from "../../format";
import { useDownloadEvents } from "../../hooks/useDownloadEvents";
import { useRuntime } from "../../hooks/useRuntime";

const ROLES: Role[] = ["main", "ocr", "ocr.hand", "code"];
const ROLE_COLOURS: Record<Role, string> = {
  main: "bg-accent",
  ocr: "bg-amber-600",
  "ocr.hand": "bg-violet-600",
  code: "bg-slate-500",
};

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
  const residents = new Map((runtime.data?.residents ?? []).map((r) => [r.role, r]));

  return (
    <Page>
      <h1 className="text-2xl font-semibold">{t("modelsPage.title")}</h1>
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
            {runtime.data.waiting > 0 && ` · ${t("memory.waiting", { count: runtime.data.waiting })}`}
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
                const id = models.data.defaults[role];
                const model = id ? byId.get(id) : undefined;
                if (!model) return null;
                return (
                  <RoleRow
                    key={role}
                    role={role}
                    model={model}
                    resident={residents.get(role)}
                    live={live[model.id]}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="text-sm text-muted">{t("modelsPage.onFirstUse")}</p>
      </Card>

      <SettingsCard />
    </Page>
  );
}

function RoleRow({
  role,
  model,
  resident,
  live,
}: {
  role: Role;
  model: CatalogModel;
  resident?: Resident;
  live?: CatalogModel["download"];
}) {
  const { t } = useTranslation();
  const state = live ?? model.download;
  return (
    <tr className="border-t border-line align-top" data-role={role}>
      <td className="py-2 pr-4 font-mono text-xs">{role}</td>
      <td className="py-2 pr-4">
        {model.title}
        <span className="text-muted"> · {model.quant}</span>
      </td>
      <td className="py-2 pr-4 tabular-nums">{formatBytes(model.size)}</td>
      <td className="py-2 pr-4">{model.licence}</td>
      <td className="py-2" data-testid={`status-${role}`}>
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
        {state.status === "downloading" && <DownloadProgress state={state} />}
      </td>
    </tr>
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
            className={`${ROLE_COLOURS[r.role]} h-full`}
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
            <span className={`mr-1 inline-block size-2 rounded-full ${ROLE_COLOURS[r.role]}`} />
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

function SettingsCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const current = useQuery({
    queryKey: ["runtime-settings"],
    queryFn: () => apiFetch<RuntimeSettings>("/api/runtime/settings"),
  });
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
  const { values, implemented } = current.data;
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
      <label className="flex gap-2 text-sm">
        <input
          type="checkbox"
          checked={values.mlx_enabled}
          disabled={save.isPending}
          onChange={(e) => set({ mlx_enabled: e.target.checked })}
        />
        <span>
          {t("policy.mlx")}
          <span className="block text-muted">{t("policy.mlxWhy")}</span>
        </span>
      </label>
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
