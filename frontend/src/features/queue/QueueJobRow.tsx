import type { ReactNode } from "react";
import { canRetryJob, isActiveJob, type Job } from "../../api/jobs";
import { formatFileSize } from "../new-download/format";
import { QUEUE_STATUS_LABELS } from "./queueState";
import type { QueueAction } from "./useQueue";

interface QueueJobRowProps {
  job: Job;
  pendingAction: QueueAction | undefined;
  error: string | undefined;
  detailOpen: boolean;
  onCancel: () => void;
  onRetry: () => void;
  onToggleDetail: () => void;
  /** Inline detail panel (History only), rendered beneath the row summary. */
  children?: ReactNode;
}

/** Compact row: no per-row Activity or children requests until Details opens. */
export function QueueJobRow({
  job,
  pendingAction,
  error,
  detailOpen,
  onCancel,
  onRetry,
  onToggleDetail,
  children,
}: QueueJobRowProps) {
  const active = isActiveJob(job.status);
  const collection = job.job_type === "collection";
  const value = Math.min(Math.max(job.progress.percent, 0), 100);
  // Same display rule as JobProgress: never round an incomplete collection up.
  const percent = collection
    ? String(Math.min(Number(value.toFixed(1)), value < 100 ? 99.9 : 100))
    : value.toFixed(0);
  const speed = job.progress.speed_bytes_per_second != null &&
    job.progress.speed_bytes_per_second > 0
    ? formatFileSize(job.progress.speed_bytes_per_second)
    : null;
  const busy = pendingAction != null;

  return (
    <li className="queue-row" aria-label={job.title}>
      <div className="queue-row-summary">
        <div className="queue-row-main">
          <span className="queue-row-title">{job.title}</span>
          <span className={`badge badge-status badge-status-${job.status}`}>
            {QUEUE_STATUS_LABELS[job.status]}
          </span>
        </div>
        {active && (
          <p className="job-progress-meta">
            <span>{percent}%</span>
            {collection && (
              <span>
                {job.completed_items ?? "?"} / {job.total_items ?? "?"} items
              </span>
            )}
            {speed && <span>{speed}/s</span>}
          </p>
        )}
        {job.status === "failed" && job.error && (
          <p className="job-result job-error">{job.error.message}</p>
        )}
        {error && <p className="job-result job-error" role="alert">{error}</p>}
        <div className="queue-row-actions">
          {active && (
            <button type="button" className="button-danger" onClick={onCancel} disabled={busy}>
              {pendingAction === "cancel" ? "Cancelling…" : "Cancel"}
            </button>
          )}
          {canRetryJob(job) && (
            <button type="button" className="button-secondary" onClick={onRetry} disabled={busy}>
              {pendingAction === "retry" ? "Retrying…" : "Retry"}
            </button>
          )}
          <button
            type="button"
            className="button-quiet"
            aria-expanded={detailOpen}
            aria-controls={detailOpen ? "queue-detail" : undefined}
            onClick={onToggleDetail}
          >
            {detailOpen ? "Hide details" : "Details"}
          </button>
        </div>
      </div>
      {children}
    </li>
  );
}
