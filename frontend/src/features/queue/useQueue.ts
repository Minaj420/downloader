import { useEffect, useState } from "react";
import { getJobActivity, type ActivityEvent } from "../../api/activity";
import { ApiError, ApiRequestError } from "../../api/client";
import { subscribeToJobUpdates, type Unsubscribe } from "../../api/events";
import {
  cancelJob,
  canRetryJob,
  CLEAR_PREVIEW_EXPIRED,
  confirmClearHistory,
  getJob,
  isActiveJob,
  listActiveJobs,
  listHistoryJobs,
  previewClearHistory,
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
  isHistoryJob,
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

/**
 * One open on-demand detail, keyed by job ID (not list membership). `slot`
 * is the opening's identity: a new value on every open, kept while the panel
 * moves between inline/top/hidden, so a close/reopen never reuses it.
 */
export interface QueueDetail {
  id: string;
  slot: number;
  job: Job;
  itemsStatus: ItemsLoadStatus;
  events: ActivityEvent[];
  activityStatus: ActivityLoadStatus;
}

/**
 * Clear History (PRODUCT_SPEC §17, ARCHITECTURE §59 Q3): one preview, one
 * explicit confirm. `jobCount` is the server `job_count` of the current
 * preview (ready/confirming only); no records or byte estimates exist here.
 */
export type ClearHistoryPhase = "idle" | "previewing" | "ready" | "confirming";

export interface ClearHistoryMessage {
  kind: "status" | "error";
  text: string;
  /**
   * Unknown confirm outcome (not a proven expiry): the page offers a manual
   * Refresh queue that only runs the existing reset; never a preview/confirm.
   */
  refresh?: boolean;
}

export interface ClearHistoryView {
  includeCancelled: boolean;
  phase: ClearHistoryPhase;
  jobCount: number | null;
  message: ClearHistoryMessage | null;
}

export const INITIAL_CLEAR_HISTORY: ClearHistoryView = {
  includeCancelled: false,
  phase: "idle",
  jobCount: null,
  message: null,
};

export function jobsLabel(count: number): string {
  return `${count} ${count === 1 ? "job" : "jobs"}`;
}

export interface QueueView {
  /** Clear History state; reset on every controller start. */
  clear: ClearHistoryView;
  lists: QueueLists;
  listStatus: QueueListStatus;
  listError: string | null;
  loadMoreStatus: LoadMoreStatus;
  sseOpen: boolean;
  pending: Readonly<Record<string, QueueAction>>;
  errors: Readonly<Record<string, string>>;
  /** Transient UI placement of a Retry error (one surface at a time). */
  errorSurfaces: Readonly<Record<string, ErrorSurface>>;
  /** Open details in opening order (one per job ID). */
  details: readonly QueueDetail[];
  /**
   * The single upper panel: an Active job's detail, or a History detail whose
   * row is not loaded. Never a History detail whose row is loaded (that one
   * is inline). Open details that are neither inline nor top stay hidden.
   */
  topId: string | null;
}

export const INITIAL_QUEUE_VIEW: QueueView = {
  clear: INITIAL_CLEAR_HISTORY,
  lists: EMPTY_QUEUE_LISTS,
  listStatus: "loading",
  listError: null,
  loadMoreStatus: "idle",
  sseOpen: false,
  pending: {},
  errors: {},
  errorSurfaces: {},
  details: [],
  topId: null,
};

export function findDetail(view: QueueView, id: string): QueueDetail | undefined {
  return view.details.find((detail) => detail.id === id);
}

function inHistory(lists: QueueLists, id: string): boolean {
  return lists.history.some((job) => job.id === id);
}

/** Rendered somewhere: inline under its History row, or as the top panel. */
function detailVisible(view: QueueView, id: string): boolean {
  return findDetail(view, id) != null && (view.topId === id || inHistory(view.lists, id));
}

const LIST_ERROR = "Could not refresh the queue. Check your connection and try again.";
const ACTION_ERRORS: Record<QueueAction, string> = {
  cancel: "Could not cancel the download. Check your connection and try again.",
  retry: "Could not retry the download. Check your connection and try again.",
};

const CLEAR_PREVIEW_ERROR = "Could not preview Clear History. Try again.";
const CLEAR_NOTHING = "There are no history jobs to clear.";
const CLEAR_EXPIRED = "This preview has expired. Preview Clear History again.";
// The outcome is unknown: never claim that nothing was deleted or that the
// list refresh succeeded.
const CLEAR_FAILED = "Clear History could not finish. Check the list and preview again.";

/**
 * One Clear History attempt: the displayed server count and the opaque token
 * of the same preview live together. A continuation is current only for its
 * own attempt object; `previewId` is consumed (nulled) before the confirm
 * request, so it is sent at most once.
 */
interface ClearAttempt {
  previewId: string | null;
  jobCount: number | null;
}

function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

function without<T>(record: Readonly<Record<string, T>>, id: string): Record<string, T> {
  const next = { ...record };
  delete next[id];
  return next;
}

/** Per-opening request state; a continuation is current only for its own slot object. */
interface DetailSlot {
  readonly serial: number;
  request: object | null;
  wanted: string | null;
  resets: number;
  activityGeneration: number;
}

/**
 * Queue page controller implementing the Q1 live-update rules
 * (ARCHITECTURE §59): one global SSE subscription, reset/merge reconciles
 * with a history generation, guarded Load more, on-demand details and
 * Cancel/Retry without optimistic changes or automatic repeats. Every async
 * continuation checks the session (start/stop) and its own generation/slot.
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
  // One slot per open detail ID; replaced (new identity) on every open.
  private readonly slots = new Map<string, DetailSlot>();
  private slotSerial = 0;
  private clearAttempt: ClearAttempt | null = null;

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
    // A previous session's preview/confirm never continues into this one.
    this.clearAttempt = null;
    for (const slot of this.slots.values()) {
      slot.request = null;
      slot.wanted = null;
    }
    const session = this.session;
    // Subscribe first; every REST read happens after it (mount reset below).
    this.unsubscribe = subscribeToJobUpdates(
      (job) => { if (this.alive(session)) this.handleSnapshot(job); },
      () => { if (this.alive(session)) this.handleError(); },
      (event) => { if (this.alive(session)) this.handleActivity(event); },
      () => { if (this.alive(session)) this.handleOpen(); },
    );
    this.emit({ sseOpen: false, pending: {}, loadMoreStatus: "idle", clear: INITIAL_CLEAR_HISTORY });
    this.requestReconcile("reset");
  }

  stop(): void {
    this.session += 1;
    this.live = false;
    this.unsubscribe?.();
    this.unsubscribe = null;
    if (this.pollTimer != null) clearInterval(this.pollTimer);
    this.pollTimer = null;
  }

  private alive(session: number): boolean {
    return this.live && session === this.session;
  }

  private emit(patch: Partial<QueueView>): void {
    this.view = this.alignErrorSurfaces(this.placeDetails({ ...this.view, ...patch }));
    if (!this.live) return;
    this.publish(this.view);
    this.syncPolling();
  }

  /**
   * Top-panel invariant, applied at every state change (no fallback
   * promotion here; that only happens at a reset boundary). Status comes
   * from the winning snapshot of the row and the detail (`newestSnapshot`),
   * so a stale row/response never decides placement, and placement never
   * changes list membership:
   * - a top detail whose row is in History is shown inline, so the top frees;
   * - an open detail that is Active by its winning snapshot (Active row, or
   *   rowless after an SSE/GET/Retry response) takes the single top panel.
   *   When it arrives there, only previous Active selections (the old top
   *   when Active, any other Active detail) are closed, so they cannot pop
   *   back. A displaced terminal fallback stays open but hidden (slot,
   *   Activity and expanded state kept) and returns inline with its row; it
   *   is never put back on top except by a later reset fallback.
   */
  private placeDetails(view: QueueView): QueueView {
    const { lists, details } = view;
    const activeNow = (detail: QueueDetail) =>
      isActiveJob(newestSnapshot(findJob(lists, detail.id), detail.job).status);
    // A History-row detail is always inline (never also on top).
    const topEligible = (detail: QueueDetail) => activeNow(detail) && !inHistory(lists, detail.id);
    let topId = view.topId;
    if (topId != null && (!details.some((detail) => detail.id === topId) || inHistory(lists, topId))) {
      topId = null;
    }
    let winner: QueueDetail | null = null;
    for (const detail of details) {
      if (detail.id !== topId && topEligible(detail) && (winner == null || detail.slot > winner.slot)) {
        winner = detail;
      }
    }
    let kept = details;
    if (winner != null) {
      const winnerId = winner.id;
      kept = details.filter((detail) => detail.id === winnerId || !topEligible(detail));
      for (const detail of details) {
        if (!kept.includes(detail)) this.slots.delete(detail.id);
      }
      topId = winnerId;
    }
    if (topId === view.topId && kept === details) return view;
    return { ...view, details: kept, topId };
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
        const hasDetail = detailVisible(view, id);
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

  /** Newest-wins for one snapshot into the lists and that ID's open detail. */
  private offer(job: Job, rich: boolean): void {
    this.commitLists(applySnapshot(this.view.lists, job, { rich, insert: false }).lists);
    this.offerDetail(job, rich);
  }

  private offerDetail(job: Job, rich: boolean): void {
    const detail = findDetail(this.view, job.id);
    if (detail == null) return;
    const next = decideSnapshot(detail.job, job, rich);
    if (next == null) return;
    const collection = next.job_type === "collection";
    this.replaceDetail({
      ...detail,
      job: next,
      itemsStatus: collection ? (job.items != null ? "loaded" : "loading") : detail.itemsStatus,
    });
    this.clearSupersededErrors();
    // A snapshot without children triggers one coalesced detail read.
    if (collection && job.items == null) void this.refreshDetail(job.id);
  }

  /** Every open detail snapshot: comparison bases only, never list membership. */
  private priors(): Job[] {
    return this.view.details.map((detail) => detail.job);
  }

  private newestTime(id: string): string | null {
    const times = [findJob(this.view.lists, id), findDetail(this.view, id)?.job]
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
      const priors = this.priors();
      const result = kind === "reset"
        ? applyReset(this.view.lists, active.jobs, head, priors)
        : applyMerge(this.view.lists, active.jobs, head, priors);
      if (result.reset && kind === "merge") this.generation += 1;
      this.commitLists(result.lists);
      this.emit({
        listStatus: "ready",
        listError: null,
        ...(result.reset ? { loadMoreStatus: "idle" as const } : {}),
      });
      if (result.reset) this.promoteMissingDetail();
      for (const job of [...active.jobs, ...head.jobs]) this.offerDetail(job, true);
      for (const id of result.missingActiveIds) void this.readMissing(id, !result.reset);
      if (result.reset) this.reloadDetails();
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
   * Reset boundary only: when the top panel is free, the most recently opened
   * open detail whose row is in neither list and whose winning snapshot is
   * terminal is shown there (an Active one already holds the top through
   * placeDetails). Other such details stay open (hidden) and return inline
   * when their row returns. A manual or 404 close of the top never promotes
   * until the next reset.
   */
  private promoteMissingDetail(): void {
    if (this.view.topId != null) return;
    let latest: QueueDetail | null = null;
    for (const detail of this.view.details) {
      if (findJob(this.view.lists, detail.id) != null || !isHistoryJob(detail.job.status)) continue;
      if (latest == null || detail.slot > latest.slot) latest = detail;
    }
    if (latest != null) this.emit({ topId: latest.id });
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
      this.commitLists(applyHistoryPage(this.view.lists, page, this.priors()));
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

  /** Row toggle: opens this ID's detail, or closes it when already open. */
  openDetail(id: string): void {
    if (!this.live) return;
    if (findDetail(this.view, id) != null) {
      this.closeDetail(id);
      return;
    }
    const job = findJob(this.view.lists, id);
    if (job == null) return;
    const slot: DetailSlot = {
      serial: ++this.slotSerial,
      request: null,
      wanted: null,
      resets: 0,
      activityGeneration: 0,
    };
    this.slots.set(id, slot);
    // A History row opens inline; an Active row takes the top (placeDetails).
    this.emit({
      details: [...this.view.details, {
        id,
        slot: slot.serial,
        job,
        itemsStatus: job.job_type === "collection" && job.items == null ? "loading" : "loaded",
        events: [],
        activityStatus: "idle",
      }],
    });
    void this.refreshDetail(id);
    void this.loadActivity(id);
  }

  /** Closes only this ID's detail; never promotes another one. */
  closeDetail(id: string): void {
    if (findDetail(this.view, id) == null) return;
    this.slots.delete(id);
    this.emit({
      details: this.view.details.filter((detail) => detail.id !== id),
      ...(this.view.topId === id ? { topId: null } : {}),
    });
  }

  private replaceDetail(next: QueueDetail): void {
    this.emit({ details: this.view.details.map((detail) => (detail.id === next.id ? next : detail)) });
  }

  private patchDetail(id: string, patch: (detail: QueueDetail) => Partial<QueueDetail>): void {
    const detail = findDetail(this.view, id);
    if (detail != null) this.replaceDetail({ ...detail, ...patch(detail) });
  }

  private slotCurrent(id: string, slot: DetailSlot, session: number): boolean {
    return this.alive(session) && this.slots.get(id) === slot;
  }

  // Rule 6: every reset re-reads each open detail (hidden ones too; 404 closes).
  private reloadDetails(): void {
    for (const detail of this.view.details) {
      const slot = this.slots.get(detail.id);
      if (slot == null) continue;
      slot.resets += 1;
      void this.refreshDetail(detail.id);
      void this.loadActivity(detail.id);
    }
  }

  // Per ID: one detail read in flight; bursts coalesce to the latest key.
  private async refreshDetail(id: string): Promise<void> {
    const slot = this.slots.get(id);
    const detail = findDetail(this.view, id);
    if (!this.live || slot == null || detail == null) return;
    const session = this.session;
    const keyOf = (open: QueueDetail) => `${snapshotTime(open.job)}:${slot.resets}`;
    const key = keyOf(detail);
    slot.wanted = key;
    if (slot.request != null) return;
    const request = {};
    slot.request = request;
    const current = () => this.slotCurrent(id, slot, session);
    try {
      const fresh = await getJob(id);
      if (current()) this.offer(fresh, true);
    } catch (error) {
      if (!current()) return;
      if (isNotFound(error)) {
        this.closeDetail(id);
        return;
      }
      // Only a still-current request may flag its items: a newer snapshot or
      // reset since then (e.g. a rich REST response with items) stays as is.
      const open = findDetail(this.view, id);
      if (slot.wanted === key && open != null && keyOf(open) === key) {
        this.patchDetail(id, () => ({ itemsStatus: "error" }));
      }
    } finally {
      if (slot.request === request) {
        slot.request = null;
        if (current() && slot.wanted !== key) void this.refreshDetail(id);
      }
    }
  }

  /** Manual Activity reload for one open detail. */
  refreshActivity(id: string): void {
    void this.loadActivity(id);
  }

  private async loadActivity(id: string): Promise<void> {
    const slot = this.slots.get(id);
    if (!this.live || slot == null || findDetail(this.view, id) == null) return;
    const session = this.session;
    const generation = ++slot.activityGeneration;
    const current = () => this.slotCurrent(id, slot, session) && generation === slot.activityGeneration;
    this.patchDetail(id, () => ({ activityStatus: "loading" }));
    try {
      const events = await getJobActivity(id);
      if (!current()) return;
      this.patchDetail(id, (open) => ({
        events: mergeActivityEvents(open.events, events),
        activityStatus: "loaded",
      }));
    } catch (error) {
      if (!current()) return;
      if (isNotFound(error)) this.closeDetail(id);
      else this.patchDetail(id, () => ({ activityStatus: "error" }));
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
    const detail = findDetail(this.view, id);
    const row = findJob(this.view.lists, id);
    const current = action === "retry" && detail != null ? newestSnapshot(row, detail.job) : row;
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

  private emitClear(patch: Partial<ClearHistoryView>): void {
    this.emit({ clear: { ...this.view.clear, ...patch } });
  }

  /** Only while no attempt runs: the selection is locked from preview to result. */
  setClearIncludeCancelled(includeCancelled: boolean): void {
    if (!this.live || this.clearAttempt != null) return;
    this.emitClear({ includeCancelled, message: null });
  }

  /** User-started only; every attempt asks the server for a fresh preview. */
  startClearHistory(): void {
    if (!this.live || this.clearAttempt != null) return;
    const attempt: ClearAttempt = { previewId: null, jobCount: null };
    this.clearAttempt = attempt;
    const includeCancelled = this.view.clear.includeCancelled;
    this.emitClear({ phase: "previewing", jobCount: null, message: null });
    void this.runClearPreview(attempt, includeCancelled);
  }

  private async runClearPreview(attempt: ClearAttempt, includeCancelled: boolean): Promise<void> {
    const session = this.session;
    const current = () => this.alive(session) && this.clearAttempt === attempt;
    try {
      const preview = await previewClearHistory(includeCancelled);
      if (!current()) return;
      if (!(preview.job_count > 0)) {
        // Nothing to confirm: the token is dropped, no confirm is offered.
        this.clearAttempt = null;
        this.emitClear({ phase: "idle", jobCount: null, message: { kind: "status", text: CLEAR_NOTHING } });
        return;
      }
      attempt.previewId = preview.preview_id;
      attempt.jobCount = preview.job_count;
      this.emitClear({ phase: "ready", jobCount: attempt.jobCount });
    } catch {
      if (!current()) return;
      this.clearAttempt = null;
      this.emitClear({ phase: "idle", jobCount: null, message: { kind: "error", text: CLEAR_PREVIEW_ERROR } });
    }
  }

  /** Non-destructive: drops the preview (or its late response); a new attempt previews again. */
  dismissClearHistory(): void {
    const attempt = this.clearAttempt;
    if (!this.live || attempt == null || this.view.clear.phase === "confirming") return;
    this.clearAttempt = null;
    this.emitClear({ phase: "idle", jobCount: null, message: null });
  }

  /** Sends the displayed preview's token exactly once; never repeated automatically. */
  confirmClearHistory(): void {
    const attempt = this.clearAttempt;
    if (!this.live || attempt == null || this.view.clear.phase !== "ready") return;
    const previewId = attempt.previewId;
    if (previewId == null) return;
    // Consumed synchronously, before any await: a second click finds nothing.
    attempt.previewId = null;
    this.emitClear({ phase: "confirming" });
    void this.runClearConfirm(attempt, previewId);
  }

  private async runClearConfirm(attempt: ClearAttempt, previewId: string): Promise<void> {
    const session = this.session;
    let message: ClearHistoryMessage;
    try {
      const result = await confirmClearHistory(previewId);
      const deleted = `Deleted ${jobsLabel(result.deleted_count)}.`;
      message = result.failed_count > 0
        ? { kind: "error", text: `${deleted} ${jobsLabel(result.failed_count)} could not be deleted.` }
        : { kind: "status", text: deleted };
    } catch (error) {
      const expired = error instanceof ApiRequestError && error.code === CLEAR_PREVIEW_EXPIRED;
      message = expired
        ? { kind: "error", text: CLEAR_EXPIRED }
        : { kind: "error", text: CLEAR_FAILED, refresh: true };
    }
    if (this.alive(session) && this.clearAttempt === attempt) {
      this.clearAttempt = null;
      this.emitClear({ phase: "idle", jobCount: null, message });
    }
    // Every settled outcome may have changed server state (partial commits
    // included): run the existing reset, which also re-reads open details
    // (404 closes them). No optimistic list change; no-op once unmounted.
    this.requestReconcile("reset");
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
