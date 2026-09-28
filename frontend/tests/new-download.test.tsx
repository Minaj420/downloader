import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/app/App";
import type { AnalyzeRequest, NormalizedAnalysis } from "../src/api/analysis";

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
      variant_id: "video-1080-video_only-h264-mp4",
      format_id: "137",
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
      variant_id: "audio-opus-160",
      format_id: "251",
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
    expect(screen.getByText(/1080p/)).toBeInTheDocument();
    expect(screen.getByText(/~160 kbps/)).toBeInTheDocument();
    expect(screen.getByText(/90\.6 MiB/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();
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
});
