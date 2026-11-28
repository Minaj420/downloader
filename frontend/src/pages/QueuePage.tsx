import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ActivityPanel } from "../features/new-download/ActivityPanel";
import { CollectionProgress } from "../features/new-download/CollectionProgress";
import { QueueJobRow } from "../features/queue/QueueJobRow";
import { findJob, newestSnapshot, QUEUE_STATUS_LABELS } from "../features/queue/queueState";
import {
  findDetail,
  jobsLabel,
  useQueue,
  type ClearHistoryPhase,
  type ClearHistoryView,
  type QueueController,
  type QueueDetail,
  type QueueView,
} from "../features/queue/useQueue";
import { canRetryJob, type Job } from "../api/jobs";

// Several details can be on the page at once: DOM ids are per job ID (one
// open detail per ID), so every row toggle/Activity toggle controls its own.
const detailDomId = (id: string) => `queue-detail-${id}`;
const activityDomId = (id: string) => `queue-activity-${id}`;

function JobRows({ jobs, view, controller, renderDetail }: {
  jobs: Job[];
  view: QueueView;
  controller: QueueController;
  /** Inline panel for a row (History only); the Active detail stays at the top. */
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
          detailOpen={findDetail(view, job.id) != null}
          detailId={detailDomId(job.id)}
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

function JobDetail({
  detail, view, controller, onRetry, onClose, activityExpanded, onActivityExpandedChange,
}: {
  detail: QueueDetail;
  view: QueueView;
  controller: QueueController;
  onRetry: () => void;
  /** Top panel only; inline History details close via the row's Hide details. */
  onClose?: () => void;
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
    <section className="job-progress" id={detailDomId(detail.id)} aria-label="Job details" tabIndex={-1}>
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
        {onClose && (
          <button type="button" className="button-quiet" onClick={onClose}>
            Close details
          </button>
        )}
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
        key={detail.slot}
        events={detail.events}
        loadStatus={detail.activityStatus}
        onRefresh={() => controller.refreshActivity(detail.id)}
        expanded={activityExpanded}
        onExpandedChange={onActivityExpandedChange}
        listId={activityDomId(detail.id)}
      />
    </section>
  );
}

/**
 * Clear History: one preview (server `job_count` only) and one inline
 * explicit confirm. Clear phases change only through the user's own actions
 * and their responses, so focus moves only when it is inside this group (or
 * lost to the body): to "Keep history" when an attempt starts, back to the
 * trigger when it ends.
 */
function ClearHistory({ clear, controller }: { clear: ClearHistoryView; controller: QueueController }) {
  const groupRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const previous = useRef<ClearHistoryPhase>(clear.phase);
  const { phase, jobCount, message } = clear;
  const locked = phase !== "idle";

  useEffect(() => {
    const before = previous.current;
    previous.current = phase;
    if (before === phase) return;
    const active = document.activeElement;
    const ours = active == null || active === document.body || groupRef.current?.contains(active) === true;
    if (!ours) return;
    if (before === "idle") keepRef.current?.focus();
    else if (phase === "idle") triggerRef.current?.focus();
  }, [phase]);

  // The count shown is the one bound to the current preview token.
  const count = phase === "ready" || phase === "confirming" ? jobCount : null;
  // One persistent live region: waiting text, then the server count, then a
  // non-error result, updated in place (never inserted per phase).
  const statusText = phase === "previewing"
    ? "Counting history jobs…"
    : count != null
      ? `Clear History will delete ${jobsLabel(count)}. This cannot be undone.`
      : message?.kind === "status" ? message.text : "";
  const error = message?.kind === "error" ? message : null;
  return (
    <div ref={groupRef} role="group" aria-label="Clear History">
      <label className="format-option">
        <input
          type="checkbox"
          checked={clear.includeCancelled}
          disabled={locked}
          onChange={(event) => controller.setClearIncludeCancelled(event.target.checked)}
        />
        Include cancelled jobs
      </label>
      {/* First action row: fixed texts only, so "Keep history" (one DOM
          button from pending to result) never moves when the response
          arrives; variable content and Delete come below it. */}
      <div className="form-actions">
        <button
          ref={triggerRef}
          type="button"
          className="button-secondary"
          onClick={() => controller.startClearHistory()}
          disabled={locked}
        >
          Clear History
        </button>
        {locked && (
          <button
            ref={keepRef}
            type="button"
            className="button-secondary"
            onClick={() => controller.dismissClearHistory()}
            disabled={phase === "confirming"}
          >
            Keep history
          </button>
        )}
      </div>
      <p className="job-result" role="status" id="queue-clear-summary">{statusText}</p>
      {count != null && (
        <div className="form-actions">
          <button
            type="button"
            className="button-danger"
            aria-describedby="queue-clear-summary"
            onClick={() => controller.confirmClearHistory()}
            disabled={phase === "confirming"}
          >
            {phase === "confirming" ? "Deleting…" : `Delete ${jobsLabel(count)}`}
          </button>
        </div>
      )}
      {error && <p className="job-result job-error" role="alert">{error.text}</p>}
      {error?.refresh === true && (
        <div className="form-actions">
          <button
            type="button"
            className="button-secondary"
            onClick={() => controller.requestReconcile("reset")}
          >
            Refresh queue
          </button>
        </div>
      )}
    </div>
  );
}

// Focus is only moved after the user's own Retry inside an inline panel,
// never on background updates.
type FocusRequest = { kind: "panel"; id: string };

export function QueuePage() {
  const { view, controller } = useQueue();
  const { lists } = view;
  const loading = view.listStatus === "loading";
  const focusRequest = useRef<FocusRequest | null>(null);
  // Activity expanded is UI state of one opening (detail slot): it survives
  // the panel moving between inline/top, and is collapsed again on any new
  // opening (toggle, panel close, 404, same ID reopen). Each detail is
  // independent of the others.
  const [expandedSlots, setExpandedSlots] = useState<ReadonlySet<number>>(() => new Set());

  // The controller keeps the top invariant (never a History-row detail).
  const top = view.topId == null ? undefined : findDetail(view, view.topId);

  useEffect(() => {
    const request = focusRequest.current;
    if (!request) return;
    const inline = lists.history.some((job) => job.id === request.id);
    if (findDetail(view, request.id) == null) {
      focusRequest.current = null;
    } else if (!inline) {
      focusRequest.current = null;
      // Retry relocated the panel to the top: its inline copy (and focus) is
      // gone; only take focus when nothing else holds it, so a control the
      // user moved to meanwhile is kept.
      const active = document.activeElement;
      if (view.topId === request.id && (active == null || active === document.body)) {
        document.getElementById(detailDomId(request.id))?.focus();
      }
    } else if (view.pending[request.id] == null) {
      focusRequest.current = null;
    }
  });

  // Event handler only (panel Retry click); never called during render.
  const retryFromPanel = useCallback((id: string, inline: boolean) => {
    if (inline) focusRequest.current = { kind: "panel", id };
    controller.retry(id, "detail");
  }, [controller]);

  function setActivityExpanded(slot: number, expanded: boolean) {
    const open = new Set(view.details.map((detail) => detail.slot));
    setExpandedSlots((previous) => {
      const next = new Set([...previous].filter((entry) => open.has(entry) && entry !== slot));
      if (expanded) next.add(slot);
      return next;
    });
  }

  function renderDetail(detail: QueueDetail, inline: boolean) {
    return (
      <JobDetail
        key={detail.slot}
        detail={detail}
        view={view}
        controller={controller}
        activityExpanded={expandedSlots.has(detail.slot)}
        onActivityExpandedChange={(expanded) => setActivityExpanded(detail.slot, expanded)}
        onRetry={() => retryFromPanel(detail.id, inline)}
        onClose={inline ? undefined : () => controller.closeDetail(detail.id)}
      />
    );
  }

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

      {top && renderDetail(top, false)}

      <section className="queue-section" aria-label="Active and queued">
        <h2>Active and queued</h2>
        {lists.active.length > 0
          ? <JobRows jobs={lists.active} view={view} controller={controller} />
          : !loading && <p className="queue-empty">No active or queued downloads.</p>}
      </section>

      <section className="queue-section" aria-label="History">
        <h2>History</h2>
        <ClearHistory clear={view.clear} controller={controller} />
        {lists.history.length > 0
          ? (
            <JobRows
              jobs={lists.history}
              view={view}
              controller={controller}
              renderDetail={(job) => {
                const detail = findDetail(view, job.id);
                return detail ? renderDetail(detail, true) : null;
              }}
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
