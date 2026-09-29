import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { DownloadState, SystemReport } from "./api/types";
import { formatBytes, formatMemory, percent } from "./format";

export function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-line bg-surface p-5">
      <h2 className="text-xs font-semibold uppercase tracking-wider text-muted">{title}</h2>
      <div className="mt-3 grid gap-2">{children}</div>
    </section>
  );
}

export function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-wrap justify-between gap-x-6 gap-y-1 text-sm">
      <span className="text-muted">{label}</span>
      <span className="font-medium tabular-nums">{value}</span>
    </div>
  );
}

export function HardwareRows({ system }: { system: SystemReport }) {
  const { t } = useTranslation();
  const { hardware } = system;
  const gpu = hardware.gpus[0];
  return (
    <>
      <Row label={t("hardware.os")} value={t(`hardware.osNames.${hardware.os}`)} />
      <Row label={t("hardware.cpu")} value={hardware.cpu} />
      <Row label={t("hardware.ram")} value={formatMemory(hardware.ram_total)} />
      <Row label={t("hardware.gpu")} value={gpu ? gpu.name : t("hardware.noGpu")} />
      {gpu && (
        <Row
          label={gpu.unified ? t("hardware.gpuBudgetUnified") : t("hardware.gpuBudget")}
          value={formatMemory(hardware.gpu_budget)}
        />
      )}
    </>
  );
}

export function TierBadge({ tier }: { tier: SystemReport["tier"] }) {
  const { t } = useTranslation();
  return (
    <div className="grid gap-1">
      <span className="w-fit rounded-full bg-accent-soft px-3 py-0.5 text-sm font-semibold text-accent">
        {t(`tiers.${tier}.name`)}
      </span>
      <span className="text-sm text-muted">{t(`tiers.${tier}.summary`)}</span>
    </div>
  );
}

export function DownloadProgress({ state }: { state: DownloadState }) {
  const { t } = useTranslation();
  const pct = percent(state.bytes_done, state.bytes_total);
  return (
    <div className="grid gap-1">
      <progress
        className="h-2 w-full overflow-hidden rounded-full [&::-moz-progress-bar]:bg-accent [&::-webkit-progress-bar]:bg-paper [&::-webkit-progress-value]:bg-accent"
        max={100}
        value={pct}
        aria-label={t("download.progressLabel")}
      />
      <span className="text-xs text-muted tabular-nums">
        {t("download.progress", {
          done: formatBytes(state.bytes_done),
          total: formatBytes(state.bytes_total),
          pct,
        })}
        {state.rate > 0 && ` · ${t("download.rate", { rate: formatBytes(state.rate) })}`}
      </span>
    </div>
  );
}

export function StatusText({ state }: { state: DownloadState }) {
  const { t } = useTranslation();
  return <span>{t(`download.status.${state.status}`)}</span>;
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <p role="alert" className="rounded-md border border-doubt bg-doubt/20 px-3 py-2 text-sm">
      {message}
    </p>
  );
}
