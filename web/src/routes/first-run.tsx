import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, ApiError } from "../api/client";
import type { DownloadState, FirstRun, SandboxStatus, SystemReport } from "../api/types";
import {
  Card,
  DownloadProgress,
  ErrorNote,
  HardwareRows,
  Row,
  StatusText,
  TierBadge,
} from "../components";
import { formatBytes } from "../format";
import { useDownloadEvents } from "../hooks/useDownloadEvents";

const ACTIVE: DownloadState["status"][] = ["queued", "downloading", "verifying"];
// Keep in step with DISK_HEADROOM in src/lectern/downloads/manager.py (1 GiB).
const DISK_HEADROOM = 1024 ** 3;
// While no engine is running, check again this often, so starting Docker or Podman shows up
// without a reload.
const SANDBOX_POLL_MS = 10_000;

export function FirstRunPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const live = useDownloadEvents();

  const system = useQuery({
    queryKey: ["system"],
    queryFn: () => apiFetch<SystemReport>("/api/system"),
  });
  const firstRun = useQuery({
    queryKey: ["first-run"],
    queryFn: () => apiFetch<FirstRun>("/api/first-run"),
  });

  const advance = useMutation({
    mutationFn: (to: FirstRun["step"]) => apiPost<FirstRun>("/api/first-run", { to }),
    onSuccess: (data) => {
      queryClient.setQueryData(["first-run"], data);
      if (data.step === "done") void navigate({ to: "/projects" });
    },
  });
  const download = useMutation({
    mutationFn: (id: string) => apiPost<DownloadState>(`/api/models/${id}/download`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["first-run"] }),
  });

  if (system.isPending || firstRun.isPending) {
    return <Page>{t("common.loading")}</Page>;
  }
  if (system.isError || firstRun.isError) {
    const error = system.error ?? firstRun.error;
    return (
      <Page>
        <ErrorNote message={error instanceof ApiError ? error.detail : t("common.loadFailed")} />
      </Page>
    );
  }

  const run = firstRun.data;
  const model = run.chat_model;
  const state = live[model.id] ?? model.download;
  const enoughDisk = system.data.disk_free >= model.size - state.bytes_done + DISK_HEADROOM;

  return (
    <Page>
      <header className="grid gap-2">
        <h1 className="text-2xl font-semibold">{t("firstRun.title")}</h1>
        <p className="text-muted">{t("firstRun.intro")}</p>
      </header>

      <Card title={t("firstRun.hardwareCard")}>
        <TierBadge tier={system.data.tier} />
        <HardwareRows system={system.data} />
        {run.step === "hardware" && (
          <button className="btn-primary mt-2 w-fit" onClick={() => advance.mutate("download")}>
            {t("firstRun.continue")}
          </button>
        )}
      </Card>

      {run.step !== "hardware" && (
        <Card title={t("firstRun.modelCard")}>
          <Row label={t("models.model")} value={`${model.title} · ${model.quant}`} />
          <Row label={t("models.size")} value={formatBytes(model.size)} />
          <Row label={t("models.licence")} value={model.licence} />
          <Row label={t("firstRun.diskFree")} value={formatBytes(system.data.disk_free)} />
          <Row label={t("models.status")} value={<StatusText state={state} />} />
          {ACTIVE.includes(state.status) && <DownloadProgress state={state} />}
          {state.error && <ErrorNote message={state.error} />}
          {!enoughDisk && state.status !== "done" && (
            <ErrorNote message={t("firstRun.notEnoughDisk")} />
          )}
          {state.status !== "done" && !ACTIVE.includes(state.status) && (
            <button
              className="btn-primary mt-2 w-fit"
              disabled={download.isPending || !enoughDisk}
              onClick={() => download.mutate(model.id)}
            >
              {state.bytes_done > 0 ? t("firstRun.resume") : t("firstRun.download")}
            </button>
          )}
          {state.status === "done" && run.step === "download" && (
            <button className="btn-primary mt-2 w-fit" onClick={() => advance.mutate("done")}>
              {t("firstRun.finish")}
            </button>
          )}
        </Card>
      )}

      <SandboxCard initial={system.data.sandbox} />

      {advance.error && (
        <ErrorNote
          message={advance.error instanceof ApiError ? advance.error.detail : t("common.loadFailed")}
        />
      )}
    </Page>
  );
}

function SandboxCard({ initial }: { initial: SandboxStatus }) {
  const { t } = useTranslation();
  const check = useQuery({
    queryKey: ["system", "sandbox"],
    queryFn: () => apiFetch<SandboxStatus>("/api/system/sandbox"),
    initialData: initial,
    staleTime: SANDBOX_POLL_MS,
    refetchInterval: (query) => (query.state.data?.engine_running ? false : SANDBOX_POLL_MS),
  });
  const sandbox = check.data;

  return (
    <Card title={t("firstRun.sandboxCard")}>
      <p className="text-sm" data-testid="sandbox-status">
        {sandbox.engine_running
          ? t("firstRun.sandboxFound", { runtime: t(`sandbox.${sandbox.runtime}`), version: sandbox.version })
          : sandbox.runtime
            ? t("firstRun.sandboxStopped", { runtime: t(`sandbox.${sandbox.runtime}`) })
            : t("firstRun.sandboxMissing")}
      </p>
      {!sandbox.engine_running && (
        <>
          <p className="text-sm text-muted">{t("firstRun.sandboxWhy")}</p>
          <button
            className="btn-secondary mt-2 w-fit"
            disabled={check.isFetching}
            onClick={() => void check.refetch()}
          >
            {check.isFetching ? t("firstRun.sandboxChecking") : t("firstRun.sandboxCheck")}
          </button>
        </>
      )}
      {check.isError && !check.isFetching && (
        <ErrorNote
          message={check.error instanceof ApiError ? check.error.detail : t("common.loadFailed")}
        />
      )}
    </Card>
  );
}

function Page({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto grid max-w-2xl gap-5 px-4 py-12">{children}</main>;
}
