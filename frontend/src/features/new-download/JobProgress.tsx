import type { ActivityEvent } from "../../api/activity";
import type { Job, JobStatus } from "../../api/jobs";
import { isActiveJob } from "../../api/jobs";
import { ActivityPanel } from "./ActivityPanel";
import type { ActivityLoadStatus } from "./activity";
import { formatFileSize, formatDuration } from "./format";

interface JobProgressProps {
  job: Job;
  isCancelling: boolean;
  onCancel: () => void;
  activityEvents: ActivityEvent[];
  activityLoadStatus: ActivityLoadStatus;
  onRefreshActivity: () => void;
}

const STATUS_LABELS: Record<JobStatus, string> = {
  queued: "Queued",
  analyzing: "Analyzing",
  downloading: "Downloading",
  post_processing: "Post-processing",
  finalizing: "Finalizing",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

function speedLabel(bytesPerSecond: number | null): string | null {
  if (bytesPerSecond == null || bytesPerSecond <= 0) return null;
  const size = formatFileSize(bytesPerSecond);
  return size ? `${size}/s` : null;
}

export function JobProgress({
  job,
  isCancelling,
  onCancel,
  activityEvents,
  activityLoadStatus,
  onRefreshActivity,
}: JobProgressProps) {
  const percent = Math.min(Math.max(job.progress.percent, 0), 100).toFixed(0);
  const speed = speedLabel(job.progress.speed_bytes_per_second);
  const eta = formatDuration(job.progress.eta_seconds);
  const size =
    job.progress.downloaded_bytes != null
      ? formatFileSize(job.progress.downloaded_bytes)
      : null;
  const totalSize =
    job.progress.total_bytes != null ? formatFileSize(job.progress.total_bytes) : null;

  return (
    <section className="job-progress" aria-label="Download progress">
      <header className="job-progress-header">
        <h2>{job.title}</h2>
        <span className={`badge badge-status badge-status-${job.status}`}>
          {STATUS_LABELS[job.status]}
        </span>
      </header>

      {isActiveJob(job.status) && (
        <>
          <div
            className="progress-bar"
            role="progressbar"
            aria-valuenow={Number(percent)}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div className="progress-fill" style={{ width: `${percent}%` }} />
          </div>
          <p className="job-progress-meta">
            <span>{percent}%</span>
            {size && totalSize && (
              <span>
                {size} / {totalSize}
              </span>
            )}
            {speed && <span>{speed}</span>}
            {eta && <span>ETA {eta}</span>}
          </p>
          <button type="button" className="button-danger" onClick={onCancel} disabled={isCancelling}>
            {isCancelling ? "Cancelling…" : "Cancel"}
          </button>
        </>
      )}

      {job.status === "completed" && (
        <p className="job-result" role="status">
          Saved to App Library{job.final_path ? ` — ${job.final_path}` : ""}
        </p>
      )}
      {job.status === "failed" && job.error && (
        <p className="job-result job-error" role="alert">
          {job.error.message}
        </p>
      )}
      {job.status === "cancelled" && (
        <p className="job-result">Download cancelled.</p>
      )}

      <ActivityPanel
        key={job.id}
        events={activityEvents}
        loadStatus={activityLoadStatus}
        onRefresh={onRefreshActivity}
      />
    </section>
  );
}
