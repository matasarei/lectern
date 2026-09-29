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

// --- projects ------------------------------------------------------------------------

export interface Project {
  id: number;
  name: string;
  folder: string;
  created_at: string | null;
  first_chat_id?: number | null;
}

export interface SessionSummary {
  id: number;
  kind: "chat";
  title: string;
  messages: number;
  created_at: string;
  updated_at: string;
}

export interface Artifact {
  id: number;
  name: string;
  kind: "imported" | "generated";
  mime: string;
  size: number;
  text: "none" | "indexed" | "failed";
  text_note: string | null;
  from: {
    kind: "import" | "session" | "artifact";
    id: number | null;
    note: string | null;
    title: string | null;
  };
  created_at: string;
  modified_at: string;
}

export interface ProjectItems {
  sessions: SessionSummary[];
  artifacts: Artifact[];
}

export interface SearchHit {
  artifact_id: number;
  name: string;
  ref: string;
  heading: string;
  snippet: string;
}

export interface Tab {
  kind: "session" | "artifact";
  id: number;
}

export interface Tabs {
  open: Tab[];
  active: Tab | null; // null: Overview
}

// --- chat ----------------------------------------------------------------------------

export interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant" | "tool";
  content: string;
  tool_calls: ToolCall[] | null;
  tool_call_id: string | null;
  tool_name: string | null;
  attachments: { id: number; name: string | null }[];
  created_at: string;
}

export interface Chat {
  id: number;
  kind: "chat";
  title: string;
  state: "idle" | "running" | "waiting";
  messages: ChatMessage[];
  pending: { confirmation_id: number; call_id: string; name: string }[];
}

export interface ToolRef {
  kind: "artifact" | "session";
  id: number;
  name: string;
  ref: string | null;
}

/** What the chat's event stream sends. Every event also has `seq`. */
export type ChatEvent = { seq: number } & (
  | { type: "turn"; status: "running"; message_id: number; text: string; attachments: number[] }
  | { type: "turn"; status: "done" | "error" | "stopped" }
  | { type: "model"; model_id: string; ctx: number }
  | { type: "usage"; parts: Record<string, number>; budget: number }
  | { type: "context"; left_out: number; released: number; stubbed_files: string[] }
  | { type: "delta"; text: string }
  | { type: "assistant"; message_id: number; tool_calls: ToolCall[] }
  | { type: "tool"; call_id: string; name: string; arguments: string }
  | { type: "confirm"; confirmation_id: number; call_id: string; name: string; text: string }
  | { type: "confirmed"; confirmation_id: number; approved: boolean }
  | {
      type: "tool_result";
      call_id: string;
      text: string;
      error: boolean;
      refs: ToolRef[];
      action: { type: "open_tab"; tab: Tab } | null;
    }
  | { type: "done"; ttft: number | null; tps: number | null; tokens: number }
  | { type: "error"; detail: string }
  | { type: "stopped" }
  | { type: "replayed" } // sent once, where the replay of the current turn ends; seq is 0
);
