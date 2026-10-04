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
}

export interface CreateJobRequest {
  analysis_id: string;
  variant_id: string;
  destination: "app_library";
}

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
