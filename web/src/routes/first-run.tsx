import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { apiFetch, apiPost, ApiError } from "../api/client";
import type {
  DownloadState,
  EngineState,
  FirstRun,
  FirstRunModel,
  RuntimeSettings,
  SandboxStatus,
  SystemReport,
} from "../api/types";
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
const ENGINE_POLL_MS = 1_000;

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
    // The engine's download is followed here; the chat model's through its own events.
    refetchInterval: (query) =>
      query.state.data?.engine?.status === "downloading" ? ENGINE_POLL_MS : false,
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
  const fetchEngine = useMutation({
    mutationFn: () => apiPost<FirstRun>("/api/first-run/engine"),
    onSuccess: (data) => queryClient.setQueryData(["first-run"], data),
  });
  // Why the last pick (a model or an engine) was refused; the next pick clears it.
  const [refused, setRefused] = useState<Error | null>(null);
  // The same choice as in Models & memory, which shows it afterwards.
  const choose = useMutation({
    onMutate: () => setRefused(null),
    onError: setRefused,
    mutationFn: (modelId: string) =>
      apiFetch("/api/models/roles/main", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["first-run"] }),
  });
  // The "Use MLX" switch of Models & memory. The settings are saved whole, so the others
  // are read first and sent back as they are.
  const pickEngine = useMutation({
    onMutate: () => setRefused(null),
    onError: setRefused,
    mutationFn: async (enabled: boolean) => {
      const current = await apiFetch<RuntimeSettings>("/api/runtime/settings");
      return apiFetch<RuntimeSettings>("/api/runtime/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...current.values, mlx_enabled: enabled }),
      });
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["runtime-settings"], data);
      return queryClient.invalidateQueries({ queryKey: ["first-run"] });
    },
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
  // With MLX picked the chat runs on the model's MLX build, and that is all setup fetches:
  // llama.cpp and the usual model come with whatever first needs them (a scanned page).
  const build = run.mlx.enabled ? model.mlx_build : null;
  const target = build ?? model;
  const state = live[target.id] ?? target.download;
  const enoughDisk = system.data.disk_free >= target.size - state.bytes_done + DISK_HEADROOM;
  const engine = build ? null : run.engine;
  const chatToFetch = state.status !== "done" && !ACTIVE.includes(state.status);
  const engineToFetch = engine?.status === "none";
  // A pick is kept while it downloads: changing it would leave that download running.
  const downloading = ACTIVE.includes(state.status) || engine?.status === "downloading";
  const settled = !downloading && !choose.isPending && !pickEngine.isPending;

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
          <Row
            label={t("models.model")}
            value={
              <ModelChoice
                model={model}
                // The usual model's own detail: its MLX build has a row to itself.
                quant={build ? null : model.quant}
                options={run.chat_models}
                disabled={!settled}
                onChoose={(id) => choose.mutate(id)}
              />
            }
          />
          {!model.suits_tier && <p className="text-sm text-muted">{t("modelsPage.tooBigWhy")}</p>}
          {run.mlx.available && (
            <EngineChoice
              mlx={run.mlx.enabled}
              disabled={!settled}
              onChoose={(enabled) => pickEngine.mutate(enabled)}
            />
          )}
          {run.mlx.supported && !run.mlx.available && (
            <p className="text-sm text-muted">{t("policy.mlxMissing")}</p>
          )}
          {refused && (
            <ErrorNote
              message={refused instanceof ApiError ? refused.detail : t("common.loadFailed")}
            />
          )}
          {build && <Row label={t("modelsPage.mlxBuild")} value={build.title} />}
          <Row label={t("models.size")} value={formatBytes(target.size)} />
          <Row label={t("models.licence")} value={model.licence} />
          <Row label={t("firstRun.diskFree")} value={formatBytes(system.data.disk_free)} />
          <Row label={t("models.status")} value={<StatusText state={state} />} />
          {ACTIVE.includes(state.status) && <DownloadProgress state={state} />}
          {state.error && <ErrorNote message={state.error} />}
          {!enoughDisk && state.status !== "done" && (
            <ErrorNote message={t("firstRun.notEnoughDisk")} />
          )}
          {engine && <EngineRows engine={engine} onRetry={() => fetchEngine.mutate()} />}
          {(chatToFetch || engineToFetch) && (
            <button
              className="btn-primary mt-2 w-fit"
              disabled={download.isPending || fetchEngine.isPending || !enoughDisk}
              onClick={() => {
                // One click fetches both, so the first chat only has to load the model.
                if (chatToFetch) download.mutate(target.id);
                if (engineToFetch) fetchEngine.mutate();
              }}
            >
              {state.bytes_done > 0 ? t("firstRun.resume") : t("firstRun.download")}
            </button>
          )}
          {run.can_finish && run.step === "download" && (
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

/** The chat model: a list when there's more than one to choose from. */
function ModelChoice({
  model,
  quant,
  options,
  disabled,
  onChoose,
}: {
  model: FirstRunModel;
  quant: string | null;
  options: FirstRunModel[];
  disabled: boolean;
  onChoose: (modelId: string) => void;
}) {
  const { t } = useTranslation();
  const detail = quant && <span className="text-muted"> · {quant}</span>;
  if (options.length < 2) {
    return (
      <>
        {model.title}
        {detail}
      </>
    );
  }
  return (
    <>
      <select
        aria-label={t("firstRun.chooseModel")}
        className="max-w-full rounded border border-line bg-paper px-2 py-1"
        value={model.id}
        disabled={disabled}
        onChange={(e) => onChoose(e.target.value)}
      >
        {options.map((option) => (
          <option key={option.id} value={option.id}>
            {t(
              option.recommended
                ? "modelsPage.optionRecommended"
                : option.suits_tier
                  ? "modelsPage.option"
                  : "modelsPage.optionTooBig",
              { title: option.title, size: formatBytes(option.size) },
            )}
          </option>
        ))}
      </select>
      {detail}
    </>
  );
}

/** On a Mac with MLX installed: which engine the chat runs on. */
function EngineChoice({
  mlx,
  disabled,
  onChoose,
}: {
  mlx: boolean;
  disabled: boolean;
  onChoose: (mlx: boolean) => void;
}) {
  const { t } = useTranslation();
  const option = (picksMlx: boolean, name: string, why: string) => (
    <label className="flex items-start gap-2 text-sm">
      <input
        type="radio"
        name="engine"
        className="mt-1"
        checked={mlx === picksMlx}
        disabled={disabled}
        onChange={() => onChoose(picksMlx)}
      />
      <span>
        {name}
        <span className="block text-muted">{why}</span>
      </span>
    </label>
  );
  return (
    <fieldset className="grid gap-2">
      <legend className="text-sm text-muted">{t("firstRun.engineChoice")}</legend>
      {option(false, t("firstRun.engineLlama"), t("firstRun.engineLlamaWhy"))}
      {option(true, t("firstRun.engineMlx"), t("firstRun.engineMlxWhy"))}
    </fieldset>
  );
}

/** The llama.cpp build: what it is, where its download is, and why it stopped. */
function EngineRows({ engine, onRetry }: { engine: EngineState; onRetry: () => void }) {
  const { t } = useTranslation();
  const progress: DownloadState = {
    model_id: "engine",
    status: engine.status === "downloading" ? "downloading" : "none",
    bytes_done: engine.bytes_done,
    bytes_total: engine.bytes_total,
    rate: 0,
    error: null,
  };
  return (
    <>
      <Row
        label={t("firstRun.engine")}
        value={
          <span data-testid="engine-status">
            {t("firstRun.engineName", { variant: engine.variant })} ·{" "}
            {t(`download.status.${engine.status}`)}
          </span>
        }
      />
      {engine.status === "downloading" && <DownloadProgress state={progress} />}
      {engine.status === "failed" && (
        <>
          <ErrorNote message={engine.error ?? t("common.loadFailed")} />
          <button className="btn-secondary mt-1 w-fit" onClick={onRetry}>
            {t("firstRun.engineRetry")}
          </button>
        </>
      )}
    </>
  );
}
