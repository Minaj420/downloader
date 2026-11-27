import { useEffect, useState } from "react";
import { getJobActivity, type ActivityEvent } from "../../api/activity";
import { ApiError, ApiRequestError } from "../../api/client";
import { subscribeToJobUpdates, type Unsubscribe } from "../../api/events";
import {
  cancelJob,
  canRetryJob,
  getJob,
  isActiveJob,
  listActiveJobs,
  listHistoryJobs,
  retryJob,
  type Job,
} from "../../api/jobs";
import { mergeActivityEvents, type ActivityLoadStatus } from "../new-download/activity";
import type { ItemsLoadStatus } from "../new-download/CollectionProgress";
import {
  applyHistoryPage,
  applyMerge,
  applyReadSnapshot,
  applyReset,
  applySnapshot,
  decideSnapshot,
  EMPTY_QUEUE_LISTS,
  findJob,
  newestSnapshot,
  removeJob,
  snapshotTime,
  type QueueLists,
} from "./queueState";

export const QUEUE_POLL_INTERVAL_MS = 2000;

type ReconcileKind = "merge" | "reset";
export type QueueAction = "cancel" | "retry";
export type QueueListStatus = "loading" | "ready" | "error";
export type LoadMoreStatus = "idle" | "loading" | "error";
/** Where a Retry error is shown; Cancel errors have no entry (row only). */
export type ErrorSurface = "row" | "detail";

/** The single open on-demand detail, keyed by job ID (not list membership). */
export interface QueueDetail {
  id: string;
  job: Job;
  itemsStatus: ItemsLoadStatus;
  events: ActivityEvent[];
  activityStatus: ActivityLoadStatus;
}

export interface QueueView {
  lists: QueueLists;
  listStatus: QueueListStatus;
  listError: string | null;
  loadMoreStatus: LoadMoreStatus;
  sseOpen: boolean;
  pending: Readonly<Record<string, QueueAction>>;
  errors: Readonly<Record<string, string>>;
  /** Transient UI placement of a Retry error (one surface at a time). */
  errorSurfaces: Readonly<Record<string, ErrorSurface>>;
  detail: QueueDetail | null;
}

export const INITIAL_QUEUE_VIEW: QueueView = {
  lists: EMPTY_QUEUE_LISTS,
  listStatus: "loading",
  listError: null,
  loadMoreStatus: "idle",
  sseOpen: false,
  pending: {},
  errors: {},
  errorSurfaces: {},
  detail: null,
};

const LIST_ERROR = "Could not refresh the queue. Check your connection and try again.";
const ACTION_ERRORS: Record<QueueAction, string> = {
  cancel: "Could not cancel the download. Check your connection and try again.",
  retry: "Could not retry the download. Check your connection and try again.",
};

function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

function without<T>(record: Readonly<Record<string, T>>, id: string): Record<string, T> {
  const next = { ...record };
  delete next[id];
  return next;
}

/**
 * Queue page controller implementing the Q1 live-update rules
 * (ARCHITECTURE §59): one global SSE subscription, reset/merge reconciles
 * with a history generation, guarded Load more, on-demand detail and
 * Cancel/Retry without optimistic changes or automatic repeats. Every async
 * continuation checks the session (start/stop) and its own generation.
 */
export class QueueController {
  private view: QueueView = INITIAL_QUEUE_VIEW;
  private readonly publish: (view: QueueView) => void;
  private session = 0;
  private live = false;
  private unsubscribe: Unsubscribe | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private errorHandled = false;
  private generation = 0;
  private reconciling = false;
  private wantedReconcile: ReconcileKind | null = null;
  private loadMoreRequest: { generation: number; cursor: string } | null = null;
  private readonly missingReads = new Set<string>();
  // Synchronous per-ID guard: closes the double-click window before render.
  private readonly pendingActions = new Map<string, QueueAction>();
  private readonly errorFloors = new Map<string, string>();
  private detailEpoch = 0;
  private detailRequest: object | null = null;
  private detailWanted: string | null = null;
  private detailResets = 0;
  private activityGeneration = 0;

  constructor(publish: (view: QueueView) => void) {
    this.publish = publish;
  }

  start(): void {
    this.session += 1;
    this.live = true;
    this.errorHandled = false;
    this.reconciling = false;
    this.wantedReconcile = null;
    this.loadMoreRequest = null;
    this.missingReads.clear();
    this.pendingActions.clear();
    this.detailRequest = null;
    const session = this.session;
    // Subscribe first; every REST read happens after it (mount reset below).
    this.unsubscribe = subscribeToJobUpdates(
      (job) => { if (this.alive(session)) this.handleSnapshot(job); },
      () => { if (this.alive(session)) this.handleError(); },
      (event) => { if (this.alive(session)) this.handleActivity(event); },
      () => { if (this.alive(session)) this.handleOpen(); },
    );
    this.emit({ sseOpen: false, pending: {}, loadMoreStatus: "idle" });
    this.requestReconcile("reset");
  }

  stop(): void {
    this.session += 1;
    this.live = false;
    this.unsubscribe?.();
    this.unsubscribe = null;
    if (this.pollTimer != null) clearInterval(this.pollTimer);
    this.pollTimer = null;
    this.detailEpoch += 1;
    this.activityGeneration += 1;
  }

  private alive(session: number): boolean {
    return this.live && session === this.session;
  }

  private emit(patch: Partial<QueueView>): void {
    this.view = this.alignErrorSurfaces({ ...this.view, ...patch });
    if (!this.live) return;
    this.publish(this.view);
    this.syncPolling();
  }

  // A Retry error stays where the action started. Only when that surface is
  // gone does it move to the matching other one, and it stays there when the
  // first surface comes back (no second live alert, no jumping back).
  private alignErrorSurfaces(view: QueueView): QueueView {
    let surfaces = view.errorSurfaces;
    for (const [id, surface] of Object.entries(view.errorSurfaces)) {
      let next: ErrorSurface | null = surface;
      if (view.errors[id] == null) {
        next = null;
      } else {
        const hasRow = findJob(view.lists, id) != null;
        const hasDetail = view.detail?.id === id;
        if (surface === "row" && !hasRow && hasDetail) next = "detail";
        else if (surface === "detail" && !hasDetail && hasRow) next = "row";
      }
      if (next !== surface) {
        surfaces = next == null ? without(surfaces, id) : { ...surfaces, [id]: next };
      }
    }
    return surfaces === view.errorSurfaces ? view : { ...view, errorSurfaces: surfaces };
  }

  // Rule 5: poll while SSE is not open (even with empty lists) and while
  // SSE is open with a non-empty active list.
  private syncPolling(): void {
    const wanted = this.live && (!this.view.sseOpen || this.view.lists.active.length > 0);
    if (wanted && this.pollTimer == null) {
      this.pollTimer = setInterval(() => this.requestReconcile("merge"), QUEUE_POLL_INTERVAL_MS);
    } else if (!wanted && this.pollTimer != null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  private handleSnapshot(job: Job): void {
    const known = findJob(this.view.lists, job.id) != null;
    this.offer(job, false);
    // Rule 3: SSE is not owner-scoped; an unknown ID only triggers a merge.
    if (!known) this.requestReconcile("merge");
  }

  private handleActivity(event: ActivityEvent): void {
    this.patchDetail(event.job_id, (detail) => ({
      events: mergeActivityEvents(detail.events, [event]),
    }));
    if (findJob(this.view.lists, event.job_id) == null) this.requestReconcile("merge");
  }

  private handleOpen(): void {
    this.errorHandled = false;
    this.emit({ sseOpen: true });
    this.requestReconcile("reset");
  }

  private handleError(): void {
    // Merge once per outage (also before the first open); repeated errors of
    // the same outage are ignored and polling covers it until the next onOpen.
    if (this.errorHandled) return;
    this.errorHandled = true;
    if (this.view.sseOpen) this.emit({ sseOpen: false });
    this.requestReconcile("merge");
  }

  private commitLists(lists: QueueLists): void {
    if (lists === this.view.lists) return;
    this.emit({ lists });
    this.clearSupersededErrors();
  }

  /** Newest-wins for one snapshot into the lists and the open detail. */
  private offer(job: Job, rich: boolean): void {
    this.commitLists(applySnapshot(this.view.lists, job, { rich, insert: false }).lists);
    this.offerDetail(job, rich);
  }

  private offerDetail(job: Job, rich: boolean): void {
    const detail = this.view.detail;
    if (detail?.id !== job.id) return;
    const next = decideSnapshot(detail.job, job, rich);
    if (next == null) return;
    const collection = next.job_type === "collection";
    this.emit({
      detail: {
        ...detail,
        job: next,
        itemsStatus: collection ? (job.items != null ? "loaded" : "loading") : detail.itemsStatus,
      },
    });
    this.clearSupersededErrors();
    // A snapshot without children triggers one coalesced detail read.
    if (collection && job.items == null) void this.refreshDetail();
  }

  private newestTime(id: string): string | null {
    const times = [findJob(this.view.lists, id), this.view.detail?.id === id ? this.view.detail.job : undefined]
      .filter((job): job is Job => job != null)
      .map(snapshotTime)
      .sort();
    return times[times.length - 1] ?? null;
  }

  // An action error clears once a newer snapshot of the same ID is seen.
  private clearSupersededErrors(): void {
    let errors = this.view.errors;
    for (const [id, floor] of this.errorFloors) {
      const newest = this.newestTime(id);
      if (newest != null && newest > floor) {
        this.errorFloors.delete(id);
        errors = without(errors, id);
      }
    }
    if (errors !== this.view.errors) this.emit({ errors });
  }

  requestReconcile(kind: ReconcileKind): void {
    if (!this.live) return;
    // Every reset starts a new history generation right away, so in-flight
    // Load more and older reconcile history results are discarded.
    if (kind === "reset") this.generation += 1;
    if (this.reconciling) {
      this.wantedReconcile = kind === "reset" || this.wantedReconcile === "reset" ? "reset" : "merge";
      return;
    }
    void this.runReconcile(kind);
  }

  private async runReconcile(kind: ReconcileKind): Promise<void> {
    const session = this.session;
    const generation = this.generation;
    this.reconciling = true;
    try {
      const [active, head] = await Promise.all([listActiveJobs(), listHistoryJobs()]);
      if (!this.alive(session)) return;
      if (generation !== this.generation) {
        // Previous generation: active rows only pass through rule 1.
        for (const job of active.jobs) this.offer(job, true);
        return;
      }
      // The open detail's snapshot is only a comparison base (never membership).
      const prior = this.view.detail?.job;
      const result = kind === "reset"
        ? applyReset(this.view.lists, active.jobs, head, prior)
        : applyMerge(this.view.lists, active.jobs, head, prior);
      if (result.reset && kind === "merge") this.generation += 1;
      this.commitLists(result.lists);
      this.emit({
        listStatus: "ready",
        listError: null,
        ...(result.reset ? { loadMoreStatus: "idle" as const } : {}),
      });
      for (const job of [...active.jobs, ...head.jobs]) this.offerDetail(job, true);
      for (const id of result.missingActiveIds) void this.readMissing(id, !result.reset);
      if (result.reset) this.reloadDetail();
    } catch {
      if (this.alive(session) && generation === this.generation) {
        this.emit({
          listError: LIST_ERROR,
          listStatus: this.view.listStatus === "ready" ? "ready" : "error",
        });
      }
    } finally {
      if (this.alive(session)) {
        this.reconciling = false;
        const next = this.wantedReconcile;
        this.wantedReconcile = null;
        if (next != null) void this.runReconcile(next);
      }
    }
  }

  /**
   * One GET per ID in flight; 404 removes the row, never repeats on error.
   * Only a merge read whose generation is still current may move a terminal
   * row into history; reset-related or older-generation reads never add a
   * history row outside the current reset head.
   */
  private async readMissing(id: string, fromMerge: boolean): Promise<void> {
    if (this.missingReads.has(id)) return;
    this.missingReads.add(id);
    const session = this.session;
    const generation = this.generation;
    try {
      const fresh = await getJob(id);
      if (!this.alive(session)) return;
      const historyInsert = fromMerge && generation === this.generation;
      this.commitLists(applyReadSnapshot(this.view.lists, fresh, historyInsert));
      this.offerDetail(fresh, true);
    } catch (error) {
      if (this.alive(session) && isNotFound(error)) {
        this.commitLists(removeJob(this.view.lists, id));
      }
    } finally {
      if (this.alive(session)) this.missingReads.delete(id);
    }
  }

  loadMore(): void {
    const cursor = this.view.lists.nextCursor;
    if (!this.live || cursor == null || this.loadMoreRequest?.generation === this.generation) return;
    const request = { generation: this.generation, cursor };
    this.loadMoreRequest = request;
    void this.runLoadMore(request);
  }

  private async runLoadMore(request: { generation: number; cursor: string }): Promise<void> {
    const session = this.session;
    const current = () => this.alive(session) && request.generation === this.generation &&
      this.view.lists.nextCursor === request.cursor;
    this.emit({ loadMoreStatus: "loading" });
    try {
      const page = await listHistoryJobs({ cursor: request.cursor });
      if (!current()) return;
      this.commitLists(applyHistoryPage(this.view.lists, page, this.view.detail?.job));
      this.emit({ loadMoreStatus: "idle" });
      for (const job of page.jobs) this.offerDetail(job, true);
    } catch {
      // The cursor is kept for a manual retry.
      if (current()) this.emit({ loadMoreStatus: "error" });
    } finally {
      if (this.alive(session) && this.loadMoreRequest === request) {
        this.loadMoreRequest = null;
        if (this.view.loadMoreStatus === "loading") this.emit({ loadMoreStatus: "idle" });
      }
    }
  }

  openDetail(id: string): void {
    if (!this.live) return;
    if (this.view.detail?.id === id) {
      this.closeDetail();
      return;
    }
    const job = findJob(this.view.lists, id);
    if (job == null) return;
    this.detailEpoch += 1;
    this.activityGeneration += 1;
    this.detailRequest = null;
    this.detailWanted = null;
    this.emit({
      detail: {
        id,
        job,
        itemsStatus: job.job_type === "collection" && job.items == null ? "loading" : "loaded",
        events: [],
        activityStatus: "idle",
      },
    });
    void this.refreshDetail();
    void this.refreshActivity();
  }

  closeDetail(): void {
    this.detailEpoch += 1;
    this.activityGeneration += 1;
    this.detailRequest = null;
    this.detailWanted = null;
    this.emit({ detail: null });
  }

  private patchDetail(id: string, patch: (detail: QueueDetail) => Partial<QueueDetail>): void {
    const detail = this.view.detail;
    if (detail?.id === id) this.emit({ detail: { ...detail, ...patch(detail) } });
  }

  private reloadDetail(): void {
    if (this.view.detail == null) return;
    this.detailResets += 1;
    void this.refreshDetail();
    void this.refreshActivity();
  }

  // One detail read in flight; bursts coalesce to the latest key.
  private async refreshDetail(): Promise<void> {
    const detail = this.view.detail;
    if (!this.live || detail == null) return;
    const session = this.session;
    const epoch = this.detailEpoch;
    const keyOf = (open: QueueDetail) => `${open.id}:${snapshotTime(open.job)}:${this.detailResets}`;
    const key = keyOf(detail);
    this.detailWanted = key;
    if (this.detailRequest != null) return;
    const request = {};
    this.detailRequest = request;
    const current = () => this.alive(session) && epoch === this.detailEpoch;
    try {
      const fresh = await getJob(detail.id);
      if (current()) this.offer(fresh, true);
    } catch (error) {
      if (!current()) return;
      if (isNotFound(error)) this.closeDetail();
      // Only a still-current request may flag its items: a newer snapshot or
      // reset since then (e.g. a rich REST response with items) stays as is.
      else if (this.detailWanted === key && this.view.detail != null &&
        keyOf(this.view.detail) === key) {
        this.patchDetail(detail.id, () => ({ itemsStatus: "error" }));
      }
    } finally {
      if (this.detailRequest === request) {
        this.detailRequest = null;
        if (current() && this.detailWanted !== key) void this.refreshDetail();
      }
    }
  }

  refreshActivity(): void {
    void this.loadActivity();
  }

  private async loadActivity(): Promise<void> {
    const detail = this.view.detail;
    if (!this.live || detail == null) return;
    const session = this.session;
    const epoch = this.detailEpoch;
    const generation = ++this.activityGeneration;
    const current = () => this.alive(session) && epoch === this.detailEpoch &&
      generation === this.activityGeneration;
    this.patchDetail(detail.id, () => ({ activityStatus: "loading" }));
    try {
      const events = await getJobActivity(detail.id);
      if (!current()) return;
      this.patchDetail(detail.id, (open) => ({
        events: mergeActivityEvents(open.events, events),
        activityStatus: "loaded",
      }));
    } catch (error) {
      if (!current()) return;
      if (isNotFound(error)) this.closeDetail();
      else this.patchDetail(detail.id, () => ({ activityStatus: "error" }));
    }
  }

  cancel(id: string): void {
    void this.runAction(id, "cancel");
  }

  /** `origin` is the surface that started the Retry (its error appears there). */
  retry(id: string, origin: ErrorSurface = "row"): void {
    void this.runAction(id, "retry", origin);
  }

  private async runAction(id: string, action: QueueAction, origin: ErrorSurface = "row"): Promise<void> {
    if (!this.live || this.pendingActions.has(id)) return;
    // Retry also works for a row-less job with the matching open detail, using
    // the same winning snapshot the panel renders.
    const detail = this.view.detail;
    const row = findJob(this.view.lists, id);
    const current = action === "retry" && detail?.id === id ? newestSnapshot(row, detail.job) : row;
    if (current == null || (action === "cancel" ? !isActiveJob(current.status) : !canRetryJob(current))) return;
    const session = this.session;
    const floor = snapshotTime(current);
    this.pendingActions.set(id, action);
    this.errorFloors.delete(id);
    this.emit({ pending: { ...this.view.pending, [id]: action }, errors: without(this.view.errors, id) });
    try {
      const updated = await (action === "cancel" ? cancelJob(id) : retryJob(id));
      // Applied per rule 1: a newer SSE snapshot is never overwritten.
      if (this.alive(session)) {
        this.offer(updated, true);
        // A row-less retried job is never inserted from the response: the
        // owner-scoped merge lists it again.
        if (action === "retry" && findJob(this.view.lists, id) == null) this.requestReconcile("merge");
      }
    } catch (error) {
      if (!this.alive(session)) return;
      const newest = this.newestTime(id);
      if (newest == null || newest <= floor) {
        this.errorFloors.set(id, floor);
        this.emit({
          errors: {
            ...this.view.errors,
            [id]: error instanceof ApiRequestError ? error.message : ACTION_ERRORS[action],
          },
          ...(action === "retry" ? { errorSurfaces: { ...this.view.errorSurfaces, [id]: origin } } : {}),
        });
      }
      // A lost response may still mean the server accepted the action: only
      // read authoritative state, never repeat the mutation.
      try {
        const fresh = await getJob(id);
        if (this.alive(session)) this.offer(fresh, true);
      } catch {
        // SSE, polling or the next reconcile recovers.
      }
    } finally {
      if (this.alive(session)) {
        this.pendingActions.delete(id);
        this.emit({ pending: without(this.view.pending, id) });
      }
    }
  }
}

/** Page-local hook: one controller per mounted Queue page. */
export function useQueue(): { view: QueueView; controller: QueueController } {
  const [view, setView] = useState<QueueView>(INITIAL_QUEUE_VIEW);
  const [controller] = useState(() => new QueueController(setView));
  useEffect(() => {
    controller.start();
    return () => controller.stop();
  }, [controller]);
  return { view, controller };
}
