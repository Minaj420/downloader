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
      destination: "app_library",
    });
  });

  it("creates a job with the audio variant id after selecting it", async () => {
    const fetchMock = jobFlowHandler();
    const { user } = await renderNewDownload();

    await analyzeSingleResult(user);
    const audioVariant = screen.getByRole("radio", { name: /opus/i });
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
      destination: "app_library",
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
