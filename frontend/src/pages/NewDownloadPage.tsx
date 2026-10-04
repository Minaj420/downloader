import { useCallback, useEffect, useRef, useState } from "react";
import { ApiRequestError } from "../api/client";
import { useAnalyzeUrl } from "../api/analysis";
import { getJobActivity, type ActivityEvent } from "../api/activity";
import {
  cancelJob as cancelJobRequest,
  getJob,
  isActiveJob,
  retryJob,
  useCreateJob,
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
import { AUTO_PLATFORM_VALUE } from "../features/new-download/platforms";

const POLL_INTERVAL_MS = 2000;

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
  const [job, setJob] = useState<Job | null>(null);
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

  const acceptSnapshot = useCallback((updated: Job) => {
    const current = trackedJob.current;
    if (!mounted.current || current?.id !== updated.id) return;
    const time = snapshotTime(updated);
    if (retryFloor.current != null && time <= retryFloor.current) return;
    // Equal timestamps cannot order snapshots: conservatively keep current.
    if (time <= snapshotTime(current)) return;
    trackedJob.current = updated;
    setJob(updated);
    // A newer same-ID attempt disproves the mutation alarm, not job.error.
    if (retryErrorFloor.current != null && time > retryErrorFloor.current) {
      retryErrorFloor.current = null;
      setRetryError(null);
    }
  }, []);
  const analyze = useAnalyzeUrl();
  const createJob = useCreateJob();

  const platformHint = platform === AUTO_PLATFORM_VALUE ? null : platform;
  const selectedVariantId =
    analyze.data == null
      ? null
      : variantSelection?.analysisId === analyze.data.analysis_id
        ? variantSelection.variantId
        : (analyze.data.formats[0]?.variant_id ?? null);

  const runAnalysis = (nextUrl: string, hint: string | null) => {
    analyze.mutate({ url: nextUrl.trim(), platform_hint: hint });
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

  const handleDownload = () => {
    if (analyze.data && selectedVariantId != null) {
      createJob.mutate(
        {
          analysis_id: analyze.data.analysis_id,
          variant_id: selectedVariantId,
          destination: "app_library",
        },
        {
          onSuccess: (created) => {
            if (!mounted.current) return;
            trackingGeneration.current += 1;
            trackedJob.current = created;
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
          },
        },
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
          acceptSnapshot(updated);
        }
      },
      () => {
        // The event stream dropped: refresh authoritative REST state.
        if (!disposed && hasOpened) {
          void refreshActivity();
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
      },
    );
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, [jobId, refreshActivity, acceptSnapshot]);

  const jobActive = job != null && isActiveJob(job.status);
  const pollKey = jobActive ? jobId : null;
  useEffect(() => {
    if (pollKey == null) {
      return;
    }
    let cancelled = false;
    const poll = window.setInterval(async () => {
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
  }, [pollKey, acceptSnapshot]);

  const handleCancel = async () => {
    const current = trackedJob.current;
    if (current == null || cancelPending.current || retryPending.current) return;
    const generation = trackingGeneration.current;
    cancelPending.current = true;
    setIsCancelling(true);
    try {
      const updated = await cancelJobRequest(current.id);
      if (generation === trackingGeneration.current) acceptSnapshot(updated);
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
    if (current?.status !== "failed" || !current.error?.retryable || retryPending.current || cancelPending.current) return;
    const generation = ++trackingGeneration.current;
    retryPending.current = true;
    const previousFloor = retryFloor.current;
    retryFloor.current = snapshotTime(current);
    setIsRetrying(true);
    retryErrorFloor.current = null;
    setRetryError(null);
    const stillTracked = () => mounted.current && generation === trackingGeneration.current;
    try {
      const updated = await retryJob(current.id);
      if (!stillTracked()) return;
      // SSE may already have observed this attempt progressing or finishing
      // while the queued HTTP response was in flight.
      if (trackedJob.current === current) acceptSnapshot(updated);
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
        acceptSnapshot(fresh);
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
          isCreatingJob={createJob.isPending}
          onSelectVariant={(variantId) =>
            setVariantSelection({ analysisId: analyze.data.analysis_id, variantId })
          }
          onDownload={handleDownload}
          onUseDetectedPlatform={handleUseDetectedPlatform}
        />
      )}

      {job && (
        <JobProgress
          job={job}
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
