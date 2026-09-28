import { apiGet } from "./client";

/** One structured activity row; same field set as the SSE payload. */
export interface ActivityEvent {
  id: number;
  job_id: string;
  level: string;
  event_type: string;
  message: string;
  created_at: string;
}

interface ActivityResponse {
  events: ActivityEvent[];
}

/**
 * Full activity history for one job, id ASC.
 *
 * The SSE stream replays nothing on first connect, so this REST resource
 * is the initial-history source for the Activity panel (and the refresh
 * fallback after an SSE drop).
 */
export async function getJobActivity(jobId: string): Promise<ActivityEvent[]> {
  const response = await apiGet<ActivityResponse>(
    `/api/v1/jobs/${jobId}/activity`,
  );
  return response.events;
}
