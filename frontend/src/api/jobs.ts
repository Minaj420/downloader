import { useMutation } from "@tanstack/react-query";
import { apiGet, apiPost } from "./client";

export type JobStatus =
  | "queued"
  | "analyzing"
  | "downloading"
  | "post_processing"
  | "finalizing"
  | "completed"
  | "failed"
  | "cancelled";

export const ACTIVE_JOB_STATUSES: readonly JobStatus[] = [
  "queued",
  "analyzing",
  "downloading",
  "post_processing",
  "finalizing",
];

export function isActiveJob(status: JobStatus): boolean {
  return ACTIVE_JOB_STATUSES.includes(status);
}

export interface JobProgress {
  percent: number;
  downloaded_bytes: number | null;
  total_bytes: number | null;
  speed_bytes_per_second: number | null;
  eta_seconds: number | null;
}

export interface JobErrorBody {
  code: string;
  message: string;
  retryable: boolean;
}

export interface JobItem {
  id: string;
  item_id: string;
  source_order: number;
  source_id: string | null;
  source_url: string;
  title: string | null;
  playlist_index: number | null;
  track_number: number | null;
  disc_number: number | null;
  status: JobStatus;
  progress: JobProgress;
  final_path: string | null;
  error: JobErrorBody | null;
}

export interface Job {
  id: string;
  analysis_id: string;
  variant_id: string;
  status: JobStatus;
  title: string;
  source_url: string;
  destination: string;
  progress: JobProgress;
  final_path: string | null;
  error: JobErrorBody | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  // Collection jobs (M5): absent/null on legacy single-job responses.
  job_type?: "single" | "collection";
  collection_mode?: CollectionMode | null;
  collection_profile?: CollectionProfile | null;
  total_items?: number | null;
  completed_items?: number | null;
  failed_items?: number | null;
  items?: JobItem[] | null;
}

export function canRetryJob(job: Job): boolean {
  return Boolean(job.error?.retryable && (job.status === "failed" ||
    (job.job_type === "collection" && job.status === "cancelled")));
}

export type CollectionMode = "audio" | "video";

/** Native source-aware preference; not a conversion or quality promise. */
export interface CollectionProfile {
  kind: "original_best_available";
  max_height?: number | null;
}

/** Existing single-format request: analysis plus one variant id. */
export interface CreateSingleJobRequest {
  analysis_id: string;
  variant_id: string;
  destination: "app_library";
}

/**
 * Collection request: backend snapshot item ids, an explicit collection-level
 * mode and an optional native profile. Mutually exclusive with `variant_id`
 * on the backend wire; no synthetic ids or URLs are ever sent.
 */
export interface CreateCollectionJobRequest {
  analysis_id: string;
  item_ids: string[];
  collection_mode: CollectionMode;
  collection_profile?: CollectionProfile | null;
  destination: "app_library";
}

export type CreateJobRequest = CreateSingleJobRequest | CreateCollectionJobRequest;

export function useCreateJob() {
  return useMutation({
    mutationFn: (request: CreateJobRequest) =>
      apiPost<Job, CreateJobRequest>("/api/v1/jobs", request),
  });
}

export async function getJob(jobId: string): Promise<Job> {
  return apiGet<Job>(`/api/v1/jobs/${jobId}`);
}

export async function cancelJob(jobId: string): Promise<Job> {
  return apiPost<Job, Record<string, never>>(`/api/v1/jobs/${jobId}/cancel`, {});
}

export async function retryJob(jobId: string): Promise<Job> {
  return apiPost<Job, Record<string, never>>(`/api/v1/jobs/${jobId}/retry`, {});
}
