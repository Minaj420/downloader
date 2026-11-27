import {
  isActiveJob,
  type Job,
  type JobStatus,
  type ScopedJobList,
} from "../../api/jobs";

/** Terminal statuses served by `scope=history`; unknown statuses are in neither list. */
export const HISTORY_JOB_STATUSES: readonly JobStatus[] = ["completed", "failed", "cancelled"];

export function isHistoryJob(status: JobStatus): boolean {
  return HISTORY_JOB_STATUSES.includes(status);
}

export const QUEUE_STATUS_LABELS: Record<JobStatus, string> = {
  queued: "Queued",
  analyzing: "Analyzing",
  downloading: "Downloading",
  post_processing: "Post-processing",
  finalizing: "Finalizing",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

/**
 * The two disjoint Queue lists (ARCHITECTURE §59 Q1): `active` sorted by
 * `created_at ASC, id ASC`, `history` by `created_at DESC, id DESC`; one ID is
 * in at most one list. `nextCursor` is the server-issued history cursor.
 */
export interface QueueLists {
  active: Job[];
  history: Job[];
  nextCursor: string | null;
}

export const EMPTY_QUEUE_LISTS: QueueLists = { active: [], history: [], nextCursor: null };

// Queue-local copy of the NewDownloadPage helper (same precision rule):
// preserve backend sub-millisecond precision (Date.parse alone discards it).
function timeKey(value: string): string {
  const fraction = value.match(/\.(\d+)/)?.[1] ?? "";
  return new Date(value).toISOString().slice(0, 19) + fraction.padEnd(9, "0");
}

export function snapshotTime(job: Job): string {
  return timeKey(job.updated_at);
}

export function compareActive(a: Job, b: Job): number {
  const left = timeKey(a.created_at);
  const right = timeKey(b.created_at);
  if (left !== right) return left < right ? -1 : 1;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

export function compareHistory(a: Job, b: Job): number {
  return compareActive(b, a);
}

/**
 * Newest-wins decision for one ID (rule 1). Returns the snapshot to keep, or
 * null when the incoming one loses. Equal times keep the current snapshot,
 * except a same-status REST (`rich`) collection response may add its items.
 * Collection snapshots with `items == null` never delete loaded items (rule 6).
 */
export function decideSnapshot(
  current: Job | undefined,
  incoming: Job,
  rich: boolean,
): Job | null {
  if (current == null) return incoming;
  const time = snapshotTime(incoming);
  const currentTime = snapshotTime(current);
  if (rich && incoming.job_type === "collection" && incoming.items != null &&
    time === currentTime && incoming.status === current.status) {
    return { ...current, items: incoming.items };
  }
  if (time <= currentTime) return null;
  return incoming.job_type === "collection" && incoming.items == null
    ? { ...incoming, items: current.items }
    : incoming;
}

/**
 * The winning snapshot for the open detail's ID across its list row and the
 * detail record: the existing newest-wins comparison (rule 1), so an older
 * row never wins and `items: null` never drops the detail's loaded items
 * (rule 6). Equal time keeps the detail record.
 */
export function newestSnapshot(row: Job | undefined, detail: Job): Job {
  return row == null ? detail : decideSnapshot(detail, row, false) ?? detail;
}

export function findJob(lists: QueueLists, id: string): Job | undefined {
  return lists.active.find((job) => job.id === id) ??
    lists.history.find((job) => job.id === id);
}

/** Rebuilds both lists from one snapshot per ID; placement follows status (rule 2). */
function build(jobs: Iterable<Job>, nextCursor: string | null): QueueLists {
  const active: Job[] = [];
  const history: Job[] = [];
  for (const job of jobs) {
    if (isActiveJob(job.status)) active.push(job);
    else if (isHistoryJob(job.status)) history.push(job);
  }
  return { active: active.sort(compareActive), history: history.sort(compareHistory), nextCursor };
}

function byId(jobs: Iterable<Job>): Map<string, Job> {
  const map = new Map<string, Job>();
  for (const job of jobs) map.set(job.id, job);
  return map;
}

/** Applies fresh REST rows newest-wins onto `known`, inserting unknown IDs. */
function considerFresh(known: Map<string, Job>, fresh: readonly Job[], prior?: Job): void {
  for (const job of fresh) {
    const current = withPrior(known.get(job.id), job.id, prior);
    known.set(job.id, decideSnapshot(current, job, true) ?? (current as Job));
  }
}

/**
 * Comparison base for one ID: the held list snapshot and the open detail's
 * snapshot (`prior`), the newer one by `newestSnapshot`. The detail only
 * serves as a base; it never adds an ID to a list by itself.
 */
function withPrior(held: Job | undefined, id: string, prior: Job | undefined): Job | undefined {
  if (prior == null || prior.id !== id) return held;
  return held == null ? prior : newestSnapshot(held, prior);
}

/** Lets an already listed row follow a strictly newer open-detail snapshot. */
function alignPrior(known: Map<string, Job>, prior: Job | undefined): void {
  const held = prior == null ? undefined : known.get(prior.id);
  if (prior != null && held != null) known.set(prior.id, newestSnapshot(held, prior));
}

export interface SnapshotResult {
  lists: QueueLists;
  accepted: boolean;
  known: boolean;
}

/**
 * One snapshot (SSE, action response or detail GET). SSE is not owner-scoped,
 * so callers pass `insert: false` for it: an unknown ID never adds a row.
 */
export function applySnapshot(
  lists: QueueLists,
  incoming: Job,
  options: { rich: boolean; insert: boolean },
): SnapshotResult {
  const current = findJob(lists, incoming.id);
  if (current == null && !options.insert) return { lists, accepted: false, known: false };
  const next = decideSnapshot(current, incoming, options.rich);
  if (next == null) return { lists, accepted: false, known: true };
  const all = byId([...lists.active, ...lists.history]);
  all.set(next.id, next);
  return { lists: build(all.values(), lists.nextCursor), accepted: true, known: current != null };
}

export function removeJob(lists: QueueLists, id: string): QueueLists {
  if (findJob(lists, id) == null) return lists;
  return {
    active: lists.active.filter((job) => job.id !== id),
    history: lists.history.filter((job) => job.id !== id),
    nextCursor: lists.nextCursor,
  };
}

export interface ReconcileResult {
  lists: QueueLists;
  /** Locally active IDs missing from both fresh responses (re-read via GET). */
  missingActiveIds: string[];
  /** True when the history was replaced by the fresh head (reset). */
  reset: boolean;
}

function missingActive(lists: QueueLists, active: readonly Job[], head: readonly Job[]): string[] {
  const fresh = new Set([...active, ...head].map((job) => job.id));
  return lists.active.filter((job) => !fresh.has(job.id)).map((job) => job.id);
}

/**
 * Reset (rule 4): fresh active + history head applied newest-wins against
 * local snapshots; history becomes exactly the head membership and its cursor
 * (deeper loaded pages are dropped). The snapshot winner decides the status,
 * but a terminal winner outside the head is not a reset history row: it
 * leaves both lists. Local active rows stay until re-read.
 */
export function applyReset(
  lists: QueueLists,
  active: readonly Job[],
  head: ScopedJobList,
  prior?: Job,
): ReconcileResult {
  const local = byId([...lists.active, ...lists.history]);
  const headIds = new Set(head.jobs.map((job) => job.id));
  const known = new Map<string, Job>();
  for (const job of [...active, ...head.jobs]) {
    const current = known.get(job.id) ?? withPrior(local.get(job.id), job.id, prior);
    known.set(job.id, decideSnapshot(current, job, true) ?? (current as Job));
  }
  for (const job of lists.active) {
    if (!known.has(job.id)) known.set(job.id, withPrior(job, job.id, prior) as Job);
  }
  const kept = [...known.values()].filter((job) => isActiveJob(job.status) || headIds.has(job.id));
  const next = build(kept, head.next_cursor);
  return { lists: next, missingActiveIds: missingActive(next, active, head.jobs), reset: true };
}

/**
 * A missing-active `GET /jobs/{id}` result (rule 4), REST so `rich`, never
 * inserting an unknown ID. With `historyInsert` (a current-generation merge)
 * it is a normal rule 1/2 snapshot. Otherwise (reset-related or a later
 * generation) a winning terminal snapshot for a row outside the history
 * removes the row instead of adding it, so reset history keeps exactly the
 * head membership.
 */
export function applyReadSnapshot(
  lists: QueueLists,
  incoming: Job,
  historyInsert: boolean,
): QueueLists {
  const current = findJob(lists, incoming.id);
  if (current == null) return lists;
  if (historyInsert || isActiveJob(incoming.status) ||
    lists.history.some((job) => job.id === incoming.id)) {
    return applySnapshot(lists, incoming, { rich: true, insert: false }).lists;
  }
  return decideSnapshot(current, incoming, true) == null ? lists : removeJob(lists, incoming.id);
}

/**
 * Merge (rule 4): active + head merged newest-wins, loaded pages and the
 * cursor kept — except a possible gap (rule 7: the fresh head has a cursor
 * and its last row is not loaded), which is a reset to that head.
 */
export function applyMerge(
  lists: QueueLists,
  active: readonly Job[],
  head: ScopedJobList,
  prior?: Job,
): ReconcileResult {
  const last = head.jobs[head.jobs.length - 1];
  if (head.next_cursor != null && last != null &&
    !lists.history.some((job) => job.id === last.id)) {
    return applyReset(lists, active, head, prior);
  }
  const known = byId([...lists.active, ...lists.history]);
  alignPrior(known, prior);
  considerFresh(known, [...active, ...head.jobs], prior);
  const next = build(known.values(), lists.nextCursor);
  return { lists: next, missingActiveIds: missingActive(next, active, head.jobs), reset: false };
}

/** A Load more page (already generation/cursor guarded): dedup, sort, new cursor. */
export function applyHistoryPage(lists: QueueLists, page: ScopedJobList, prior?: Job): QueueLists {
  const known = byId([...lists.active, ...lists.history]);
  alignPrior(known, prior);
  considerFresh(known, page.jobs, prior);
  return build(known.values(), page.next_cursor);
}
