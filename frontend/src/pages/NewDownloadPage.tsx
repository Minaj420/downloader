import { useCallback, useEffect, useRef, useState } from "react";
import { ApiRequestError } from "../api/client";
import { useAnalyzeUrl } from "../api/analysis";
import { getJobActivity, type ActivityEvent } from "../api/activity";
import {
  cancelJob as cancelJobRequest,
  getJob,
  isActiveJob,
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
    const unsubscribe = subscribeToJobUpdates(
      (updated) => {
        if (updated.id === jobId) {
          setJob(updated);
        }
      },
      () => {
        // The event stream dropped: refresh authoritative REST state.
        if (hasOpened) {
          void refreshActivity();
        }
      },
      (activityEvent) => {
        if (activityEvent.job_id === jobId) {
          setActivityEvents((previous) =>
            mergeActivityEvents(previous, [activityEvent]),
          );
        }
      },
      () => {
        // Subscribe first, then close the history/live race with REST.
        hasOpened = true;
        void refreshActivity();
      },
    );
    return unsubscribe;
  }, [jobId, refreshActivity]);

  const jobActive = job != null && isActiveJob(job.status);
  const pollKey = jobActive ? jobId : null;
  useEffect(() => {
    if (pollKey == null) {
      return;
    }
    let cancelled = false;
    const poll = window.setInterval(async () => {
      try {
        const fresh = await getJob(pollKey);
        if (!cancelled) {
          setJob(fresh);
        }
      } catch {
        // Transient polling failure: the SSE stream or the next poll recovers.
      }
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
    };
  }, [pollKey]);

  const handleCancel = async () => {
    const current = job;
    if (current == null) return;
    setIsCancelling(true);
    try {
      setJob(await cancelJobRequest(current.id));
    } catch {
      // Keep the panel as-is; polling will reflect the outcome.
    } finally {
      setIsCancelling(false);
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
          activityEvents={activityEvents}
          activityLoadStatus={activityLoadStatus}
          onRefreshActivity={() => void refreshActivity()}
        />
      )}
    </section>
  );
}
