import { apiFetch } from "./client";
import type { Artifact } from "./types";

/** Import one file into a project. The body is the file itself, streamed by the browser. */
export function uploadFile(projectId: number, file: File): Promise<Artifact> {
  return apiFetch<Artifact>(
    `/api/projects/${projectId}/files?name=${encodeURIComponent(file.name)}`,
    { method: "PUT", body: file },
  );
}
