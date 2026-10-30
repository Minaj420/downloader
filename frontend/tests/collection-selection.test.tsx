import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/app/App";
import type {
  CollectionInfo,
  CollectionSelectionItem,
  NormalizedAnalysis,
} from "../src/api/analysis";
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

const ALBUM_URL = "https://soundcloud.com/artist/sets/test-album";
const SECOND_ALBUM_URL = "https://soundcloud.com/artist/sets/second-album";

function selectionItem(
  overrides: Partial<CollectionSelectionItem>,
): CollectionSelectionItem {
  return {
    item_id: "item-x",
    source_order: 0,
    selectable: true,
    unavailable_reason: null,
    source_id: null,
    url: null,
    title: "Untitled",
    duration_seconds: null,
    position: null,
    ...overrides,
  };
}

// Snapshot with nil/duplicate source ids, a position gap, unavailable rows
// and an unknown (lower-bound) source total.
const windowCollection: CollectionInfo = {
  title: "Test Album",
  total_items: 500,
  preview_items: [],
  truncated: false,
  items: [
    selectionItem({
      item_id: "item-a",
      source_order: 0,
      source_id: "t1",
      url: `${ALBUM_URL}/track-a`,
      title: "Track One",
      duration_seconds: 100,
      position: 1,
    }),
    selectionItem({
      item_id: "item-b",
      source_order: 1,
      selectable: false,
      unavailable_reason: "invalid_url",
      source_id: null,
      url: null,
      title: "Track Two",
      duration_seconds: 140,
      position: 2,
    }),
    selectionItem({
      item_id: "item-c",
      source_order: 2,
      source_id: "t1", // duplicate source id: never an identity
      url: `${ALBUM_URL}/track-c`,
      title: "Track Three",
      duration_seconds: null,
      position: null, // gap: positions are shown as provided, not renumbered
    }),
    selectionItem({
      item_id: "item-d",
      source_order: 3,
      selectable: false,
      unavailable_reason: "unavailable",
      source_id: "t4",
      url: `${ALBUM_URL}/track-d`,
      title: "Track Four",
      duration_seconds: 90,
      position: 4,
    }),
  ],
  selection_scope: "analysis_window",
  analysis_window_limit: 25,
  analyzed_items: 4,
  selectable_items: 2,
  selected_item_limit: 200,
  total_items_known: false,
  selection_truncated: true,
  selection_limit_message:
    "v0.1 allows at most 200 selected items per job. Only selectable items in this analysis snapshot are available; unseen items are not included.",
};

const knownCollection: CollectionInfo = {
  ...windowCollection,
  total_items: 8,
  items: [
    selectionItem({
      item_id: "item-a",
      source_order: 0,
      source_id: "t1",
      url: `${ALBUM_URL}/track-a`,
      title: "Track One",
      duration_seconds: 100,
      position: 1,
    }),
    selectionItem({
      item_id: "item-b",
      source_order: 1,
      source_id: "t2",
      url: `${ALBUM_URL}/track-b`,
      title: "Track Two",
      duration_seconds: 140,
      position: 2,
    }),
  ],
  analyzed_items: 2,
  selectable_items: 2,
  total_items_known: true,
  selection_truncated: false,
};

const largeCollection: CollectionInfo = {
  title: "Large Playlist",
  total_items: 205,
  preview_items: [],
  truncated: false,
  items: Array.from({ length: 205 }, (_, index) =>
    selectionItem({
      item_id: `item-${index}`,
      source_order: index,
      source_id: `t${index}`,
      url: `${ALBUM_URL}/track-${index}`,
      title: `Large Track ${index}`,
      duration_seconds: null,
      position: index + 1,
    }),
  ),
  selection_scope: "analysis_window",
  analysis_window_limit: 25,
  analyzed_items: 205,
  selectable_items: 205,
  selected_item_limit: 200,
  total_items_known: true,
  selection_truncated: false,
  selection_limit_message: windowCollection.selection_limit_message,
};

const legacyCollection: CollectionInfo = {
  title: "Old Album",
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
  selection_limit_message: windowCollection.selection_limit_message,
};

function collectionAnalysis(
  collection: CollectionInfo,
  analysisId = "c".repeat(32),
  originalUrl = ALBUM_URL,
): NormalizedAnalysis {
  return {
    analysis_id: analysisId,
    original_url: originalUrl,
    source_id: null,
    detected_platform: "soundcloud",
    detected_platform_label: "SoundCloud",
    extractor: null,
    title: collection.title,
    uploader: "Test Artist",
    duration_seconds: null,
    thumbnail_url: null,
    content_type: "collection",
    formats: [],
    collection,
    platform_hint: null,
    platform_mismatch: false,
  };
}

const analysis = collectionAnalysis(windowCollection);

const secondAnalysis = collectionAnalysis(
  {
    ...windowCollection,
    title: "Second Album",
    total_items: 2,
    items: [
      selectionItem({
        item_id: "new-item-1",
        source_order: 0,
        source_id: "n1",
        url: `${SECOND_ALBUM_URL}/track-1`,
        title: "New Track One",
        duration_seconds: 100,
        position: 1,
      }),
      selectionItem({
        item_id: "new-item-2",
        source_order: 1,
        source_id: "n2",
        url: `${SECOND_ALBUM_URL}/track-2`,
        title: "New Track Two",
        duration_seconds: 140,
        position: 2,
      }),
    ],
    analyzed_items: 2,
    selectable_items: 2,
    total_items_known: true,
    selection_truncated: false,
  },
  "d".repeat(32),
  SECOND_ALBUM_URL,
);

const collectionJob: Job = {
  id: "job-c1",
  analysis_id: analysis.analysis_id,
  variant_id: "",
  status: "queued",
  title: "Test Album",
  source_url: ALBUM_URL,
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
  created_at: "2026-10-05T12:00:00Z",
  updated_at: "2026-10-05T12:00:00Z",
  started_at: null,
  completed_at: null,
  job_type: "collection",
  collection_mode: "audio",
  collection_profile: { kind: "original_best_available" },
  total_items: 2,
  completed_items: 0,
  failed_items: 0,
};

const singleAnalysis: NormalizedAnalysis = {
  analysis_id: "s".repeat(32),
  original_url: "https://www.youtube.com/watch?v=abc123",
  source_id: "abc123",
  detected_platform: "youtube",
  detected_platform_label: "YouTube",
  extractor: null,
  title: "Test Video",
  uploader: null,
  duration_seconds: null,
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
  ],
  collection: null,
  platform_hint: null,
  platform_mismatch: false,
};

const singleJob: Job = {
  id: "job-s1",
  analysis_id: singleAnalysis.analysis_id,
  variant_id: "video-best",
  status: "queued",
  title: "Test Video",
  source_url: singleAnalysis.original_url,
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
  created_at: "2026-10-05T12:00:00Z",
  updated_at: "2026-10-05T12:00:00Z",
  started_at: null,
  completed_at: null,
};

interface CollectionFlowOptions {
  analysis?: () => Promise<Response>;
  create?: () => Promise<Response>;
}

function collectionFlowHandler(options: CollectionFlowOptions = {}) {
  stubEventSource();
  return stubFetch((url, init) => {
    if (url === "/api/v1/health") {
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
    }
    if (url === "/api/v1/analysis") {
      return options.analysis?.() ?? Promise.resolve(jsonResponse(analysis));
    }
    if (url === "/api/v1/jobs" && init?.method === "POST") {
      return options.create?.() ?? Promise.resolve(jsonResponse(collectionJob));
    }
    if (url === `/api/v1/jobs/${collectionJob.id}`) {
      return Promise.resolve(jsonResponse(collectionJob));
    }
    if (url.endsWith("/activity")) {
      return Promise.resolve(jsonResponse({ events: [] }));
    }
    return Promise.reject(
      new Error(`Unexpected fetch: ${url} ${init?.method ?? "GET"}`),
    );
  });
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

async function analyzeCollection(user: ReturnType<typeof userEvent.setup>) {
  await fillAndSubmit(user, ALBUM_URL);
  await screen.findByText("Track One");
}

type FetchCall = [input: RequestInfo | URL, init?: RequestInit];

function createRequests(fetchMock: ReturnType<typeof stubFetch>): FetchCall[] {
  return fetchMock.mock.calls.filter(
    ([url, init]) => url === "/api/v1/jobs" && init?.method === "POST",
  );
}

function createBody(request: FetchCall | undefined): Record<string, unknown> {
  return JSON.parse(String(request?.[1]?.body)) as Record<string, unknown>;
}

describe("CollectionSelection", () => {
  it("renders the analyzed snapshot in source order with stable ids, raw gaps and reasons", async () => {
    collectionFlowHandler();
    const { user } = await renderNewDownload();
    await analyzeCollection(user);

    const preview = screen.getByRole("region", { name: "Media Preview" });
    const rows = within(preview).getAllByRole("listitem");
    expect(rows).toHaveLength(4);
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Track One"),
      expect.stringContaining("Track Two"),
      expect.stringContaining("Track Three"),
      expect.stringContaining("Track Four"),
    ]);

    // Analyzed, selectable items are selected by default within the cap.
    expect(
      within(preview).getByRole("checkbox", { name: /Track One/ }),
    ).toBeChecked();
    expect(
      within(preview).getByRole("checkbox", { name: /Track Three/ }),
    ).toBeChecked();
    // Unavailable rows are disabled with their known backend reason.
    const invalid = within(preview).getByRole("checkbox", { name: /Track Two/ });
    expect(invalid).toBeDisabled();
    expect(invalid).not.toBeChecked();
    expect(screen.getByText(/no usable direct link/)).toBeInTheDocument();
    const unavailable = within(preview).getByRole("checkbox", {
      name: /Track Four/,
    });
    expect(unavailable).toBeDisabled();
    expect(screen.getByText(/unavailable in this snapshot/)).toBeInTheDocument();

    // Source positions are displayed as provided: gap kept, no renumbering.
    expect(within(preview).getByText("01", { selector: ".item-position" })).toBeInTheDocument();
    expect(within(preview).getByText("02", { selector: ".item-position" })).toBeInTheDocument();
    expect(within(preview).getByText("04", { selector: ".item-position" })).toBeInTheDocument();
    expect(
      within(preview).queryByText("03", { selector: ".item-position" }),
    ).not.toBeInTheDocument();

    // Output profile is collection-level only; no per-child controls.
    expect(within(preview).queryByRole("radio")).not.toBeInTheDocument();
    const modeGroup = screen.getByRole("group", { name: "Audio / Video" });
    expect(within(modeGroup).getAllByRole("radio")).toHaveLength(2);
  });

  it("posts the exact selected snapshot ids in source order with an explicit mode", async () => {
    const fetchMock = collectionFlowHandler();
    const { user } = await renderNewDownload();
    await analyzeCollection(user);

    await user.click(screen.getByRole("checkbox", { name: /Track One/ }));
    await user.click(screen.getByRole("radio", { name: "Audio" }));
    await user.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByText("Queued");

    expect(createRequests(fetchMock)).toHaveLength(1);
    expect(createBody(createRequests(fetchMock)[0])).toEqual({
      analysis_id: analysis.analysis_id,
      item_ids: ["item-c"],
      collection_mode: "audio",
      collection_profile: { kind: "original_best_available" },
      output_profile: "original",
      destination: "app_library",
    });
  });

  it("selects and deselects only the analyzed selectable items of the snapshot", async () => {
    collectionFlowHandler();
    const { user } = await renderNewDownload();
    await analyzeCollection(user);

    expect(screen.getByRole("heading", { name: /2 selected/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Deselect All" }));
    expect(screen.getByRole("heading", { name: /0 selected/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Track One/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Track Three/ })).not.toBeChecked();
    // An empty selection cannot start a collection job.
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Select All" }));
    expect(screen.getByRole("heading", { name: /2 selected/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Track One/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Track Three/ })).toBeChecked();
    // Unavailable items are never selected by Select All.
    expect(screen.getByRole("checkbox", { name: /Track Two/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Track Four/ })).not.toBeChecked();
  });

  it("never selects beyond the backend cap when more selectable items exist", async () => {
    const fetchMock = collectionFlowHandler({
      analysis: () => Promise.resolve(jsonResponse(collectionAnalysis(largeCollection))),
    });
    const { user } = await renderNewDownload();
    await fillAndSubmit(user, ALBUM_URL);
    await screen.findByText(/^Large Track 0$/);

    // Default selection is the first 200 selectable items in source order.
    expect(screen.getByRole("heading", { name: /200 selected/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select All" })).toBeDisabled();
    const overCap = screen.getByRole<HTMLInputElement>("checkbox", {
      name: /Large Track 204/,
    });
    expect(overCap).not.toBeChecked();
    expect(overCap).toBeDisabled();
    // Bypassing the DOM disable must not exceed the cap (handler guard).
    act(() => {
      overCap.disabled = false;
      overCap.click();
    });
    expect(screen.getByRole("heading", { name: /200 selected/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Large Track 204/ })).not.toBeChecked();

    // Rebuilding via Deselect All + Select All keeps exactly the cap.
    await user.click(screen.getByRole("button", { name: "Deselect All" }));
    expect(screen.getByRole("heading", { name: /0 selected/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Select All" }));
    expect(screen.getByRole("heading", { name: /200 selected/ })).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "Audio" }));
    await user.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByText("Queued");
    const body = createBody(createRequests(fetchMock)[0]);
    expect(body.item_ids).toHaveLength(200);
    expect(body.item_ids).toEqual(
      Array.from({ length: 200 }, (_, index) => `item-${index}`),
    );
  });

  it("reports an unknown source total as a lower bound with explicit scope", async () => {
    collectionFlowHandler();
    const { user } = await renderNewDownload();
    await analyzeCollection(user);

    expect(screen.getByText(/At least 500 items in source/)).toBeInTheDocument();
    expect(screen.queryByText(/^500 items in source$/)).not.toBeInTheDocument();
    expect(
      screen.getByText(windowCollection.selection_limit_message),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/outside this window are not included/),
    ).toBeInTheDocument();
  });

  it("reports an exact source total when known and covered by the window", async () => {
    collectionFlowHandler({
      analysis: () =>
        Promise.resolve(jsonResponse(collectionAnalysis(knownCollection))),
    });
    const { user } = await renderNewDownload();
    await fillAndSubmit(user, ALBUM_URL);
    await screen.findByText("Track One");

    expect(screen.getByText(/^8 items in source$/)).toBeInTheDocument();
    expect(screen.queryByText(/At least/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/outside this window are not included/),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(knownCollection.selection_limit_message),
    ).toBeInTheDocument();
  });

  it("falls back to a read-only legacy preview without synthetic ids", async () => {
    const fetchMock = collectionFlowHandler({
      analysis: () =>
        Promise.resolve(jsonResponse(collectionAnalysis(legacyCollection))),
    });
    const { user } = await renderNewDownload();
    await fillAndSubmit(user, ALBUM_URL);
    await screen.findByText("Track One");

    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Select All" })).not.toBeInTheDocument();
    expect(screen.getByText(/predates selectable items/)).toBeInTheDocument();
    expect(screen.getByText("Only a preview is shown.")).toBeInTheDocument();

    const download = screen.getByRole<HTMLButtonElement>("button", {
      name: "Download",
    });
    expect(download).toBeDisabled();
    // Even a bypassed click cannot post a legacy preview as a selection.
    act(() => {
      download.disabled = false;
      download.click();
    });
    expect(createRequests(fetchMock)).toHaveLength(0);
    // The legacy preview is read-only: no mode and no output choice.
    expect(screen.queryByRole("group", { name: "Audio / Video" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Output format" })).not.toBeInTheDocument();
  });

  it("requires an explicit collection mode and shows no output choice until one is chosen", async () => {
    const fetchMock = collectionFlowHandler();
    const { user } = await renderNewDownload();
    await analyzeCollection(user);

    const audio = screen.getByRole("radio", { name: "Audio" });
    const video = screen.getByRole("radio", { name: "Video" });
    // No implicit default: the backend receives a mode only when chosen.
    expect(audio).not.toBeChecked();
    expect(video).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();

    expect(screen.getByText(/One output format applies to the whole collection/)).toBeInTheDocument();
    expect(screen.getByText(/never upgraded or upscaled/)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Output format" })).not.toBeInTheDocument();
    expect(screen.queryByText(/mp3/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/wav/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/flac/i)).not.toBeInTheDocument();

    await user.click(video);
    expect(video).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByText("Queued");
    expect(createBody(createRequests(fetchMock)[0]).collection_mode).toBe("video");
  });

  it("resets snapshot selection and mode when a new analysis arrives", async () => {
    let analysisCalls = 0;
    const fetchMock = collectionFlowHandler({
      analysis: () => {
        analysisCalls += 1;
        return Promise.resolve(
          jsonResponse(analysisCalls === 1 ? analysis : secondAnalysis),
        );
      },
    });
    const { user } = await renderNewDownload();
    await analyzeCollection(user);

    await user.click(screen.getByRole("checkbox", { name: /Track One/ }));
    await user.click(screen.getByRole("radio", { name: "Video" }));
    expect(screen.getByRole("radio", { name: "Video" })).toBeChecked();

    await user.clear(screen.getByLabelText("URL"));
    await user.type(screen.getByLabelText("URL"), SECOND_ALBUM_URL);
    await user.click(screen.getByRole("button", { name: "Analyze" }));
    await screen.findByText("New Track One");

    // The mode is never reused across snapshots.
    expect(screen.getByRole("radio", { name: "Audio" })).not.toBeChecked();
    expect(screen.getByRole("radio", { name: "Video" })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();
    // New snapshot defaults come from the new item ids only.
    expect(screen.getByRole("checkbox", { name: /New Track One/ })).toBeChecked();
    expect(screen.getAllByRole("checkbox")).toHaveLength(2);
    expect(screen.queryByRole("checkbox", { name: /^Track One/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "Audio" }));
    await user.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByText("Queued");
    const body = createBody(createRequests(fetchMock)[0]);
    expect(body.analysis_id).toBe(secondAnalysis.analysis_id);
    expect(body.item_ids).toEqual(["new-item-1", "new-item-2"]);
  });

  it("does not post a stale snapshot while a re-analysis is pending", async () => {
    const pending = deferred<Response>();
    let analysisCalls = 0;
    const fetchMock = collectionFlowHandler({
      analysis: () => {
        analysisCalls += 1;
        return analysisCalls === 1
          ? Promise.resolve(jsonResponse(analysis))
          : pending.promise;
      },
    });
    const { user } = await renderNewDownload();
    await analyzeCollection(user);
    await user.click(screen.getByRole("radio", { name: "Audio" }));

    await user.clear(screen.getByLabelText("URL"));
    await user.type(screen.getByLabelText("URL"), SECOND_ALBUM_URL);
    await user.click(screen.getByRole("button", { name: "Analyze" }));

    // The stale snapshot is not rendered while the new analysis is pending,
    // so there is nothing to post from and no selection to mutate.
    expect(screen.getByRole("button", { name: "Analyzing…" })).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "Download" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(createRequests(fetchMock)).toHaveLength(0);

    pending.resolve(jsonResponse(secondAnalysis));
    await screen.findByText("New Track One");
  });

  it("freezes the snapshot and posts exactly once while create is pending", async () => {
    const pending = deferred<Response>();
    const fetchMock = collectionFlowHandler({ create: () => pending.promise });
    const { user } = await renderNewDownload();
    await analyzeCollection(user);
    await user.click(screen.getByRole("radio", { name: "Audio" }));
    await user.click(screen.getByRole("button", { name: "Download" }));

    expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /Track One/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Video" })).toBeDisabled();
    // Bypassed clicks cannot produce a duplicate or mixed-snapshot post.
    act(() => {
      const starting = screen.getByRole<HTMLButtonElement>("button", {
        name: "Starting…",
      });
      starting.disabled = false;
      starting.click();
      starting.click();
    });
    expect(createRequests(fetchMock)).toHaveLength(1);

    await act(async () => {
      pending.resolve(jsonResponse(collectionJob));
    });
    expect(await screen.findByText("Queued")).toBeInTheDocument();
    expect(createRequests(fetchMock)).toHaveLength(1);
  });

  it("posts exactly once when Download is double-clicked before the pending state renders", async () => {
    const pending = deferred<Response>();
    const fetchMock = collectionFlowHandler({ create: () => pending.promise });
    const { user } = await renderNewDownload();
    await analyzeCollection(user);
    await user.click(screen.getByRole("radio", { name: "Audio" }));

    // Two synchronous clicks inside one act(): React Query has not yet
    // delivered the pending render, so only the create-flight ref can keep
    // the second click from posting a duplicate request.
    const download = screen.getByRole<HTMLButtonElement>("button", {
      name: "Download",
    });
    act(() => {
      download.click();
      download.click();
    });
    // React Query dispatches the mutation itself asynchronously; flush the
    // microtasks without any user interaction or pending-response wait so
    // the post count reflects the two synchronous clicks only.
    await act(async () => {});
    expect(createRequests(fetchMock)).toHaveLength(1);

    await screen.findByRole("button", { name: "Starting…" });
    await act(async () => {
      pending.resolve(jsonResponse(collectionJob));
    });
    expect(await screen.findByText("Queued")).toBeInTheDocument();
    expect(createRequests(fetchMock)).toHaveLength(1);
  });

  it("keeps the single-format create single-flight on a synchronous double click", async () => {
    const pending = deferred<Response>();
    const fetchMock = collectionFlowHandler({
      analysis: () => Promise.resolve(jsonResponse(singleAnalysis)),
      create: () => pending.promise,
    });
    const { user } = await renderNewDownload();
    await fillAndSubmit(user, "https://www.youtube.com/watch?v=abc123");
    await screen.findByText("Test Video");

    const download = screen.getByRole<HTMLButtonElement>("button", {
      name: "Download",
    });
    act(() => {
      download.click();
      download.click();
    });
    // Same microtask flush as the collection case: count posts from the two
    // synchronous clicks only, before any pending UI or response wait.
    await act(async () => {});
    expect(createRequests(fetchMock)).toHaveLength(1);
    expect(createBody(createRequests(fetchMock)[0])).toEqual({
      analysis_id: singleAnalysis.analysis_id,
      variant_id: "video-best",
      output_profile: "original",
      destination: "app_library",
    });

    await act(async () => {
      pending.resolve(jsonResponse(singleJob));
    });
    expect(await screen.findByText("Queued")).toBeInTheDocument();
    expect(createRequests(fetchMock)).toHaveLength(1);
  });

  it("preserves the selection and mode after a failed create, then retries the same body", async () => {
    let creates = 0;
    const fetchMock = collectionFlowHandler({
      create: () => {
        creates += 1;
        return creates === 1
          ? Promise.resolve(
              jsonResponse(
                {
                  error: {
                    code: "selection_expired",
                    message: "The analysis expired. Re-analyze and try again.",
                    retryable: false,
                  },
                },
                409,
              ),
            )
          : Promise.resolve(jsonResponse(collectionJob));
      },
    });
    const { user } = await renderNewDownload();
    await analyzeCollection(user);
    await user.click(screen.getByRole("checkbox", { name: /Track One/ }));
    await user.click(screen.getByRole("radio", { name: "Audio" }));
    await user.click(screen.getByRole("button", { name: "Download" }));

    await screen.findByText(/The analysis expired/);
    // The same snapshot's choices survive the failure.
    expect(screen.getByRole("checkbox", { name: /Track One/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Track Three/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Audio" })).toBeChecked();

    await user.click(screen.getByRole("button", { name: "Download" }));
    await screen.findByText("Queued");
    expect(creates).toBe(2);
    const bodies = createRequests(fetchMock).map((request) => createBody(request));
    expect(bodies[1]).toEqual(bodies[0]);
  });

  describe("output format", () => {
    const outputGroup = () => screen.getByRole("group", { name: "Output format" });
    const outputRadio = (name: string) =>
      within(outputGroup()).getByRole("radio", { name });
    const outputLabels = () =>
      within(outputGroup())
        .getAllByRole("radio")
        .map((radio) => radio.closest("label")?.textContent);

    it("offers the audio or video set per mode, defaulting to Original, with a compatibility-only note", async () => {
      collectionFlowHandler();
      const { user } = await renderNewDownload();
      await analyzeCollection(user);

      await user.click(screen.getByRole("radio", { name: "Audio" }));
      expect(outputLabels()).toEqual([
        "Original / Best Available",
        "MP3",
        "WAV",
        "M4A",
        "Opus",
        "FLAC",
      ]);
      expect(outputRadio("Original / Best Available")).toBeChecked();
      expect(within(outputGroup()).getByText(/not a quality upgrade/)).toBeInTheDocument();
      expect(within(outputGroup()).getByText(/WAV and FLAC do not restore detail/)).toBeInTheDocument();
      // Only one parent-level group; children have no output control.
      expect(screen.getAllByRole("group", { name: "Output format" })).toHaveLength(1);

      await user.click(screen.getByRole("radio", { name: "Video" }));
      expect(outputLabels()).toEqual(["Original", "MP4 (compatible)"]);
      expect(outputRadio("Original")).toBeChecked();
      // Source quality / no-upscale information stays alongside the choice.
      expect(screen.getByText(/never upgraded or upscaled/)).toBeInTheDocument();
    });

    it.each([
      ["audio", "Audio", "FLAC", "flac"],
      ["video", "Video", "MP4 (compatible)", "mp4"],
    ])("posts %s %s output with the unchanged ids, mode and collection profile", async (mode, modeLabel, label, profile) => {
      const fetchMock = collectionFlowHandler();
      const { user } = await renderNewDownload();
      await analyzeCollection(user);

      await user.click(screen.getByRole("checkbox", { name: /Track One/ }));
      await user.click(screen.getByRole("radio", { name: modeLabel }));
      await user.click(outputRadio(label));
      await user.click(screen.getByRole("button", { name: "Download" }));
      await screen.findByText("Queued");

      expect(createRequests(fetchMock)).toHaveLength(1);
      expect(createBody(createRequests(fetchMock)[0])).toEqual({
        analysis_id: analysis.analysis_id,
        item_ids: ["item-c"],
        collection_mode: mode,
        collection_profile: { kind: "original_best_available" },
        output_profile: profile,
        destination: "app_library",
      });
    });

    it("clears audio FLAC for good across audio -> video -> audio", async () => {
      const fetchMock = collectionFlowHandler();
      const { user } = await renderNewDownload();
      await analyzeCollection(user);

      await user.click(screen.getByRole("radio", { name: "Audio" }));
      await user.click(outputRadio("FLAC"));
      await user.click(screen.getByRole("radio", { name: "Video" }));
      expect(outputRadio("Original")).toBeChecked();
      await user.click(screen.getByRole("radio", { name: "Audio" }));
      expect(outputRadio("Original / Best Available")).toBeChecked();
      expect(outputRadio("FLAC")).not.toBeChecked();

      await user.click(screen.getByRole("button", { name: "Download" }));
      await screen.findByText("Queued");
      expect(createBody(createRequests(fetchMock)[0])).toMatchObject({
        collection_mode: "audio",
        output_profile: "original",
      });
    });

    it.each([
      ["a different", secondAnalysis],
      ["the same", analysis],
    ])("resets mode and output on a new analysis with %s analysis id", async (_label, next) => {
      let calls = 0;
      collectionFlowHandler({
        analysis: () =>
          Promise.resolve(jsonResponse(++calls === 1 ? analysis : next)),
      });
      const { user } = await renderNewDownload();
      await analyzeCollection(user);

      await user.click(screen.getByRole("radio", { name: "Audio" }));
      await user.click(outputRadio("FLAC"));
      await user.click(screen.getByRole("button", { name: "Analyze" }));
      // The stale Audio mode is still checked until the new analysis lands.
      await waitFor(() => expect(screen.getByRole("radio", { name: "Audio" })).not.toBeChecked());
      expect(screen.queryByRole("group", { name: "Output format" })).not.toBeInTheDocument();
      await user.click(screen.getByRole("radio", { name: "Audio" }));
      expect(outputRadio("Original / Best Available")).toBeChecked();
      expect(outputRadio("FLAC")).not.toBeChecked();
    });

    it("disables the output radios while create is pending", async () => {
      const pending = deferred<Response>();
      const fetchMock = collectionFlowHandler({ create: () => pending.promise });
      const { user } = await renderNewDownload();
      await analyzeCollection(user);
      await user.click(screen.getByRole("radio", { name: "Audio" }));
      await user.click(outputRadio("FLAC"));
      await user.click(screen.getByRole("button", { name: "Download" }));

      expect(screen.getByRole("button", { name: "Starting…" })).toBeDisabled();
      for (const radio of within(outputGroup()).getAllByRole("radio")) {
        expect(radio).toBeDisabled();
      }
      expect(createRequests(fetchMock)).toHaveLength(1);

      await act(async () => {
        pending.resolve(jsonResponse(collectionJob));
      });
      await screen.findByText("Queued");
      expect(createRequests(fetchMock)).toHaveLength(1);
    });
  });
});
