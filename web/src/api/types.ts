export type Tier = "below_minimum" | "minimum" | "recommended" | "comfortable";
export type Role = "main" | "ocr" | "ocr.hand" | "code";

export interface Gpu {
  vendor: "nvidia" | "apple" | "amd" | "intel";
  name: string;
  memory_total: number;
  memory_free: number | null;
  unified: boolean;
  cuda_driver: string | null;
}

export interface SystemReport {
  hardware: {
    os: "linux" | "macos" | "windows";
    arch: string;
    cpu: string;
    cpu_cores: number;
    ram_total: number;
    ram_available: number;
    gpus: Gpu[];
    gpu_budget: number;
  };
  tier: Tier;
  disk_free: number;
  sandbox: SandboxStatus;
}

export interface SandboxStatus {
  runtime: "podman" | "docker" | null;
  version: string | null;
  engine_running: boolean;
}

export type DownloadStatus =
  | "none"
  | "queued"
  | "downloading"
  | "verifying"
  | "done"
  | "failed"
  | "paused";

export interface DownloadState {
  model_id: string;
  status: DownloadStatus;
  bytes_done: number;
  bytes_total: number;
  rate: number;
  error: string | null;
}

export interface CatalogModel {
  id: string;
  title: string;
  quant: string;
  roles: Role[];
  size: number;
  licence: string;
  licence_notice: boolean;
  notes: string | null;
  repo: string;
  download: DownloadState;
}

export interface ModelsResponse {
  tier: Tier;
  defaults: Partial<Record<Role, string>>;
  models: CatalogModel[];
}

export interface FirstRun {
  step: "hardware" | "download" | "done";
  chat_model: {
    id: string;
    title: string;
    quant: string;
    size: number;
    licence: string;
    download: DownloadState;
  };
  can_finish: boolean;
}

export interface Resident {
  role: Role;
  model_id: string;
  bytes: number;
  ctx: number;
  kv_type: string;
  on_gpu: boolean;
  leases: number;
  status: "loaded" | "crashed";
  pid: number;
}

export interface RuntimeState {
  tier: Tier;
  budget: number;
  unified: boolean;
  used: number;
  swaps: number;
  waiting: number;
  residents: Resident[];
}

export interface RuntimeSettingsValues {
  mlx_enabled: boolean;
  when_models_dont_fit: "swap_between_phases" | "keep_one_loaded";
  chat_during_job: "pause_job_after_current_item" | "chat_waits";
}

export interface RuntimeSettings {
  values: RuntimeSettingsValues;
  implemented: Record<"when_models_dont_fit" | "chat_during_job", string[]>;
  /** MLX runs only on Apple Silicon Macs; the switch is hidden elsewhere. */
  mlx_supported: boolean;
}
