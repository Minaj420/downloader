import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/app/App";
import type { ActivityEvent } from "../src/api/activity";
import type { NormalizedAnalysis } from "../src/api/analysis";
import type { Job } from "../src/api/jobs";
import { ActivityPanel } from "../src/features/new-download/ActivityPanel";
import {
  formatActivityTime,
  mergeActivityEvents,
} from "../src/features/new-download/activity";

function activityEvent(overrides: Partial<ActivityEvent> = {}): ActivityEvent {
  return {
    id: 1,
    job_id: "job-1",
    level: "info",
    event_type: "job",
    message: "Queued",
    created_at: "2026-09-28T12:00:00Z",
    ...overrides,
  };
}

describe("ActivityPanel", () => {
  it("renders collapsed by default and hides event rows", () => {
    render(
      <ActivityPanel
        events={[activityEvent()]}
        loadStatus="loaded"
        onRefresh={vi.fn()}
      />,
    );

    const toggle = screen.getByRole("button", { name: /Activity/ });
    expect(toggle).toHaveTextContent("Activity ▸");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Queued")).not.toBeInTheDocument();
  });

  it("expands on toggle and shows structured HH:MM:SS message rows", async () => {
    const user = userEvent.setup();
    render(
      <ActivityPanel
        events={[
          activityEvent({ id: 1, message: "Queued" }),
          activityEvent({ id: 2, message: "Download started" }),
        ]}
        loadStatus="loaded"
        onRefresh={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Activity/ }));

    const toggle = screen.getByRole("button", { name: /Activity/ });
    expect(toggle).toHaveTextContent("Activity ▾");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Queued");
    expect(rows[1]).toHaveTextContent("Download started");
    for (const row of rows) {
      // The gap between time and message comes from CSS, not text.
      expect(row.textContent ?? "").toMatch(/^\d{2}:\d{2}:\d{2}/);
    }
  });

  it("shows an empty state when there is no activity", async () => {
    render(<ActivityPanel events={[]} loadStatus="loaded" onRefresh={vi.fn()} />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: /Activity/ }));

    expect(screen.getByText("No activity yet.")).toBeInTheDocument();
  });

  it("shows a load error without an empty state and retries", async () => {
    const onRefresh = vi.fn();
    render(<ActivityPanel events={[]} loadStatus="error" onRefresh={onRefresh} />);

    const toggle = screen.getByRole("button", { name: /Activity/ });
    await userEvent.setup().click(toggle);

    expect(screen.getByText("Could not load activity.")).toBeInTheDocument();
    expect(screen.queryByText("No activity yet.")).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });
});

describe("mergeActivityEvents", () => {
  it("dedupes by id and sorts ascending, keeping the first-seen event", () => {
    const rest = [
      activityEvent({ id: 2, message: "Download started" }),
      activityEvent({ id: 1, message: "Queued" }),
    ];
    const live = [
      activityEvent({ id: 3, message: "Saved to App Library" }),
      activityEvent({ id: 1, message: "Queued duplicate" }),
    ];

    const merged = mergeActivityEvents(rest, live);

    expect(merged.map((event) => event.id)).toEqual([1, 2, 3]);
    expect(merged[0].message).toBe("Queued");
    expect(merged).toHaveLength(3);
  });

  it("returns existing events unchanged when nothing new arrives", () => {
    const events = [activityEvent({ id: 5 })];

    expect(mergeActivityEvents(events, [])).toEqual(events);
    expect(mergeActivityEvents([], events)).toEqual(events);
  });
});

describe("formatActivityTime", () => {
  it("formats an ISO timestamp as a local HH:MM:SS label", () => {
    expect(formatActivityTime("2026-09-28T12:00:00Z")).toMatch(
      /^\d{2}:\d{2}:\d{2}$/,
    );
  });

  it("returns an empty label for invalid timestamps", () => {
    expect(formatActivityTime("not a date")).toBe("");
  });
});

/* ---- Integration through the app (SSE + REST data flow) ---- */

class FakeEventSource {
  static current: FakeEventSource | null = null;
  static instances: FakeEventSource[] = [];

  readonly url: string;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  private readonly listeners = new Map<string, EventListener[]>();

  constructor(url: string | URL) {
    this.url = String(url);
    FakeEventSource.current = this;
    FakeEventSource.instances.push(this);
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

  open() {
    this.onopen?.();
  }

  error() {
    this.onerror?.();
  }

  close() {
    this.closed = true;
  }
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
  ],
  collection: null,
  platform_hint: null,
  platform_mismatch: false,
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

const restHistory = [
  activityEvent({ id: 1, message: "Queued" }),
  activityEvent({ id: 2, message: "Download started" }),
];

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function activityFlowHandler() {
  FakeEventSource.current = null;
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/health") {
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
    }
    if (url === "/api/v1/analysis") {
      return Promise.resolve(jsonResponse(singleResult));
    }
    if (url === "/api/v1/jobs") {
      return Promise.resolve(jsonResponse(queuedJob));
    }
    if (url === "/api/v1/jobs/job-1") {
      return Promise.resolve(jsonResponse(queuedJob));
    }
    if (url === "/api/v1/jobs/job-1/activity") {
      return Promise.resolve(jsonResponse({ events: restHistory }));
    }
    return Promise.reject(new Error(`Unexpected fetch: ${url}`));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function startJob(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("URL"), singleResult.original_url);
  await user.click(screen.getByRole("button", { name: "Analyze" }));
  await screen.findByText("Test Video");
  await user.click(screen.getByRole("button", { name: "Download" }));
  await screen.findByText("Queued");
  await waitFor(() => expect(FakeEventSource.current).not.toBeNull());
}

async function createJobWithActivityPanel(user: ReturnType<typeof userEvent.setup>) {
  await startJob(user);
  act(() => FakeEventSource.current?.open());
  const toggle = await screen.findByRole("button", { name: /Activity/ });
  await user.click(toggle);
  await screen.findByText("Download started");
}

describe("NewDownloadPage activity flow", () => {
  it("does not start initial REST history until the EventSource opens", async () => {
    const fetchMock = activityFlowHandler();
    render(<App />);
    await startJob(userEvent.setup());

    expect(
      fetchMock.mock.calls.filter(([url]) => url === "/api/v1/jobs/job-1/activity"),
    ).toHaveLength(0);

    act(() => FakeEventSource.current?.open());
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(
          ([url]) => url === "/api/v1/jobs/job-1/activity",
        ),
      ).toHaveLength(1),
    );
  });

  it("loads REST history on expand and appends matching live SSE events", async () => {
    activityFlowHandler();
    render(<App />);
    const user = userEvent.setup();
    await createJobWithActivityPanel(user);

    // Live event for the same job is appended; a different job is filtered.
    act(() => {
      FakeEventSource.current?.emit(
        "activity.created",
        activityEvent({ id: 3, message: "Saved to App Library" }),
      );
      FakeEventSource.current?.emit(
        "activity.created",
        activityEvent({ id: 4, job_id: "someone-elses-job", message: "Other job" }),
      );
    });

    await waitFor(() => {
      expect(screen.getByText("Saved to App Library")).toBeInTheDocument();
    });
    const panel = within(screen.getByLabelText("Job activity"));
    const rows = panel.getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(panel.queryByText("Other job")).not.toBeInTheDocument();
  });

  it("merges live events received during pending REST without gaps or duplicates", async () => {
    const pending = deferred<Response>();
    activityFlowHandler().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/health") {
        return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
      }
      if (url === "/api/v1/analysis") return Promise.resolve(jsonResponse(singleResult));
      if (url === "/api/v1/jobs") return Promise.resolve(jsonResponse(queuedJob));
      if (url === "/api/v1/jobs/job-1/activity") return pending.promise;
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    render(<App />);
    const user = userEvent.setup();
    await startJob(user);

    act(() => FakeEventSource.current?.open());
    act(() => {
      FakeEventSource.current?.emit(
        "activity.created",
        activityEvent({ id: 2, message: "Download started" }),
      );
      FakeEventSource.current?.emit(
        "activity.created",
        activityEvent({ id: 3, message: "Saved to App Library" }),
      );
    });
    pending.resolve(
      jsonResponse({
        events: [
          activityEvent({ id: 1, message: "Queued" }),
          activityEvent({ id: 2, message: "Download started" }),
        ],
      }),
    );

    await user.click(screen.getByRole("button", { name: /Activity/ }));
    await screen.findByText("Saved to App Library");
    const rows = within(screen.getByLabelText("Job activity")).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Queued"),
      expect.stringContaining("Download started"),
      expect.stringContaining("Saved to App Library"),
    ]);
  });

  it("retries a failed initial REST load with a new successful request", async () => {
    const fetchMock = activityFlowHandler();
    let activityRequests = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/health") {
        return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
      }
      if (url === "/api/v1/analysis") return Promise.resolve(jsonResponse(singleResult));
      if (url === "/api/v1/jobs") return Promise.resolve(jsonResponse(queuedJob));
      if (url === "/api/v1/jobs/job-1/activity") {
        activityRequests += 1;
        return Promise.resolve(
          activityRequests === 1
            ? jsonResponse({ error: { message: "unavailable" } }, 503)
            : jsonResponse({ events: restHistory }),
        );
      }
      return Promise.reject(new Error(`Unexpected fetch: ${url}`));
    });
    render(<App />);
    const user = userEvent.setup();
    await startJob(user);
    act(() => FakeEventSource.current?.open());
    await user.click(screen.getByRole("button", { name: /Activity/ }));

    expect(await screen.findByText("Could not load activity.")).toBeInTheDocument();
    expect(screen.queryByText("No activity yet.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Download started")).toBeInTheDocument();
    expect(activityRequests).toBe(2);
  });

  it("refreshes REST history when an opened EventSource reports an error", async () => {
    const fetchMock = activityFlowHandler();
    render(<App />);
    await startJob(userEvent.setup());
    act(() => FakeEventSource.current?.open());
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(
          ([url]) => url === "/api/v1/jobs/job-1/activity",
        ),
      ).toHaveLength(1),
    );

    act(() => FakeEventSource.current?.error());

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(
          ([url]) => url === "/api/v1/jobs/job-1/activity",
        ),
      ).toHaveLength(2),
    );
  });

  it("resets on job switch and ignores stale REST history from the previous job", async () => {
    const firstHistory = deferred<Response>();
    const secondJob: Job = { ...queuedJob, id: "job-2", title: "Second Video" };
    FakeEventSource.current = null;
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    let createCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/v1/health") {
          return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
        }
        if (url === "/api/v1/analysis") return Promise.resolve(jsonResponse(singleResult));
        if (url === "/api/v1/jobs") {
          createCount += 1;
          return Promise.resolve(jsonResponse(createCount === 1 ? queuedJob : secondJob));
        }
        if (url === "/api/v1/jobs/job-1/activity") return firstHistory.promise;
        if (url === "/api/v1/jobs/job-2/activity") {
          return Promise.resolve(
            jsonResponse({
              events: [
                activityEvent({
                  id: 20,
                  job_id: "job-2",
                  message: "Second queued",
                }),
              ],
            }),
          );
        }
        return Promise.reject(new Error(`Unexpected fetch: ${url}`));
      }),
    );
    render(<App />);
    const user = userEvent.setup();
    await startJob(user);
    const firstSource = FakeEventSource.current as unknown as FakeEventSource;
    act(() => firstSource.open());
    await user.click(screen.getByRole("button", { name: /Activity/ }));
    act(() =>
      firstSource.emit(
        "activity.created",
        activityEvent({ id: 10, message: "First live" }),
      ),
    );
    expect(await screen.findByText("First live")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByText("Second Video");
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(2));
    expect(firstSource.closed).toBe(true);
    expect(screen.getByRole("button", { name: /Activity/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("First live")).not.toBeInTheDocument();

    act(() => FakeEventSource.current?.open());
    await user.click(screen.getByRole("button", { name: /Activity/ }));
    expect(await screen.findByText("Second queued")).toBeInTheDocument();
    firstHistory.resolve(
      jsonResponse({ events: [activityEvent({ id: 1, message: "Stale first" })] }),
    );
    await act(async () => Promise.resolve());

    expect(screen.queryByText("Stale first")).not.toBeInTheDocument();
    expect(screen.getByText("Second queued")).toBeInTheDocument();
  });
});
