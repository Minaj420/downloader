import { useCallback, useEffect, useRef, useState } from "react";
import { ApiRequestError } from "../api/client";
import { useAnalyzeUrl, type NormalizedAnalysis } from "../api/analysis";
import { getJobActivity, type ActivityEvent } from "../api/activity";
import {
  cancelJob as cancelJobRequest,
  canRetryJob,
  getJob,
  isActiveJob,
  retryJob,
  useCreateJob,
  type CollectionMode,
  type Job,
} from "../api/jobs";
import { subscribeToJobUpdates } from "../api/events";
import { AnalysisResult } from "../features/new-download/AnalysisResult";
import { AnalyzeForm } from "../features/new-download/AnalyzeForm";
import {
  mergeActivityEvents,
  type ActivityLoadStatus,
} from "../features/new-download/activity";
import { JobProgress } from "../features/new-download/JobProgress";
import type { ItemsLoadStatus } from "../features/new-download/CollectionProgress";
import { AUTO_PLATFORM_VALUE } from "../features/new-download/platforms";

const POLL_INTERVAL_MS = 2000;

/** Collection selection is scoped to one analysis snapshot (PRODUCT_SPEC §10). */
interface CollectionSelectionState {
  analysisId: string;
  /** Snapshot-scoped backend item ids in the snapshot's source order. */
  selectedItemIds: string[];
  /** Explicit user choice; never inferred or defaulted. */
  mode: CollectionMode | null;
}

/**
 * A new analysis snapshot starts from a clean selection: analyzed, selectable
 * items are selected by default within the backend cap (PRODUCT_SPEC §10);
 * the mode is an explicit choice with no default. Old item ids and the old
 * mode are never reused across snapshots.
 */
function defaultCollectionSelection(
  result: NormalizedAnalysis,
): CollectionSelectionState | null {
  if (result.content_type !== "collection" || result.collection == null) {
    return null;
  }
  const collection = result.collection;
  const cap = Math.max(0, collection.selected_item_limit);
  return {
    analysisId: result.analysis_id,
    selectedItemIds: collection.items
      .filter((item) => item.selectable)
      .slice(0, cap)
      .map((item) => item.item_id),
    mode: null,
  };
}

// Preserve backend sub-millisecond precision (Date.parse alone discards it).
function snapshotTime(job: Job): string {
  const fraction = job.updated_at.match(/\.(\d+)/)?.[1] ?? "";
  return new Date(job.updated_at).toISOString().slice(0, 19) + fraction.padEnd(9, "0");
}

export function NewDownloadPage() {
  const [url, setUrl] = useState("");
  const [platform, setPlatform] = useState(AUTO_PLATFORM_VALUE);
  const [variantSelection, setVariantSelection] = useState<{
    analysisId: string;
    variantId: string;
  } | null>(null);
  const [collectionSelection, setCollectionSelection] =
    useState<CollectionSelectionState | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [itemsLoadStatus, setItemsLoadStatus] = useState<ItemsLoadStatus>("loading");
  const detailRequest = useRef<{ generation: number; key: string } | null>(null);
  const detailWanted = useRef<string | null>(null);
  const [activityEvents, setActivityEvents] = useState<ActivityEvent[]>([]);
  const [activityLoadStatus, setActivityLoadStatus] =
    useState<ActivityLoadStatus>("idle");
  const activityJobIdRef = useRef<string | null>(null);
  const activityRequestGeneration = useRef(0);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const trackedJob = useRef<Job | null>(null);
  const trackingGeneration = useRef(0);
  const retryPending = useRef(false);
  const cancelPending = useRef(false);
  // Closes the synchronous re-entry window before React Query's pending
  // render lands (notifyManager schedules state updates asynchronously):
  // two clicks in one tick would otherwise both pass `createJob.isPending`.
  const createPending = useRef(false);
  const retryFloor = useRef<string | null>(null);
  const retryErrorFloor = useRef<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      trackingGeneration.current += 1;
      activityRequestGeneration.current += 1;
    };
  }, []);

  const acceptSnapshot = useCallback((updated: Job, rich = false) => {
    const current = trackedJob.current;
    if (!mounted.current || current?.id !== updated.id) return false;
    const time = snapshotTime(updated);
    if (retryFloor.current != null && time <= retryFloor.current) return false;
    if (rich && updated.job_type === "collection" && updated.items != null &&
      time === snapshotTime(current) && updated.status === current.status) {
      // REST may enrich the exact SSE parent, never replace equal-time parent facts.
      const enriched = { ...current, items: updated.items };
      trackedJob.current = enriched;
      setJob(enriched);
      setItemsLoadStatus("loaded");
      return true;
    }
    // Equal timestamps cannot order snapshots: conservatively keep current.
    if (time <= snapshotTime(current)) return false;
    const next = updated.job_type === "collection" && updated.items == null
      ? { ...updated, items: current.items } : updated;
    trackedJob.current = next;
    setJob(next);
    if (updated.job_type === "collection") {
      setItemsLoadStatus(updated.items != null ? "loaded" : "loading");
    }
    // A newer same-ID attempt disproves the mutation alarm, not job.error.
    if (retryErrorFloor.current != null && time > retryErrorFloor.current) {
      retryErrorFloor.current = null;
      setRetryError(null);
    }
    return true;
  }, []);

  // One read per generation in flight; SSE bursts coalesce to the latest key.
  // A failed read does not repeat itself; later SSE/poll/reconnect may recover.
  const refreshCollection = useCallback(async function refresh() {
    const current = trackedJob.current;
    if (!mounted.current || current?.job_type !== "collection") return;
    const generation = trackingGeneration.current;
    const key = `${current.id}:${snapshotTime(current)}`;
    detailWanted.current = key;
    if (detailRequest.current?.generation === generation) return;
    const request = { generation, key };
    detailRequest.current = request;
    try {
      const fresh = await getJob(current.id);
      if (mounted.current && generation === trackingGeneration.current) {
        acceptSnapshot(fresh, true);
      }
    } catch {
      if (mounted.current && generation === trackingGeneration.current &&
        trackedJob.current?.id === current.id) setItemsLoadStatus("error");
    } finally {
      if (detailRequest.current === request) {
        detailRequest.current = null;
        if (mounted.current && generation === trackingGeneration.current &&
          detailWanted.current !== key) void refresh();
      }
    }
  }, [acceptSnapshot]);
  const analyze = useAnalyzeUrl();
  const createJob = useCreateJob();

  const platformHint = platform === AUTO_PLATFORM_VALUE ? null : platform;
  const selectedVariantId =
    analyze.data == null
      ? null
      : variantSelection?.analysisId === analyze.data.analysis_id
        ? variantSelection.variantId
        : (analyze.data.formats[0]?.variant_id ?? null);

  // Only the selection bound to the displayed analysis snapshot is live.
  const activeCollectionSelection =
    analyze.data != null &&
    collectionSelection?.analysisId === analyze.data.analysis_id
      ? collectionSelection
      : null;
  const collectionSnapshotMatches = activeCollectionSelection != null;

  const handleToggleCollectionItem = (itemId: string) => {
    if (!collectionSnapshotMatches || analyze.data?.collection == null) return;
    const collection = analyze.data.collection;
    const item = collection.items.find((entry) => entry.item_id === itemId);
    if (item == null || !item.selectable) return;
    setCollectionSelection((current) => {
      if (current == null) return current;
      const selected = current.selectedItemIds;
      if (selected.includes(itemId)) {
        return { ...current, selectedItemIds: selected.filter((id) => id !== itemId) };
      }
      if (selected.length >= collection.selected_item_limit) return current;
      // Keep the snapshot's source order for the posted item ids.
      const order = collection.items.map((entry) => entry.item_id);
      const next = [...selected, itemId].sort(
        (a, b) => order.indexOf(a) - order.indexOf(b),
      );
      return { ...current, selectedItemIds: next };
    });
  };

  // Select All covers only this snapshot's analyzed, selectable items, within
  // the backend cap; it never includes unseen, unanalyzed or over-cap items.
  const handleSelectAllCollectionItems = () => {
    const collection = analyze.data?.collection;
    if (!collectionSnapshotMatches || collection == null) return;
    const cap = Math.max(0, collection.selected_item_limit);
    setCollectionSelection((current) =>
      current == null
        ? current
        : {
            ...current,
            selectedItemIds: collection.items
              .filter((item) => item.selectable)
              .slice(0, cap)
              .map((item) => item.item_id),
          },
    );
  };

  const handleDeselectAllCollectionItems = () => {
    if (!collectionSnapshotMatches) return;
    setCollectionSelection((current) =>
      current == null ? current : { ...current, selectedItemIds: [] },
    );
  };

  const handleSelectCollectionMode = (mode: CollectionMode) => {
    if (!collectionSnapshotMatches) return;
    setCollectionSelection((current) =>
      current == null ? current : { ...current, mode },
    );
  };

  const runAnalysis = (nextUrl: string, hint: string | null) => {
    analyze.mutate({ url: nextUrl.trim(), platform_hint: hint }, {
      // Snapshot-scoped selection: every successful analysis resets the
      // defaults and the mode so old item ids are never reused.
      onSuccess: (result) => {
        if (!mounted.current) return;
        setCollectionSelection(defaultCollectionSelection(result));
      },
    });
  };

  const handleSubmit = () => {
    runAnalysis(url, platformHint);
  };

  const handleUseDetectedPlatform = (detected: string) => {
    setPlatform(detected);
    // Re-analyze the URL that produced this result, not whatever the input
    // currently contains; the user may have edited it after analyzing.
    runAnalysis(analyze.data?.original_url ?? url, detected);
  };

  const handleCreated = (created: Job) => {
    if (!mounted.current) return;
    trackingGeneration.current += 1;
    trackedJob.current = created;
    detailWanted.current = null;
    setItemsLoadStatus(created.items != null ? "loaded" : "loading");
    retryFloor.current = null;
    retryErrorFloor.current = null;
    retryPending.current = false;
    cancelPending.current = false;
    setIsRetrying(false);
    setIsCancelling(false);
    setRetryError(null);
    activityRequestGeneration.current += 1;
    activityJobIdRef.current = created.id;
    setJob(created);
    // A new job id means a new activity history.
    setActivityEvents([]);
    setActivityLoadStatus("idle");
  };

  const handleDownload = () => {
    const result = analyze.data;
    // A pending analyze/create must not post a stale snapshot or duplicate
    // the create mutation. The ref rejects a second synchronous create that
    // arrives before the pending state has rendered.
    if (
      result == null ||
      analyze.isPending ||
      createJob.isPending ||
      createPending.current
    ) {
      return;
    }
    // A failed create keeps the selection for an explicit retry; the ref is
    // released by onSettled on both success and error, never before.
    const releaseCreate = () => {
      createPending.current = false;
    };

    if (result.content_type === "collection") {
      const selection = collectionSelection;
      const collection = result.collection;
      if (
        collection == null ||
        selection?.analysisId !== result.analysis_id ||
        selection.mode == null ||
        selection.selectedItemIds.length === 0
      ) {
        return;
      }
      // Project onto the owned snapshot: distinct selectable ids in the
      // snapshot's source order, within the backend cap.
      const chosen = new Set(selection.selectedItemIds);
      const itemIds = collection.items
        .filter((item) => item.selectable && chosen.has(item.item_id))
        .map((item) => item.item_id);
      if (
        itemIds.length === 0 ||
        itemIds.length > collection.selected_item_limit
      ) {
        return;
      }
      createPending.current = true;
      createJob.mutate(
        {
          analysis_id: result.analysis_id,
          item_ids: itemIds,
          collection_mode: selection.mode,
          collection_profile: { kind: "original_best_available" },
          destination: "app_library",
        },
        { onSuccess: handleCreated, onSettled: releaseCreate },
      );
      return;
    }
    if (selectedVariantId != null) {
      createPending.current = true;
      createJob.mutate(
        {
          analysis_id: result.analysis_id,
          variant_id: selectedVariantId,
          destination: "app_library",
        },
        { onSuccess: handleCreated, onSettled: releaseCreate },
      );
    }
  };

  // Live updates via SSE, with periodic REST polling as the fallback when
  // the event stream drops. One SSE connection per tracked job serves both
  // job state and activity events; the SSE stream replays nothing on first
  // connect, so initial activity history comes from REST after SSE is open,
  // and an SSE drop refreshes it from REST again.
  const jobId = job?.id ?? null;

  const refreshActivity = useCallback(async () => {
    if (jobId == null) {
      return;
    }
    const requestGeneration = ++activityRequestGeneration.current;
    setActivityLoadStatus("loading");
    try {
      const events = await getJobActivity(jobId);
      if (
        activityJobIdRef.current !== jobId ||
        activityRequestGeneration.current !== requestGeneration
      ) {
        return;
      }
      setActivityEvents((previous) => mergeActivityEvents(previous, events));
      setActivityLoadStatus("loaded");
    } catch {
      if (
        activityJobIdRef.current === jobId &&
        activityRequestGeneration.current === requestGeneration
      ) {
        setActivityLoadStatus("error");
      }
    }
  }, [jobId]);

  useEffect(() => {
    if (jobId == null) {
      return;
    }
    let hasOpened = false;
    let disposed = false;
    const unsubscribe = subscribeToJobUpdates(
      (updated) => {
        if (!disposed && updated.id === jobId) {
          if (acceptSnapshot(updated) && updated.job_type === "collection") {
            void refreshCollection();
          }
        }
      },
      () => {
        // The event stream dropped: refresh authoritative REST state.
        if (!disposed && hasOpened) {
          void refreshActivity();
          void refreshCollection();
        }
      },
      (activityEvent) => {
        if (!disposed && activityEvent.job_id === jobId) {
          setActivityEvents((previous) =>
            mergeActivityEvents(previous, [activityEvent]),
          );
        }
      },
      () => {
        if (disposed) return;
        // Subscribe first, then close the history/live race with REST.
        hasOpened = true;
        void refreshActivity();
        void refreshCollection();
      },
    );
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, [jobId, refreshActivity, acceptSnapshot, refreshCollection]);

  const jobActive = job != null && isActiveJob(job.status);
  const pollKey = jobActive ? jobId : null;
  useEffect(() => {
    if (pollKey == null) {
      return;
    }
    let cancelled = false;
    const poll = window.setInterval(async () => {
      if (trackedJob.current?.job_type === "collection") {
        void refreshCollection();
        return;
      }
      const generation = trackingGeneration.current;
      try {
        const fresh = await getJob(pollKey);
        if (!cancelled && generation === trackingGeneration.current) {
          acceptSnapshot(fresh);
        }
      } catch {
        // Transient polling failure: the SSE stream or the next poll recovers.
      }
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
    };
  }, [pollKey, acceptSnapshot, refreshCollection]);

  const handleCancel = async () => {
    const current = trackedJob.current;
    if (current == null || !isActiveJob(current.status) || cancelPending.current || retryPending.current) return;
    const generation = trackingGeneration.current;
    cancelPending.current = true;
    setIsCancelling(true);
    try {
      const updated = await cancelJobRequest(current.id);
      if (generation === trackingGeneration.current) acceptSnapshot(updated, true);
    } catch {
      // Keep the panel as-is; polling will reflect the outcome.
    } finally {
      if (mounted.current && generation === trackingGeneration.current) {
        cancelPending.current = false;
        setIsCancelling(false);
      }
    }
  };

  const handleRetry = async () => {
    const current = trackedJob.current;
    if (current == null || !canRetryJob(current) || retryPending.current || cancelPending.current) return;
    const generation = ++trackingGeneration.current;
    retryPending.current = true;
    const previousFloor = retryFloor.current;
    retryFloor.current = snapshotTime(current);
    if (current.job_type === "collection") {
      const pending = { ...current, items: null };
      trackedJob.current = pending;
      setJob(pending);
      setItemsLoadStatus("loading");
    }
    setIsRetrying(true);
    retryErrorFloor.current = null;
    setRetryError(null);
    const stillTracked = () => mounted.current && generation === trackingGeneration.current;
    try {
      const updated = await retryJob(current.id);
      if (!stillTracked()) return;
      // SSE may already have observed this attempt progressing or finishing
      // while the queued HTTP response was in flight.
      if (trackedJob.current?.id === current.id &&
        snapshotTime(trackedJob.current) === snapshotTime(current)) acceptSnapshot(updated, true);
      else if (current.job_type === "collection") void refreshCollection();
      void refreshActivity();
    } catch (error) {
      if (!stillTracked()) return;
      const failureTime = snapshotTime(current);
      const newerAttemptObserved = trackedJob.current?.id === current.id
        && snapshotTime(trackedJob.current) > failureTime;
      retryErrorFloor.current = newerAttemptObserved ? null : failureTime;
      setRetryError(newerAttemptObserved ? null : error instanceof ApiRequestError
        ? error.message
        : "Could not retry the download. Check your connection and try again.");
      // A lost response may still mean the server accepted the action. Only
      // read authoritative state; never automatically repeat the mutation.
      try {
        const fresh = await getJob(current.id);
        if (!stillTracked()) return;
        retryFloor.current = previousFloor;
        acceptSnapshot(fresh, true);
        void refreshActivity();
      } catch {
        if (stillTracked()) retryFloor.current = previousFloor;
      }
    } finally {
      if (stillTracked()) {
        retryPending.current = false;
        setIsRetrying(false);
      }
    }
  };

  const analyzeError = analyze.error;
  const analyzeRetryable =
    analyzeError instanceof ApiRequestError && analyzeError.retryable;
  const createError = createJob.error;

  return (
    <section>
      <h1>New Download</h1>
      <AnalyzeForm
        url={url}
        platform={platform}
        isPending={analyze.isPending}
        onUrlChange={setUrl}
        onPlatformChange={setPlatform}
        onSubmit={handleSubmit}
      />

      {analyzeError && (
        <div className="error-banner" role="alert">
          <p>{analyzeError.message}</p>
          {analyzeRetryable && (
            <button type="button" className="button-secondary" onClick={handleSubmit}>
              Retry
            </button>
          )}
        </div>
      )}

      {createError && (
        <div className="error-banner" role="alert">
          <p>{createError.message}</p>
        </div>
      )}

      {analyze.data && (
        <AnalysisResult
          result={analyze.data}
          selectedVariantId={selectedVariantId}
          selectedCollectionItemIds={activeCollectionSelection?.selectedItemIds ?? []}
          collectionMode={activeCollectionSelection?.mode ?? null}
          isCreatingJob={createJob.isPending}
          isAnalyzing={analyze.isPending}
          onSelectVariant={(variantId) =>
            setVariantSelection({ analysisId: analyze.data.analysis_id, variantId })
          }
          onToggleCollectionItem={handleToggleCollectionItem}
          onSelectAllCollectionItems={handleSelectAllCollectionItems}
          onDeselectAllCollectionItems={handleDeselectAllCollectionItems}
          onSelectCollectionMode={handleSelectCollectionMode}
          onDownload={handleDownload}
          onUseDetectedPlatform={handleUseDetectedPlatform}
        />
      )}

      {job && (
        <JobProgress
          job={job}
          itemsLoadStatus={itemsLoadStatus}
          isCancelling={isCancelling}
          onCancel={handleCancel}
          isRetrying={isRetrying}
          retryError={retryError}
          onRetry={handleRetry}
          activityEvents={activityEvents}
          activityLoadStatus={activityLoadStatus}
          onRefreshActivity={() => void refreshActivity()}
        />
      )}
    </section>
  );
}
