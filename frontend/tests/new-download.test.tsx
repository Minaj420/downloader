import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/app/App";
import type { AnalyzeRequest, NormalizedAnalysis } from "../src/api/analysis";
import type { Job } from "../src/api/jobs";

type FetchHandler = (url: string, init?: RequestInit) => Promise<Response>;

function stubFetch(handler: FetchHandler) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    handler(String(input), init),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

class FakeEventSource {
  static current: FakeEventSource | null = null;

  readonly url: string;
  onerror: (() => void) | null = null;
  private readonly listeners = new Map<string, EventListener[]>();

  constructor(url: string | URL) {
    this.url = String(url);
    FakeEventSource.current = this;
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  emit(type: string, data: unknown) {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }

  close() {}
}

function stubEventSource() {
  FakeEventSource.current = null;
  vi.stubGlobal("EventSource", FakeEventSource);
}

function okHealthHandler(analysisResponse: () => Promise<Response>) {
  return stubFetch((url, init) => {
    if (url === "/api/v1/health") {
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
    }
    if (url === "/api/v1/analysis") {
      return analysisResponse();
    }
    return Promise.reject(new Error(`Unexpected fetch: ${url} ${init?.method ?? "GET"}`));
  });
}

const singleResult: NormalizedAnalysis = {
  analysis_id: "a".repeat(32),
  original_url: "https://www.youtube.com/watch?v=abc123",
  source_id: "abc123",
  detected_platform: "youtube",
  detected_platform_label: "YouTube",
  extractor: "Youtube",
  title: "Test Video",
  uploader: "Test Channel",
  duration_seconds: 205.4,
  thumbnail_url: null,
  content_type: "single",
  formats: [
    {
      variant_id: "video-best",
      format_id: "137",
      label: "Best Available (1080p)",
      kind: "video_only",
      ext: "mp4",
      resolution_label: "1080p",
      width: 1920,
      height: 1080,
      fps: 30,
      video_codec: "h264",
      audio_codec: null,
      bitrate_kbps: null,
      filesize_bytes: 95_000_000,
      filesize_approx_bytes: null,
    },
    {
      variant_id: "video-1080",
      format_id: "137",
      label: "1080p",
      kind: "video_only",
      ext: "mp4",
      resolution_label: "1080p",
      width: 1920,
      height: 1080,
      fps: 30,
      video_codec: "h264",
      audio_codec: null,
      bitrate_kbps: null,
      filesize_bytes: 95_000_000,
      filesize_approx_bytes: null,
    },
    {
      variant_id: "video-720",
      format_id: "22",
      label: "720p",
      kind: "muxed",
      ext: "mp4",
      resolution_label: "720p",
      width: 1280,
      height: 720,
      fps: 25,
      video_codec: "h264",
      audio_codec: "aac",
      bitrate_kbps: null,
      filesize_bytes: 30_000_000,
      filesize_approx_bytes: null,
    },
    {
      variant_id: "audio-opus-160",
      format_id: "251",
      label: null,
      kind: "audio_only",
      ext: "webm",
      resolution_label: null,
      width: null,
      height: null,
      fps: null,
      video_codec: null,
      audio_codec: "opus",
      bitrate_kbps: 160,
      filesize_bytes: 4_100_000,
      filesize_approx_bytes: null,
    },
  ],
  collection: null,
  platform_hint: null,
  platform_mismatch: false,
};

const collectionResult: NormalizedAnalysis = {
  ...singleResult,
  detected_platform: "soundcloud",
  detected_platform_label: "SoundCloud",
  title: "Test Album",
  uploader: "Test Artist",
  duration_seconds: null,
  content_type: "collection",
  formats: [],
  collection: {
    title: "Test Album",
    total_items: 8,
    truncated: true,
    preview_items: [
      {
        source_id: "t1",
        url: null,
        title: "Track One",
        duration_seconds: 120,
        position: 1,
      },
      {
        source_id: "t2",
        url: null,
        title: "Track Two",
        duration_seconds: 140,
        position: 2,
      },
    ],
    items: [],
    selection_scope: "legacy_preview",
    analysis_window_limit: 25,
    analyzed_items: 0,
    selectable_items: 0,
    selected_item_limit: 200,
    total_items_known: false,
    selection_truncated: true,
    selection_limit_message:
      "v0.1 allows at most 200 selected items per job. Only selectable items in this analysis snapshot are available; unseen items are not included.",
  },
};

const queuedJob: Job = {
  id: "job-1",
  analysis_id: singleResult.analysis_id,
  variant_id: singleResult.formats[0].variant_id,
  status: "queued",
  title: "Test Video",
  source_url: singleResult.original_url,
  destination: "app_library",
  progress: {
    percent: 0,
    downloaded_bytes: null,
    total_bytes: null,
    speed_bytes_per_second: null,
    eta_seconds: null,
  },
  final_path: null,
  error: null,
  created_at: "2026-09-28T12:00:00Z",
  updated_at: "2026-09-28T12:00:00Z",
  started_at: null,
  completed_at: null,
};

const failedRetryJob: Job = {
  ...queuedJob,
  status: "failed",
  error: { code: "download_failed", message: "Download failed on first attempt.", retryable: true },
  updated_at: "2026-09-28T12:00:03.123456Z",
  completed_at: "2026-09-28T12:00:03.123456Z",
};
const retriedJob: Job = {
  ...queuedJob,
  updated_at: "2026-09-28T12:00:03.123457Z",
};

function retryFlowHandler(
  retry: () => Promise<Response>,
  read: () => Promise<Response> = () => Promise.resolve(jsonResponse(failedRetryJob)),
  create: () => Job = () => queuedJob,
  cancel?: () => Promise<Response>,
) {
  stubEventSource();
  return stubFetch((url, init) => {
    if (url === "/api/v1/health") return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
    if (url === "/api/v1/analysis") return Promise.resolve(jsonResponse(singleResult));
    if (url === "/api/v1/jobs" && init?.method === "POST") return Promise.resolve(jsonResponse(create()));
    if (url === "/api/v1/jobs/job-1/retry") return retry();
    if (url === "/api/v1/jobs/job-1/cancel" && cancel) return cancel();
    if (url === "/api/v1/jobs/job-1") return read();
    if (url.endsWith("/activity")) return Promise.resolve(jsonResponse({ events: [] }));
    return Promise.reject(new Error(`Unexpected fetch: ${url}`));
  });
}

async function showRetryableFailure() {
  const { user } = await renderNewDownload();
  await createQueuedJob(user);
  act(() => FakeEventSource.current?.emit("job.failed", failedRetryJob));
  await screen.findByRole("button", { name: "Retry" });
  return user;
}

function jobFlowHandler(cancelResponse?: Job) {
  stubEventSource();
  return stubFetch((url, init) => {
    if (url === "/api/v1/health") {
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
    }
    if (url === "/api/v1/analysis") {
      return Promise.resolve(jsonResponse(singleResult));
    }
    if (url === `/api/v1/jobs/${queuedJob.id}/cancel`) {
      return Promise.resolve(jsonResponse(cancelResponse));
    }
    if (url === "/api/v1/jobs" && init?.method === "POST") {
      return Promise.resolve(jsonResponse(queuedJob));
    }
    return Promise.reject(new Error(`Unexpected fetch: ${url} ${init?.method ?? "GET"}`));
  });
}

async function analyzeSingleResult(user: ReturnType<typeof userEvent.setup>) {
  await fillAndSubmit(user, singleResult.original_url);
  await screen.findByText(singleResult.title ?? "Test Video");
}

async function createQueuedJob(user: ReturnType<typeof userEvent.setup>) {
  await analyzeSingleResult(user);
  await user.click(screen.getByRole("button", { name: "Download" }));
  await screen.findByText("Queued");
  await waitFor(() => expect(FakeEventSource.current).not.toBeNull());
}

async function renderNewDownload() {
  render(<App />);
  const user = userEvent.setup();
  return { user };
}

async function fillAndSubmit(user: ReturnType<typeof userEvent.setup>, url: string) {
  await user.type(screen.getByLabelText("URL"), url);
  await user.click(screen.getByRole("button", { name: "Analyze" }));
}

describe("NewDownloadPage", () => {
  it("only offers Retry for a retryable failed job", async () => {
    retryFlowHandler(() => Promise.resolve(jsonResponse(retriedJob)));
    await showRetryableFailure();
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
    const statuses = ["queued", "analyzing", "downloading", "post_processing", "finalizing", "completed", "cancelled", "failed"] as const;
    for (const [index, status] of statuses.entries()) {
      act(() => FakeEventSource.current?.emit("job.updated", {
        ...failedRetryJob, status, error: { ...failedRetryJob.error, retryable: status !== "failed" },
        // Each independent status transition is a strictly newer snapshot.
        updated_at: `2026-09-28T12:00:${String(4 + index).padStart(2, "0")}Z`,
      }));
      expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    }
  });

  it("posts an empty payload to the same id once, preserves Activity and resumes polling", async () => {
    const pending = deferred<Response>();
    const fetchMock = retryFlowHandler(() => pending.promise, () => Promise.resolve(jsonResponse({
      ...retriedJob, status: "downloading", updated_at: "2026-09-28T12:00:04Z",
    })));
    const user = await showRetryableFailure();
    act(() => FakeEventSource.current?.emit("activity.created", {
      id: 1, job_id: queuedJob.id, level: "error", event_type: "job.failed", message: "Historical attempt", created_at: failedRetryJob.updated_at,
    }));
    await user.click(screen.getByRole("button", { name: /Activity/ }));
    expect(screen.getByText("Historical attempt")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry" });
    act(() => { retry.click(); retry.click(); });
    expect(screen.getByRole("button", { name: "Retrying…" })).toBeDisabled();
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/retry"));
    expect(calls).toHaveLength(1);
    expect(calls[0]?.[0]).toBe("/api/v1/jobs/job-1/retry");
    expect(calls[0]?.[1]?.method).toBe("POST");
    expect(JSON.parse(String(calls[0]?.[1]?.body))).toEqual({});
    vi.useFakeTimers();
    try {
      await act(async () => pending.resolve(jsonResponse(retriedJob)));
      expect(screen.getByText("Queued")).toBeInTheDocument();
      expect(screen.getByRole("progressbar")).toBeInTheDocument();
      expect(screen.getByText("Historical attempt")).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
      expect(screen.getByText("Downloading")).toBeInTheDocument();
    } finally { vi.useRealTimers(); }
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/v1/jobs")).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/v1/analysis")).toHaveLength(1);
    act(() => FakeEventSource.current?.emit("job.completed", {
      ...retriedJob, status: "completed", updated_at: "2026-09-28T12:00:05Z",
    }));
    expect(screen.getByText("Completed")).toBeInTheDocument();
  });

  it.each(["downloading", "completed", "failed"] as const)("does not let late queued HTTP or old terminal SSE overwrite newer %s", async (status) => {
    const pending = deferred<Response>();
    retryFlowHandler(() => pending.promise);
    const user = await showRetryableFailure();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    act(() => FakeEventSource.current?.emit("job.failed", failedRetryJob));
    act(() => FakeEventSource.current?.emit("job.updated", {
      ...retriedJob, status, error: status === "failed" ? failedRetryJob.error : null,
      updated_at: "2026-09-28T12:00:03.123458Z",
    }));
    await act(async () => pending.resolve(jsonResponse(retriedJob)));
    expect(screen.getByText(status === "downloading" ? "Downloading" : status === "completed" ? "Completed" : "Failed")).toBeInTheDocument();
    expect(screen.queryByText("Queued")).not.toBeInTheDocument();
  });

  describe.each(["downloading", "completed"] as const)("equal-time snapshots after %s", (status) => {
    it.each(["queued", "failed", "downloading"] as const)("ignores stale %s SSE without contaminating current state", async (staleStatus) => {
      retryFlowHandler(() => Promise.resolve(jsonResponse(retriedJob)));
      await showRetryableFailure();
      const current: Job = { ...retriedJob, status, progress: { ...retriedJob.progress, percent: 64 } };
      act(() => FakeEventSource.current?.emit("job.updated", current));
      act(() => FakeEventSource.current?.emit("job.updated", {
        ...failedRetryJob, status: staleStatus, updated_at: current.updated_at,
      }));
      expect(screen.getByText(status === "downloading" ? "Downloading" : "Completed")).toBeInTheDocument();
      expect(screen.queryByText(failedRetryJob.error!.message)).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
      if (status === "downloading") expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "64");
    });

    it.each(["POST", "GET"])("keeps current state after delayed equal-time retry %s", async (phase) => {
      const post = deferred<Response>();
      const read = deferred<Response>();
      const fetchMock = retryFlowHandler(() => post.promise, () => read.promise);
      const user = await showRetryableFailure();
      await user.click(screen.getByRole("button", { name: "Retry" }));
      if (phase === "GET") {
        await act(async () => post.resolve(jsonResponse({ error: { message: "Retry response lost" } }, 503)));
        await screen.findByText("Retry response lost");
      }
      act(() => FakeEventSource.current?.emit("job.updated", { ...retriedJob, status }));
      await act(async () => {
        if (phase === "POST") post.resolve(jsonResponse(retriedJob));
        else read.resolve(jsonResponse({ ...failedRetryJob, updated_at: retriedJob.updated_at }));
      });
      expect(screen.getByText(status === "downloading" ? "Downloading" : "Completed")).toBeInTheDocument();
      expect(screen.queryByText("Queued")).not.toBeInTheDocument();
      expect(screen.queryByText(failedRetryJob.error!.message)).not.toBeInTheDocument();
      expect(screen.queryByText("Retry response lost")).not.toBeInTheDocument();
      expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/retry"))).toHaveLength(1);
    });
  });

  it.each(["queued", "failed"] as const)("rejects a delayed equal-time %s poll after downloading SSE", async (status) => {
    const poll = deferred<Response>();
    const fetchMock = retryFlowHandler(() => Promise.resolve(jsonResponse(retriedJob)), () => poll.promise);
    const { user } = await renderNewDownload();
    await createQueuedJob(user);
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/jobs/job-1")).toBe(true), { timeout: 3000 });
    act(() => FakeEventSource.current?.emit("job.updated", {
      ...retriedJob, status: "downloading", progress: { ...retriedJob.progress, percent: 64 },
    }));
    await act(async () => poll.resolve(jsonResponse({ ...failedRetryJob, status, updated_at: retriedJob.updated_at })));
    expect(screen.getByText("Downloading")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "64");
    expect(screen.queryByText(failedRetryJob.error!.message)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("ignores repeated equivalent timestamps but accepts strictly newer sub-millisecond snapshots", async () => {
    retryFlowHandler(() => Promise.resolve(jsonResponse(retriedJob)));
    await showRetryableFailure();
    const current: Job = { ...retriedJob, status: "downloading", progress: { ...retriedJob.progress, percent: 64 } };
    act(() => FakeEventSource.current?.emit("job.updated", current));
    for (const updated_at of [current.updated_at, "2026-09-28T12:00:03.1234570Z", current.updated_at]) {
      act(() => FakeEventSource.current?.emit("job.updated", { ...current, updated_at }));
      act(() => FakeEventSource.current?.emit("job.updated", {
        ...current, updated_at, progress: { ...current.progress, percent: 0 },
      }));
      expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "64");
    }
    act(() => FakeEventSource.current?.emit("job.failed", failedRetryJob));
    expect(screen.getByText("Downloading")).toBeInTheDocument();
    act(() => FakeEventSource.current?.emit("job.completed", {
      ...current, status: "completed", updated_at: "2026-09-28T12:00:03.123458Z",
    }));
    expect(screen.getByText("Completed")).toBeInTheDocument();
  });

  it.each(["api", "network"])("shows %s retry failure and permits an explicit reattempt after reconciliation", async (kind) => {
    let attempts = 0;
    const fetchMock = retryFlowHandler(() => {
      attempts += 1;
      if (attempts > 1) return Promise.resolve(jsonResponse(retriedJob));
      return kind === "api"
        ? Promise.resolve(jsonResponse({ error: { message: "Retry is unavailable", code: "job_not_retryable", retryable: false } }, 409))
        : Promise.reject(new TypeError("Failed to fetch"));
    });
    const user = await showRetryableFailure();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText(kind === "api" ? "Retry is unavailable" : /Could not retry the download/);
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled());
    expect(attempts).toBe(1);
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/jobs/job-1")).toBe(true);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Queued");
    expect(attempts).toBe(2);
    expect(screen.queryByText(/Could not retry|Retry is unavailable/)).not.toBeInTheDocument();
  });

  it("reconciles a lost mutation response with the current active job without automatic retry", async () => {
    const fetchMock = retryFlowHandler(() => Promise.reject(new TypeError("network")), () => Promise.resolve(jsonResponse(retriedJob)));
    const user = await showRetryableFailure();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Queued");
    expect(screen.queryByText(/Could not retry the download/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    act(() => FakeEventSource.current?.emit("job.completed", {
      ...retriedJob, status: "completed", updated_at: "2026-09-28T12:00:05Z",
    }));
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.queryByText(/Could not retry the download/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/retry"))).toHaveLength(1);
  });

  it("clears a lost-response alarm when GET confirms a completed attempt", async () => {
    const fetchMock = retryFlowHandler(() => Promise.reject(new TypeError("network")),
      () => Promise.resolve(jsonResponse({ ...retriedJob, status: "completed", updated_at: "2026-09-28T12:00:05Z" })));
    const user = await showRetryableFailure();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Completed");
    expect(screen.queryByText(/Could not retry the download/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/retry"))).toHaveLength(1);
  });

  it.each(["api", "network"])("preserves the %s mutation error when reconciliation GET fails", async (kind) => {
    const fetchMock = retryFlowHandler(() => kind === "api"
      ? Promise.resolve(jsonResponse({ error: { message: "Retry is unavailable", code: "job_not_retryable", retryable: false } }, 409))
      : Promise.reject(new TypeError("network")), () => Promise.reject(new TypeError("GET failed")));
    const user = await showRetryableFailure();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled());
    expect(screen.getByText(kind === "api" ? "Retry is unavailable" : /Could not retry the download/)).toBeInTheDocument();
    expect(screen.getByText(failedRetryJob.error!.message)).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/retry"))).toHaveLength(1);
  });

  describe.each(["POST", "GET"])("newer SSE while %s is delayed", (phase) => {
    it.each(["downloading", "completed", "failed"] as const)("preserves newer %s without a false mutation alarm", async (status) => {
      const post = deferred<Response>();
      const read = deferred<Response>();
      const fetchMock = retryFlowHandler(() => post.promise, () => read.promise);
      const user = await showRetryableFailure();
      await user.click(screen.getByRole("button", { name: "Retry" }));
      if (phase === "GET") {
        await act(async () => post.resolve(jsonResponse({ error: { message: "Retry response lost" } }, 503)));
        await screen.findByText("Retry response lost");
      }
      act(() => FakeEventSource.current?.emit("job.updated", {
        ...retriedJob, status, updated_at: "2026-09-28T12:00:05Z",
        error: status === "failed" ? { code: "download_failed", message: "The new attempt failed.", retryable: true } : null,
      }));
      if (phase === "POST") {
        await act(async () => post.resolve(jsonResponse({ error: { message: "Retry response lost" } }, 503)));
      }
      await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/jobs/job-1")).toBe(true));
      // Even a successful but older reconciliation must not replace the SSE state.
      await act(async () => read.resolve(jsonResponse(retriedJob)));
      expect(screen.getByText(status === "downloading" ? "Downloading" : status === "completed" ? "Completed" : "Failed")).toBeInTheDocument();
      expect(screen.queryByText("Queued")).not.toBeInTheDocument();
      expect(screen.queryByText("Retry response lost")).not.toBeInTheDocument();
      if (status === "failed") expect(screen.getByText("The new attempt failed.")).toBeInTheDocument();
      expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/retry"))).toHaveLength(1);
    });
  });

  it("keeps newer SSE state when reconciliation returns the old failure", async () => {
    const read = deferred<Response>();
    retryFlowHandler(() => Promise.reject(new TypeError("network")), () => read.promise);
    const user = await showRetryableFailure();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText(/Could not retry the download/);
    act(() => FakeEventSource.current?.emit("job.completed", {
      ...retriedJob, status: "completed", updated_at: "2026-09-28T12:00:05Z",
    }));
    await act(async () => read.resolve(jsonResponse(failedRetryJob)));
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.queryByText(/Could not retry the download/)).not.toBeInTheDocument();
  });

  it("does not reinstate the mutation alarm when GET fails after newer SSE", async () => {
    const read = deferred<Response>();
    retryFlowHandler(() => Promise.reject(new TypeError("network")), () => read.promise);
    const user = await showRetryableFailure();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText(/Could not retry the download/);
    act(() => FakeEventSource.current?.emit("job.updated", retriedJob));
    await act(async () => read.resolve(jsonResponse({ error: { message: "GET failed" } }, 503)));
    expect(screen.getByText("Queued")).toBeInTheDocument();
    expect(screen.queryByText(/Could not retry the download|GET failed/)).not.toBeInTheDocument();
  });

  it.each(["switch", "unmount"])("ignores delayed reconciliation after job %s", async (action) => {
    const read = deferred<Response>();
    let creates = 0;
    const fetchMock = retryFlowHandler(() => Promise.reject(new TypeError("network")), () => read.promise,
      () => ++creates === 1 ? queuedJob : { ...queuedJob, id: "job-2", title: "Another job" });
    const view = render(<App />);
    const user = userEvent.setup();
    await createQueuedJob(user);
    act(() => FakeEventSource.current?.emit("job.failed", failedRetryJob));
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText(/Could not retry the download/);
    if (action === "unmount") view.unmount();
    else {
      await user.click(screen.getByRole("button", { name: "Download" }));
      await screen.findByText("Another job");
    }
    const callCount = fetchMock.mock.calls.length;
    await act(async () => read.resolve(jsonResponse({ ...retriedJob, status: "completed", updated_at: "2026-09-28T12:00:05Z" })));
    expect(fetchMock.mock.calls).toHaveLength(callCount);
    if (action === "switch") {
      expect(screen.getByText("Another job")).toBeInTheDocument();
      expect(screen.getByText("Queued")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
      expect(screen.queryByText(/Completed|Could not retry the download/)).not.toBeInTheDocument();
    }
  });

  it.each(["success", "failure"])("ignores old retry %s callbacks when tracking another job", async (outcome) => {
    const pending = deferred<Response>();
    let creates = 0;
    retryFlowHandler(() => pending.promise, undefined, () => ++creates === 1 ? queuedJob : { ...queuedJob, id: "job-2", title: "Another job" });
    const user = await showRetryableFailure();
    const oldSource = FakeEventSource.current;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await user.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByText("Another job");
    await act(async () => pending.resolve(outcome === "success" ? jsonResponse(retriedJob) : jsonResponse({ error: { message: "Old retry error" } }, 409)));
    act(() => oldSource?.emit("job.failed", failedRetryJob));
    expect(screen.getByText("Another job")).toBeInTheDocument();
    expect(screen.getByText("Queued")).toBeInTheDocument();
    expect(screen.queryByText("Old retry error")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retrying…" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it("rejects an old in-flight polling response after retry starts", async () => {
    const poll = deferred<Response>();
    const fetchMock = retryFlowHandler(() => Promise.resolve(jsonResponse(retriedJob)), () => poll.promise);
    const { user } = await renderNewDownload();
    await createQueuedJob(user);
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/jobs/job-1")).toBe(true), { timeout: 3000 });
    act(() => FakeEventSource.current?.emit("job.failed", failedRetryJob));
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Queued");
    await act(async () => poll.resolve(jsonResponse(failedRetryJob)));
    expect(screen.getByText("Queued")).toBeInTheDocument();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });

  it("blocks Cancel while retry is pending despite active SSE, then allows explicit cancellation", async () => {
    const pending = deferred<Response>();
    const fetchMock = retryFlowHandler(() => pending.promise, undefined, undefined,
      () => Promise.resolve(jsonResponse({ ...retriedJob, status: "cancelled", updated_at: "2026-09-28T12:00:05Z" })));
    const user = await showRetryableFailure();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    act(() => FakeEventSource.current?.emit("job.updated", {
      ...retriedJob, status: "downloading", updated_at: "2026-09-28T12:00:04Z",
    }));
    expect(screen.getByText("Downloading")).toBeInTheDocument();
    const cancel = screen.getByRole<HTMLButtonElement>("button", { name: "Cancel" });
    expect(cancel).toBeDisabled();
    await user.click(cancel);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/cancel"))).toHaveLength(0);
    // Exercise the handler guard independently of the disabled UI.
    act(() => {
      cancel.disabled = false;
      cancel.click();
      cancel.click();
    });
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/cancel"))).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Cancelling…" })).not.toBeInTheDocument();
    await act(async () => pending.resolve(jsonResponse(retriedJob)));
    expect(screen.getByText("Downloading")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await screen.findByText("Download cancelled.");
    const cancelCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/cancel"));
    expect(cancelCalls).toHaveLength(1);
    expect(cancelCalls[0]?.[0]).toBe("/api/v1/jobs/job-1/cancel");
    expect(cancelCalls[0]?.[1]?.method).toBe("POST");
  });

  it.each(["success", "error"])("blocks Retry until pending cancel %s settles, then enables Cancel on the retried job", async (outcome) => {
    const pending = deferred<Response>();
    const fetchMock = retryFlowHandler(() => Promise.resolve(jsonResponse(retriedJob)), undefined, undefined,
      () => pending.promise);
    const { user } = await renderNewDownload();
    await createQueuedJob(user);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Cancelling…" })).toBeDisabled();
    act(() => FakeEventSource.current?.emit("job.failed", failedRetryJob));
    const retry = screen.getByRole<HTMLButtonElement>("button", { name: "Retry" });
    expect(retry).toBeDisabled();
    await user.click(retry);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/retry"))).toHaveLength(0);
    // Bypass the DOM disable to exercise the synchronous handler guard too:
    // racing events must not mutate tracking generation or start a retry.
    act(() => {
      retry.disabled = false;
      retry.click();
      retry.click();
    });
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/retry"))).toHaveLength(0);
    await act(async () => pending.resolve(outcome === "success"
      ? jsonResponse(failedRetryJob)
      : jsonResponse({ error: { message: "Cancel failed", code: "cancel_failed", retryable: false } }, 409)));
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Queued");
    const retryCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/retry"));
    expect(retryCalls).toHaveLength(1);
    expect(retryCalls[0]?.[0]).toBe("/api/v1/jobs/job-1/retry");
    expect(retryCalls[0]?.[1]?.method).toBe("POST");
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    act(() => FakeEventSource.current?.emit("job.updated", {
      ...retriedJob, status: "downloading", updated_at: "2026-09-28T12:00:04Z",
    }));
    expect(screen.getByText("Downloading")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Cancelling…" })).not.toBeInTheDocument();
  });

  it("does not let a stale cancel callback contaminate the next tracked job", async () => {
    const pending = deferred<Response>();
    let creates = 0;
    retryFlowHandler(() => Promise.resolve(jsonResponse(retriedJob)), undefined,
      () => ++creates === 1 ? queuedJob : { ...queuedJob, id: "job-2", title: "Another job" },
      () => pending.promise);
    const user = await showRetryableFailure();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Queued");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Cancelling…" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByText("Another job");
    await act(async () => pending.resolve(jsonResponse({ ...retriedJob, status: "cancelled", updated_at: "2026-09-28T12:00:05Z" })));
    expect(screen.getByText("Queued")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    expect(screen.queryByText("Download cancelled.")).not.toBeInTheDocument();
  });

  it("ignores a pending retry response after unmount without starting reconciliation", async () => {
    const pending = deferred<Response>();
    const fetchMock = retryFlowHandler(() => pending.promise);
    const view = render(<App />);
    const user = userEvent.setup();
    await createQueuedJob(user);
    act(() => FakeEventSource.current?.emit("job.failed", failedRetryJob));
    await user.click(screen.getByRole("button", { name: "Retry" }));
    view.unmount();
    const callCount = fetchMock.mock.calls.length;
    await act(async () => pending.resolve(jsonResponse({ error: { message: "Too late" } }, 409)));
    expect(fetchMock.mock.calls).toHaveLength(callCount);
  });

  it("renders the analyze form with platform options", async () => {
    okHealthHandler(() => Promise.resolve(jsonResponse(singleResult)));
    await renderNewDownload();

    expect(screen.getByLabelText("URL")).toBeInTheDocument();
    const platformSelect = screen.getByLabelText("Platform") as HTMLSelectElement;
    expect(platformSelect.value).toBe("auto");
    expect(platformSelect.options).toHaveLength(12);
    expect(Array.from(platformSelect.options).map((option) => option.text)).toEqual([
      "Auto Select",
      "YouTube",
      "YouTube Music",
      "SoundCloud",
      "Bandcamp",
      "Vimeo",
      "Twitch",
      "TikTok",
      "Instagram",
      "X / Twitter",
      "Reddit",
      "Other",
    ]);
    expect(screen.getByRole("button", { name: "Analyze" })).toBeDisabled();
    expect(await screen.findByText("Backend online")).toBeInTheDocument();
  });

  it("names the form with its Add Media panel heading", async () => {
    okHealthHandler(() => Promise.resolve(jsonResponse(singleResult)));
    await renderNewDownload();

    expect(screen.getByRole("heading", { name: "Add Media" })).toBeInTheDocument();
    // The form is programmatically bound to the heading via its accessible name.
    expect(screen.getByRole("form", { name: "Add Media" })).toBeInTheDocument();
  });

  it("sends the analyze request with auto platform and renders the result", async () => {
    const fetchMock = okHealthHandler(() => Promise.resolve(jsonResponse(singleResult)));
    const { user } = await renderNewDownload();

    await fillAndSubmit(user, "https://www.youtube.com/watch?v=abc123");
    await screen.findByText("Test Video");

    const analysisCall = fetchMock.mock.calls.find(([url]) => url === "/api/v1/analysis");
    expect(analysisCall).toBeDefined();
    const [, init] = analysisCall as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual<AnalyzeRequest>({
      url: "https://www.youtube.com/watch?v=abc123",
      platform_hint: null,
    });

    expect(screen.getByText("YouTube", { selector: ".badge-platform" })).toBeInTheDocument();
    expect(screen.getByText("Test Channel")).toBeInTheDocument();
    expect(screen.getByText("3:25")).toBeInTheDocument();
    expect(screen.getByText(/Best Available \(1080p\)/)).toBeInTheDocument();
    expect(screen.getByText(/~160 kbps/)).toBeInTheDocument();
    expect(screen.getAllByText(/90\.6 MiB/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Download" })).toBeEnabled();
  });

  it("organizes the result into a Media Preview and a Format & Destination panel", async () => {
    okHealthHandler(() => Promise.resolve(jsonResponse(singleResult)));
    const { user } = await renderNewDownload();

    await fillAndSubmit(user, singleResult.original_url);
    await screen.findByText("Test Video");

    // Sequential plates continue the Add Media panel (01) and stay decorative.
    expect(screen.getByText("02")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("03")).toHaveAttribute("aria-hidden", "true");

    const preview = screen.getByRole("region", { name: "Media Preview" });
    expect(
      within(preview).getByRole("heading", { name: "Test Video", level: 3 }),
    ).toBeInTheDocument();
    expect(
      within(preview).getByText("YouTube", { selector: ".badge-platform" }),
    ).toBeInTheDocument();
    expect(within(preview).getByText("Single item")).toBeInTheDocument();
    expect(within(preview).getByText("Test Channel")).toBeInTheDocument();
    // Media Preview only describes the media; no selection controls or
    // download action live there.
    expect(within(preview).queryByRole("radio")).not.toBeInTheDocument();
    expect(within(preview).queryByRole("button")).not.toBeInTheDocument();

    const selection = screen.getByRole("region", { name: "Format & Destination" });
    expect(
      within(selection).getByRole("heading", { name: "Video formats" }),
    ).toBeInTheDocument();
    expect(
      within(selection).getByRole("heading", { name: "Audio formats" }),
    ).toBeInTheDocument();
    expect(within(selection).getByRole("group", { name: "Save to" })).toBeInTheDocument();

    // Preserved selection semantics: first format selected by default,
    // App Library is the only selectable destination, Download is enabled.
    expect(within(selection).getByRole("radio", { name: /Best Available/ })).toBeChecked();
    expect(within(selection).getByRole("radio", { name: "App Library" })).toBeChecked();
    expect(within(selection).getByRole("radio", { name: "Plex Music (coming later)" })).toBeDisabled();
    const download = within(selection).getByRole("button", { name: "Download" });
    expect(download).toBeEnabled();
  });

  it("shows a loading state while analyzing", async () => {
    const pending = deferred<Response>();
    okHealthHandler(() => pending.promise);
    const { user } = await renderNewDownload();

    await fillAndSubmit(user, "https://www.youtube.com/watch?v=abc123");

    expect(screen.getByRole("button", { name: "Analyzing…" })).toBeDisabled();
    pending.resolve(jsonResponse(singleResult));
    await screen.findByText("Test Video");
  });

  it("shows a user-friendly error state", async () => {
    okHealthHandler(() =>
      Promise.resolve(
        jsonResponse(
          {
            error: {
              code: "unsupported_url",
              message: "This URL is not supported. The site may not be supported by yt-dlp.",
              retryable: false,
              analysis_id: null,
            },
          },
          422,
        ),
      ),
    );
    const { user } = await renderNewDownload();

    await fillAndSubmit(user, "https://example.com/nope");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "This URL is not supported. The site may not be supported by yt-dlp.",
    );
  });

  it("renders a collection result with a preview list", async () => {
    okHealthHandler(() => Promise.resolve(jsonResponse(collectionResult)));
    const { user } = await renderNewDownload();

    await fillAndSubmit(user, "https://soundcloud.com/artist/sets/test-album");
    await screen.findByText("Test Album");

    expect(screen.getByText("SoundCloud", { selector: ".badge-platform" })).toBeInTheDocument();
    expect(screen.getByText("Playlist")).toBeInTheDocument();
    expect(screen.getByText("8 items")).toBeInTheDocument();
    expect(screen.getByText("Track One")).toBeInTheDocument();
    expect(screen.getByText("Track Two")).toBeInTheDocument();
    expect(screen.getByText("Only a preview is shown.")).toBeInTheDocument();
    // A legacy stored preview is never promoted to choices: it stays
    // read-only and asks for a re-analysis instead of synthesizing ids.
    expect(
      screen.getByText(/This analysis predates selectable items/),
    ).toBeInTheDocument();

    // The collection preview stays read-only: items are plain list rows in
    // the Media Preview panel, never selectable variants.
    const preview = screen.getByRole("region", { name: "Media Preview" });
    const itemList = within(preview).getByRole("list");
    expect(within(itemList).getAllByRole("listitem")).toHaveLength(2);
    expect(within(itemList).queryByRole("radio")).not.toBeInTheDocument();
    expect(within(itemList).queryByRole("checkbox")).not.toBeInTheDocument();

    // With no source-derived formats there is no selectable variant, so the
    // download action stays disabled (existing behaviour).
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();
  });

  it("offers switching to the detected platform on mismatch", async () => {
    const mismatchResult: NormalizedAnalysis = {
      ...singleResult,
      platform_hint: "soundcloud",
      platform_mismatch: true,
    };
    const fetchMock = okHealthHandler(() => Promise.resolve(jsonResponse(mismatchResult)));
    const { user } = await renderNewDownload();

    await user.click(screen.getByLabelText("Platform"));
    await user.selectOptions(screen.getByLabelText("Platform"), "soundcloud");
    await fillAndSubmit(user, "https://www.youtube.com/watch?v=abc123");
    await screen.findByRole("status");

    expect(screen.getByRole("status")).toHaveTextContent(/detected as/);
    // The user edits the URL before switching; the re-run must still use the
    // URL that produced the original result.
    await user.type(screen.getByLabelText("URL"), "-edited");
    await user.click(screen.getByRole("button", { name: "Use YouTube" }));

    const analysisCalls = fetchMock.mock.calls.filter(([url]) => url === "/api/v1/analysis");
    const lastInit = analysisCalls[1]?.[1];
    const lastBody = JSON.parse(String(lastInit?.body)) as AnalyzeRequest;
    expect(lastBody.url).toBe("https://www.youtube.com/watch?v=abc123");
    expect(lastBody.platform_hint).toBe("youtube");
  });

  it("shows concise application-level video choices without stream details", async () => {
    okHealthHandler(() => Promise.resolve(jsonResponse(singleResult)));
    const { user } = await renderNewDownload();

    await fillAndSubmit(user, singleResult.original_url);
    await screen.findByText("Test Video");

    const selection = screen.getByRole("region", { name: "Format & Destination" });
    const videoGroup = within(selection).getAllByRole("radio").slice(0, 3);
    // Best Available first, then one option per real source resolution,
    // highest to lowest; sizes may stay visible.
    expect(videoGroup.map((radio) => radio.closest("label")?.textContent)).toEqual([
      "Best Available (1080p) · 90.6 MiB",
      "1080p · 90.6 MiB",
      "720p · 28.6 MiB",
    ]);
    // Low-level stream details never reach the primary video list.
    for (const radio of videoGroup) {
      const text = radio.closest("label")?.textContent ?? "";
      expect(text).not.toMatch(/video only|video \+ audio/);
      expect(text).not.toMatch(/\b(h264|vp9|av1)\b/i);
      expect(text).not.toMatch(/\b(MP4|WEBM|MKV)\b/);
      expect(text).not.toMatch(/fps/);
    }
    expect(within(selection).queryByText("video only")).not.toBeInTheDocument();
  });

  it("selects a variant and creates a job with the normalized variant id", async () => {
    const fetchMock = jobFlowHandler();
    const { user } = await renderNewDownload();

    await analyzeSingleResult(user);
    const bestVariant = screen.getByRole("radio", { name: /Best Available/ });
    const resolutionVariant = screen.getByRole("radio", { name: /^720p/ });
    // The first offered format (Best Available) is the default selection.
    expect(bestVariant).toBeChecked();

    await user.click(resolutionVariant);
    expect(resolutionVariant).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Download" }));
    expect(await screen.findByText("Queued")).toBeInTheDocument();

    const jobCall = fetchMock.mock.calls.find(
      ([url, init]) => url === "/api/v1/jobs" && init?.method === "POST",
    );
    expect(jobCall).toBeDefined();
    expect(JSON.parse(String(jobCall?.[1]?.body))).toEqual({
      analysis_id: singleResult.analysis_id,
      variant_id: "video-720",
      output_profile: "original",
      destination: "app_library",
    });
  });

  it("creates a job with the audio variant id after selecting it", async () => {
    const fetchMock = jobFlowHandler();
    const { user } = await renderNewDownload();

    await analyzeSingleResult(user);
    const audioVariant = within(
      screen.getByRole("group", { name: "Audio formats" }),
    ).getByRole("radio", { name: /opus/i });
    await user.click(audioVariant);
    expect(audioVariant).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Download" }));
    expect(await screen.findByText("Queued")).toBeInTheDocument();

    const jobCall = fetchMock.mock.calls.find(
      ([url, init]) => url === "/api/v1/jobs" && init?.method === "POST",
    );
    expect(jobCall).toBeDefined();
    expect(JSON.parse(String(jobCall?.[1]?.body))).toEqual({
      analysis_id: singleResult.analysis_id,
      variant_id: "audio-opus-160",
      output_profile: "original",
      destination: "app_library",
    });
  });

  describe("output format", () => {
    const outputGroup = () => screen.getByRole("group", { name: "Output format" });
    const outputLabels = () =>
      within(outputGroup())
        .getAllByRole("radio")
        .map((radio) => radio.closest("label")?.textContent);
    const sourceRadio = (group: string, name: RegExp) =>
      within(screen.getByRole("group", { name: group })).getByRole("radio", { name });
    const outputRadio = (name: string) =>
      within(outputGroup()).getByRole("radio", { name });
    const createBodies = (fetchMock: ReturnType<typeof stubFetch>) =>
      fetchMock.mock.calls
        .filter(([url, init]) => url === "/api/v1/jobs" && init?.method === "POST")
        .map(([, init]) => JSON.parse(String(init?.body)) as Record<string, unknown>);

    it("offers the video set by default and the audio set for an audio variant, defaulting to Original", async () => {
      jobFlowHandler();
      const { user } = await renderNewDownload();
      await analyzeSingleResult(user);

      expect(outputLabels()).toEqual(["Original", "MP4 (compatible)"]);
      expect(outputRadio("Original")).toBeChecked();
      // Output sits under the variant lists, before the destination.
      expect(
        outputGroup().compareDocumentPosition(screen.getByRole("group", { name: "Save to" })) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();

      await user.click(sourceRadio("Audio formats", /opus/i));
      expect(outputLabels()).toEqual([
        "Original / Best Available",
        "MP3",
        "WAV",
        "M4A",
        "Opus",
        "FLAC",
      ]);
      expect(outputRadio("Original / Best Available")).toBeChecked();

      // Conversion is framed as compatibility, never as a quality upgrade.
      expect(within(outputGroup()).getByText(/not a quality upgrade/)).toBeInTheDocument();
      expect(within(outputGroup()).getByText(/WAV and FLAC do not restore detail/)).toBeInTheDocument();
      expect(outputGroup().textContent).not.toMatch(/improve|better quality|higher quality|enhance/i);
      expect(screen.queryByText(/come directly from the analyzed source/)).not.toBeInTheDocument();
    });

    it("posts MP3 for an audio variant", async () => {
      const fetchMock = jobFlowHandler();
      const { user } = await renderNewDownload();
      await analyzeSingleResult(user);

      await user.click(sourceRadio("Audio formats", /opus/i));
      await user.click(outputRadio("MP3"));
      await user.click(screen.getByRole("button", { name: "Download" }));
      await screen.findByText("Queued");

      expect(createBodies(fetchMock)).toEqual([
        {
          analysis_id: singleResult.analysis_id,
          variant_id: "audio-opus-160",
          output_profile: "mp3",
          destination: "app_library",
        },
      ]);
    });

    it("posts MP4 for a video variant", async () => {
      const fetchMock = jobFlowHandler();
      const { user } = await renderNewDownload();
      await analyzeSingleResult(user);

      await user.click(outputRadio("MP4 (compatible)"));
      await user.click(screen.getByRole("button", { name: "Download" }));
      await screen.findByText("Queued");

      expect(createBodies(fetchMock)).toEqual([
        {
          analysis_id: singleResult.analysis_id,
          variant_id: "video-best",
          output_profile: "mp4",
          destination: "app_library",
        },
      ]);
    });

    it("keeps the output choice when only the source quality changes within the same kind", async () => {
      const fetchMock = jobFlowHandler();
      const { user } = await renderNewDownload();
      await analyzeSingleResult(user);

      await user.click(outputRadio("MP4 (compatible)"));
      // 720p is a muxed variant: still video, so the choice survives.
      await user.click(sourceRadio("Video formats", /^720p/));
      expect(outputRadio("MP4 (compatible)")).toBeChecked();
      await user.click(screen.getByRole("button", { name: "Download" }));
      await screen.findByText("Queued");

      expect(createBodies(fetchMock)[0]).toMatchObject({
        variant_id: "video-720",
        output_profile: "mp4",
      });
    });

    it("clears an audio MP3 choice for good across audio -> video -> audio", async () => {
      const fetchMock = jobFlowHandler();
      const { user } = await renderNewDownload();
      await analyzeSingleResult(user);

      await user.click(sourceRadio("Audio formats", /opus/i));
      await user.click(outputRadio("MP3"));
      await user.click(sourceRadio("Video formats", /^720p/));
      expect(outputRadio("Original")).toBeChecked();
      await user.click(sourceRadio("Audio formats", /opus/i));
      expect(outputRadio("Original / Best Available")).toBeChecked();
      expect(outputRadio("MP3")).not.toBeChecked();

      await user.click(screen.getByRole("button", { name: "Download" }));
      await screen.findByText("Queued");
      expect(createBodies(fetchMock)[0]).toMatchObject({
        variant_id: "audio-opus-160",
        output_profile: "original",
      });
    });

    it("resets to Original on every new analysis, including a repeated analysis id", async () => {
      const fetchMock = jobFlowHandler();
      const { user } = await renderNewDownload();
      await analyzeSingleResult(user);

      await user.click(outputRadio("MP4 (compatible)"));
      await user.click(screen.getByRole("button", { name: "Analyze" }));
      // The stale MP4 choice is still checked until the new analysis lands.
      await waitFor(() => expect(outputRadio("MP4 (compatible)")).not.toBeChecked());
      expect(fetchMock.mock.calls.filter(([url]) => url === "/api/v1/analysis")).toHaveLength(2);
      expect(outputRadio("Original")).toBeChecked();
      expect(outputRadio("MP4 (compatible)")).not.toBeChecked();

      await user.click(screen.getByRole("button", { name: "Download" }));
      await screen.findByText("Queued");
      expect(createBodies(fetchMock)[0]?.output_profile).toBe("original");
    });

    it("disables source and output radios while the create is pending", async () => {
      const pending = deferred<Response>();
      stubEventSource();
      const fetchMock = stubFetch((url, init) => {
        if (url === "/api/v1/health") return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
        if (url === "/api/v1/analysis") return Promise.resolve(jsonResponse(singleResult));
        if (url === "/api/v1/jobs" && init?.method === "POST") return pending.promise;
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      });
      const { user } = await renderNewDownload();
      await analyzeSingleResult(user);

      await user.click(outputRadio("MP4 (compatible)"));
      await user.click(screen.getByRole("button", { name: "Download" }));
      expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
      for (const radio of [
        ...within(outputGroup()).getAllByRole("radio"),
        ...within(screen.getByRole("group", { name: "Video formats" })).getAllByRole("radio"),
        ...within(screen.getByRole("group", { name: "Audio formats" })).getAllByRole("radio"),
      ]) {
        expect(radio).toBeDisabled();
      }
      expect(createBodies(fetchMock)).toHaveLength(1);

      await act(async () => pending.resolve(jsonResponse(queuedJob)));
      await screen.findByText("Queued");
      expect(createBodies(fetchMock)).toHaveLength(1);
    });

    it("hides the stale result and its output choice while a re-analysis is pending", async () => {
      const pending = deferred<Response>();
      let calls = 0;
      okHealthHandler(() => (++calls === 1 ? Promise.resolve(jsonResponse(singleResult)) : pending.promise));
      const { user } = await renderNewDownload();
      await analyzeSingleResult(user);

      await user.click(screen.getByRole("button", { name: "Analyze" }));
      expect(screen.getByRole("button", { name: "Analyzing…" })).toBeDisabled();
      expect(screen.queryByRole("group", { name: "Output format" })).not.toBeInTheDocument();
      await act(async () => pending.resolve(jsonResponse(singleResult)));
      await screen.findByRole("group", { name: "Output format" });
    });

    it("keeps the choice visible after a failed create and shows the backend error", async () => {
      stubEventSource();
      const fetchMock = stubFetch((url, init) => {
        if (url === "/api/v1/health") return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
        if (url === "/api/v1/analysis") return Promise.resolve(jsonResponse(singleResult));
        if (url === "/api/v1/jobs" && init?.method === "POST") {
          return Promise.resolve(jsonResponse({
            error: { code: "invalid_profile", message: "This output is not available for that source.", retryable: false },
          }, 422));
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      });
      const { user } = await renderNewDownload();
      await analyzeSingleResult(user);

      await user.click(outputRadio("MP4 (compatible)"));
      await user.click(screen.getByRole("button", { name: "Download" }));
      expect(await screen.findByText("This output is not available for that source.")).toBeInTheDocument();
      expect(outputRadio("MP4 (compatible)")).toBeChecked();
      expect(screen.getByRole("button", { name: "Download" })).toBeEnabled();
      expect(createBodies(fetchMock)).toHaveLength(1);
    });
  });

  it("updates active job progress from SSE", async () => {
    jobFlowHandler();
    const { user } = await renderNewDownload();
    await createQueuedJob(user);

    const downloadingJob: Job = {
      ...queuedJob,
      status: "downloading",
      progress: {
        percent: 42.4,
        downloaded_bytes: 42_400_000,
        total_bytes: 100_000_000,
        speed_bytes_per_second: 2_000_000,
        eta_seconds: 29,
      },
      started_at: "2026-09-28T12:00:01Z",
      updated_at: "2026-09-28T12:00:02Z",
    };
    act(() => FakeEventSource.current?.emit("job.updated", downloadingJob));

    expect(await screen.findByText("Downloading")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "42");
    expect(screen.getByText("42%")).toBeInTheDocument();
    expect(screen.getByText("ETA 0:29")).toBeInTheDocument();
  });

  it("shows a failed job received from SSE", async () => {
    jobFlowHandler();
    const { user } = await renderNewDownload();
    await createQueuedJob(user);

    const failedJob: Job = {
      ...queuedJob,
      status: "failed",
      error: {
        code: "download_failed",
        message: "The media download failed.",
        retryable: true,
      },
      updated_at: "2026-09-28T12:00:03Z",
      completed_at: "2026-09-28T12:00:03Z",
    };
    act(() => FakeEventSource.current?.emit("job.failed", failedJob));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The media download failed.",
    );
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("cancels an active job through the job API", async () => {
    const cancelledJob: Job = {
      ...queuedJob,
      status: "cancelled",
      error: {
        code: "cancelled",
        message: "Download cancelled.",
        retryable: false,
      },
      updated_at: "2026-09-28T12:00:03Z",
      completed_at: "2026-09-28T12:00:03Z",
    };
    const fetchMock = jobFlowHandler(cancelledJob);
    const { user } = await renderNewDownload();
    await createQueuedJob(user);

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("Download cancelled.")).toBeInTheDocument();
    expect(screen.getByText("Cancelled")).toBeInTheDocument();

    const cancelCall = fetchMock.mock.calls.find(
      ([url]) => url === `/api/v1/jobs/${queuedJob.id}/cancel`,
    );
    expect(cancelCall?.[1]?.method).toBe("POST");
    expect(JSON.parse(String(cancelCall?.[1]?.body))).toEqual({});
  });
});
