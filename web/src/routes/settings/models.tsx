import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { apiFetch, ApiError } from "../../api/client";
import type { ModelsResponse, Role, SystemReport } from "../../api/types";
import { Card, DownloadProgress, ErrorNote, HardwareRows, StatusText, TierBadge } from "../../components";
import { formatBytes } from "../../format";
import { useDownloadEvents } from "../../hooks/useDownloadEvents";

const ROLES: Role[] = ["main", "ocr", "ocr.hand", "code"];

/** Models & memory, read-only in M0a: hardware, tier, and the model for each role. The live
 * memory gauges and runner states arrive with the runtime (M0b). */
export function ModelsPage() {
  const { t } = useTranslation();
  const live = useDownloadEvents();
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

  return (
    <Page>
      <h1 className="text-2xl font-semibold">{t("modelsPage.title")}</h1>
      <Card title={t("modelsPage.machine")}>
        <TierBadge tier={system.data.tier} />
        <HardwareRows system={system.data} />
      </Card>
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
                const state = live[model.id] ?? model.download;
                return (
                  <tr key={role} className="border-t border-line align-top">
                    <td className="py-2 pr-4 font-mono text-xs">{role}</td>
                    <td className="py-2 pr-4">
                      {model.title}
                      <span className="text-muted"> · {model.quant}</span>
                    </td>
                    <td className="py-2 pr-4 tabular-nums">{formatBytes(model.size)}</td>
                    <td className="py-2 pr-4">{model.licence}</td>
                    <td className="py-2">
                      <StatusText state={state} />
                      {state.status === "downloading" && <DownloadProgress state={state} />}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="text-sm text-muted">{t("modelsPage.onFirstUse")}</p>
      </Card>
    </Page>
  );
}

function Page({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto grid max-w-3xl gap-5 px-4 py-12">{children}</main>;
}
