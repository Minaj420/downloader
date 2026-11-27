import { ActivityPanel } from "../features/new-download/ActivityPanel";
import { CollectionProgress } from "../features/new-download/CollectionProgress";
import { QueueJobRow } from "../features/queue/QueueJobRow";
import { findJob, newestSnapshot, QUEUE_STATUS_LABELS } from "../features/queue/queueState";
import { useQueue, type QueueController, type QueueDetail, type QueueView } from "../features/queue/useQueue";
import { canRetryJob, type Job } from "../api/jobs";

function JobRows({ jobs, view, controller }: {
  jobs: Job[];
  view: QueueView;
  controller: QueueController;
}) {
  return (
    <ul className="queue-list">
      {jobs.map((job) => (
        <QueueJobRow
          key={job.id}
          job={job}
          pendingAction={view.pending[job.id]}
          error={view.errorSurfaces[job.id] === "detail" ? undefined : view.errors[job.id]}
          detailOpen={view.detail?.id === job.id}
          onCancel={() => controller.cancel(job.id)}
          onRetry={() => controller.retry(job.id, "row")}
          onToggleDetail={() => controller.openDetail(job.id)}
        />
      ))}
    </ul>
  );
}

// Only one detail is open at a time: ActivityPanel uses a fixed DOM id.
function JobDetail({ detail, view, controller }: {
  detail: QueueDetail;
  view: QueueView;
  controller: QueueController;
}) {
  // Rich, newest-wins snapshot (same one the controller uses for Retry): a
  // stale row never replaces it and `items: null` never drops loaded items.
  const job = newestSnapshot(findJob(view.lists, detail.id), detail.job);
  const pendingAction = view.pending[detail.id];
  // One live alert: the panel shows only Retry errors placed on it.
  const error = view.errorSurfaces[detail.id] === "detail" ? view.errors[detail.id] : undefined;
  return (
    <section className="job-progress" id="queue-detail" aria-label="Job details">
      <header className="job-progress-header">
        <h2>{job.title}</h2>
        <span className={`badge badge-status badge-status-${job.status}`}>
          {QUEUE_STATUS_LABELS[job.status]}
        </span>
        {canRetryJob(job) && (
          <button
            type="button"
            className="button-secondary"
            onClick={() => controller.retry(detail.id, "detail")}
            disabled={pendingAction != null}
          >
            {pendingAction === "retry" ? "Retrying…" : "Retry"}
          </button>
        )}
        <button type="button" className="button-quiet" onClick={() => controller.closeDetail()}>
          Close details
        </button>
      </header>
      {error && <p className="job-result job-error" role="alert">{error}</p>}
      {job.status === "completed" && (
        <p className="job-result">
          Saved to App Library{job.final_path ? ` — ${job.final_path}` : ""}
        </p>
      )}
      {job.status === "failed" && job.error && (
        <p className="job-result job-error">{job.error.message}</p>
      )}
      {job.status === "cancelled" && <p className="job-result">Download cancelled.</p>}
      {job.job_type === "collection" && (
        <CollectionProgress job={job} loadStatus={detail.itemsStatus} />
      )}
      <ActivityPanel
        key={job.id}
        events={detail.events}
        loadStatus={detail.activityStatus}
        onRefresh={() => controller.refreshActivity()}
      />
    </section>
  );
}

export function QueuePage() {
  const { view, controller } = useQueue();
  const { lists } = view;
  const loading = view.listStatus === "loading";

  return (
    <section className="queue-page">
      <h1>Queue</h1>
      {view.listError && (
        <div className="error-banner" role="alert">
          <p>{view.listError}</p>
          <button
            type="button"
            className="button-secondary"
            onClick={() => controller.requestReconcile("reset")}
          >
            Try again
          </button>
        </div>
      )}
      {loading && !view.listError && <p role="status">Loading queue…</p>}

      {view.detail && <JobDetail detail={view.detail} view={view} controller={controller} />}

      <section className="queue-section" aria-label="Active and queued">
        <h2>Active and queued</h2>
        {lists.active.length > 0
          ? <JobRows jobs={lists.active} view={view} controller={controller} />
          : !loading && <p className="queue-empty">No active or queued downloads.</p>}
      </section>

      <section className="queue-section" aria-label="History">
        <h2>History</h2>
        {lists.history.length > 0
          ? <JobRows jobs={lists.history} view={view} controller={controller} />
          : !loading && <p className="queue-empty">No finished downloads yet.</p>}
        {view.loadMoreStatus === "error" && (
          <p className="job-result job-error" role="alert">Could not load more history.</p>
        )}
        {lists.nextCursor != null && (
          <button
            type="button"
            className="button-secondary queue-load-more"
            onClick={() => controller.loadMore()}
            disabled={view.loadMoreStatus === "loading"}
          >
            {view.loadMoreStatus === "loading" ? "Loading…" : "Load more"}
          </button>
        )}
      </section>
    </section>
  );
}
