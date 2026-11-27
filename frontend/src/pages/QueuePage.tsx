import { useEffect, useRef, useState, type ReactNode } from "react";
import { ActivityPanel } from "../features/new-download/ActivityPanel";
import { CollectionProgress } from "../features/new-download/CollectionProgress";
import { QueueJobRow } from "../features/queue/QueueJobRow";
import { findJob, newestSnapshot, QUEUE_STATUS_LABELS } from "../features/queue/queueState";
import { useQueue, type QueueController, type QueueDetail, type QueueView } from "../features/queue/useQueue";
import { canRetryJob, type Job } from "../api/jobs";

function JobRows({ jobs, view, controller, renderDetail }: {
  jobs: Job[];
  view: QueueView;
  controller: QueueController;
  /** Inline panel for a row (History only); Active details stay at the top. */
  renderDetail?: (job: Job) => ReactNode;
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
        >
          {renderDetail?.(job)}
        </QueueJobRow>
      ))}
    </ul>
  );
}

// Only one detail is open at a time: ActivityPanel uses a fixed DOM id.
function JobDetail({
  detail, view, controller, onRetry, onClose, activityExpanded, onActivityExpandedChange,
}: {
  detail: QueueDetail;
  view: QueueView;
  controller: QueueController;
  onRetry: () => void;
  onClose: () => void;
  activityExpanded: boolean;
  onActivityExpandedChange: (expanded: boolean) => void;
}) {
  // Rich, newest-wins snapshot (same one the controller uses for Retry): a
  // stale row never replaces it and `items: null` never drops loaded items.
  const job = newestSnapshot(findJob(view.lists, detail.id), detail.job);
  const pendingAction = view.pending[detail.id];
  // One live alert: the panel shows only Retry errors placed on it.
  const error = view.errorSurfaces[detail.id] === "detail" ? view.errors[detail.id] : undefined;
  return (
    <section className="job-progress" id="queue-detail" aria-label="Job details" tabIndex={-1}>
      <header className="job-progress-header">
        <h2>{job.title}</h2>
        <span className={`badge badge-status badge-status-${job.status}`}>
          {QUEUE_STATUS_LABELS[job.status]}
        </span>
        {canRetryJob(job) && (
          <button
            type="button"
            className="button-secondary"
            onClick={onRetry}
            disabled={pendingAction != null}
          >
            {pendingAction === "retry" ? "Retrying…" : "Retry"}
          </button>
        )}
        <button type="button" className="button-quiet" onClick={onClose}>
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
        expanded={activityExpanded}
        onExpandedChange={onActivityExpandedChange}
      />
    </section>
  );
}

// Focus is only moved after the user's own close/Retry inside the panel, never
// on background updates.
type FocusRequest = { kind: "toggle" | "panel"; id: string };

export function QueuePage() {
  const { view, controller } = useQueue();
  const { lists } = view;
  const loading = view.listStatus === "loading";
  const rootRef = useRef<HTMLElement>(null);
  const focusRequest = useRef<FocusRequest | null>(null);
  // Activity expanded is UI state of the selected job only: it survives the
  // panel moving between inline/top, and resets when the detail closes
  // (toggle, panel close, 404, same ID reopen) or another job is selected.
  const [activityExpandedId, setActivityExpandedId] = useState<string | null>(null);
  if (activityExpandedId !== null && view.detail?.id !== activityExpandedId) {
    setActivityExpandedId(null);
  }

  // Inline only while the selected row is in History; otherwise (Active row,
  // row absent after reset/pagination) the single panel stays at the top.
  const inlineId = view.detail != null && lists.history.some((job) => job.id === view.detail?.id)
    ? view.detail.id
    : null;

  useEffect(() => {
    const request = focusRequest.current;
    if (!request) return;
    const root = rootRef.current;
    if (request.kind === "toggle") {
      focusRequest.current = null;
      if (view.detail != null) return;
      const toggles = root?.querySelectorAll<HTMLElement>("[data-job-toggle]") ?? [];
      Array.from(toggles).find((node) => node.dataset.jobToggle === request.id)?.focus();
      return;
    }
    if (view.detail?.id !== request.id) {
      focusRequest.current = null;
    } else if (inlineId == null) {
      // Retry relocated the panel to the top: its inline copy (and focus) is gone.
      focusRequest.current = null;
      // The inline panel (and its focus) is gone; only take focus when nothing
      // else holds it, so a control the user moved to meanwhile is kept.
      const active = document.activeElement;
      if (active == null || active === document.body) {
        root?.querySelector<HTMLElement>("#queue-detail")?.focus();
      }
    } else if (view.pending[request.id] == null) {
      focusRequest.current = null;
    }
  });

  function requestFocus(request: FocusRequest) {
    focusRequest.current = request;
  }

  function renderDetail(detail: QueueDetail, inline: boolean) {
    return (
      <JobDetail
        detail={detail}
        view={view}
        controller={controller}
        activityExpanded={activityExpandedId === detail.id}
        onActivityExpandedChange={(expanded) => setActivityExpandedId(expanded ? detail.id : null)}
        onRetry={() => {
          if (inline) requestFocus({ kind: "panel", id: detail.id });
          controller.retry(detail.id, "detail");
        }}
        onClose={() => {
          if (inline) requestFocus({ kind: "toggle", id: detail.id });
          controller.closeDetail();
        }}
      />
    );
  }

  return (
    <section className="queue-page" ref={rootRef}>
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

      {view.detail && inlineId == null && renderDetail(view.detail, false)}

      <section className="queue-section" aria-label="Active and queued">
        <h2>Active and queued</h2>
        {lists.active.length > 0
          ? <JobRows jobs={lists.active} view={view} controller={controller} />
          : !loading && <p className="queue-empty">No active or queued downloads.</p>}
      </section>

      <section className="queue-section" aria-label="History">
        <h2>History</h2>
        {lists.history.length > 0
          ? (
            <JobRows
              jobs={lists.history}
              view={view}
              controller={controller}
              renderDetail={(job) => (
                view.detail && job.id === inlineId ? renderDetail(view.detail, true) : null
              )}
            />
          )
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
