import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/app/App";
import type { Job, JobItem } from "../src/api/jobs";
import type { NormalizedAnalysis } from "../src/api/analysis";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
function deferred() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((res) => { resolve = res; });
  return { promise, resolve };
}
class Stream {
  static current: Stream;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, EventListener[]>();
  constructor() { Stream.current = this; }
  addEventListener(type: string, fn: EventListener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  emit(job: Job) {
    this.listeners.get("job.updated")?.forEach((fn) => fn(new MessageEvent("job.updated", { data: JSON.stringify(job) })));
  }
  close() {}
}
const progress = { percent: 0, downloaded_bytes: null, total_bytes: null, speed_bytes_per_second: null, eta_seconds: null };
function item(id: string, order: number, status: JobItem["status"]): JobItem {
  return {
    id, item_id: `selection-${id}`, source_order: order, source_id: "duplicate",
    source_url: `https://example.com/${id}`, title: `Result ${id}`, playlist_index: null,
    track_number: null, disc_number: null, status, progress: { ...progress, percent: 100 },
    final_path: status === "completed" ? `/library/${id}` : null,
    error: status === "failed" ? { code: "download_failed", message: `Reason ${id}`, retryable: true } : null,
  };
}
const base: Job = {
  id: "parent", analysis_id: "analysis", variant_id: "", job_type: "collection",
  title: "Tracked album", source_url: "https://example.com/album", destination: "app_library",
  status: "downloading", progress: { ...progress, percent: 100 / 3 }, final_path: null,
  error: null, total_items: 3, completed_items: 1, failed_items: 0,
  created_at: "2026-10-05T12:00:00Z", updated_at: "2026-10-05T12:00:01.000001Z",
  started_at: null, completed_at: null,
  items: [item("c", 9, "queued"), item("a", 0, "completed"), item("b", 4, "downloading")],
};
const analysis: NormalizedAnalysis = {
  analysis_id: "analysis", original_url: base.source_url, source_id: null,
  detected_platform: "soundcloud", detected_platform_label: "SoundCloud", extractor: null,
  title: "Preview album", uploader: null, duration_seconds: null, thumbnail_url: null,
  content_type: "collection", formats: [], platform_hint: null, platform_mismatch: false,
  collection: {
    title: "Preview album", total_items: 3, preview_items: [], truncated: false,
    selection_scope: "analysis_window", analysis_window_limit: 25, analyzed_items: 3,
    selectable_items: 3, selected_item_limit: 200, total_items_known: true,
    selection_truncated: false, selection_limit_message: "Selected window",
    items: [0, 4, 9].map((order) => ({ item_id: `selection-${order}`, source_order: order,
      selectable: true, unavailable_reason: null, source_id: "duplicate", url: `https://example.com/${order}`,
      title: `Preview ${order}`, duration_seconds: null, position: null })),
  },
};
function snapshot(overrides: Partial<Job> = {}): Job {
  return { ...base, updated_at: "2026-10-05T12:00:02.000002Z", items: null, ...overrides };
}
function failure(status: "failed" | "cancelled" = "failed", retryable = true): Job {
  return { ...base, status, failed_items: status === "failed" ? 2 : 0,
    error: { code: status === "failed" ? "partial_failure" : "cancelled", message: "Parent reason", retryable },
    items: [item("a", 0, "completed"), item("b", 4, status), item("c", 9, status)] };
}
interface Options {
  create?: () => Job;
  read?: () => Promise<Response>;
  retry?: () => Promise<Response>;
  cancel?: () => Promise<Response>;
}
function harness(options: Options = {}) {
  vi.stubGlobal("EventSource", Stream);
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/health") return Promise.resolve(json({ status: "ok", database: "ok" }));
    if (url === "/api/v1/analysis") return Promise.resolve(json(analysis));
    if (url === "/api/v1/jobs" && init?.method === "POST") return Promise.resolve(json(options.create?.() ?? base));
    if (url.endsWith("/activity")) return Promise.resolve(json({ events: [] }));
    if (url.endsWith("/retry")) return options.retry?.() ?? Promise.reject(new Error("unexpected retry"));
    if (url.endsWith("/cancel")) return options.cancel?.() ?? Promise.reject(new Error("unexpected cancel"));
    if (url.startsWith("/api/v1/jobs/")) return options.read?.() ?? Promise.resolve(json(base));
    return Promise.reject(new Error(`Unexpected fetch ${url}`));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}
async function start() {
  const view = render(<App />);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("URL"), base.source_url);
  await user.click(screen.getByRole("button", { name: "Analyze" }));
  await screen.findByText("Preview 0");
  await user.click(screen.getByRole("radio", { name: "Audio" }));
  await user.click(screen.getByRole("button", { name: "Download" }));
  await screen.findByRole("heading", { name: "Tracked album" });
  return { ...view, user };
}
const region = () => screen.getByRole("region", { name: "Collection results" });
const reads = (fetch: ReturnType<typeof harness>) => fetch.mock.calls.filter(([url]) => String(url) === "/api/v1/jobs/parent");
const posts = (fetch: ReturnType<typeof harness>, action: string) => fetch.mock.calls.filter(([url]) => String(url).endsWith(`/${action}`));

describe("collection progress and parent controls", () => {
  it("uses committed parent progress, real counters and raw child order, not child transfer completion", async () => {
    const errors = vi.spyOn(console, "error");
    harness();
    await start();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", String(100 / 3));
    expect(screen.getByText("33.3%")).toBeInTheDocument();
    expect(within(region()).getByText("Completed: 1 / Failed: 0 / Total: 3")).toBeInTheDocument();
    const rows = within(region()).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Source order: 0 — Result a"),
      expect.stringContaining("Source order: 4 — Result b"),
      expect.stringContaining("Source order: 9 — Result c"),
    ]);
    expect(rows[1]).toHaveTextContent("downloading — 100%");
    expect(within(region()).queryByRole("button")).not.toBeInTheDocument();
    expect(within(region()).queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Cancel" })).toHaveLength(1);
    expect(screen.queryByText(/ETA|\/library\//)).not.toBeInTheDocument();
    expect(errors.mock.calls.flat().join(" ")).not.toMatch(/same key|unique.*key/);
  });

  it.each(["mixed", "all failed", "almost complete"])("keeps truthful terminal %s counts and errors", async (kind) => {
    const count = kind === "all failed" ? 0 : kind === "mixed" ? 2 : 1999;
    const total = kind === "almost complete" ? 2000 : 3;
    harness({ create: () => ({ ...failure(), completed_items: count, total_items: total,
      failed_items: total - count, progress: { ...progress, percent: 100 * count / total },
      items: kind === "all failed" ? [item("a", 0, "failed"), item("b", 4, "failed"), item("c", 9, "failed")] : failure().items,
      error: { code: kind === "all failed" ? "all_children_failed" : "partial_failure", message: "Parent reason", retryable: true } }) });
    await start();
    expect(within(region()).getByText(`Completed: ${count} / Failed: ${total - count} / Total: ${total}`)).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", String(100 * count / total));
    expect(screen.queryByText("100%")).not.toBeInTheDocument();
    expect(screen.getByText("Parent reason")).toBeInTheDocument();
    expect(within(region()).getByText("download_failed: Reason b")).toBeInTheDocument();
    expect(within(region()).getAllByRole("listitem")[0]).toHaveTextContent(kind === "all failed" ? "failed" : "completed");
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it.each(["cancelled eligible", "failed ineligible", "unknown error", "no error", "single cancelled"])("uses backend eligibility for %s", async (kind) => {
    const created = failure(kind.includes("cancelled") ? "cancelled" : "failed", kind !== "failed ineligible" && kind !== "unknown error");
    if (kind === "unknown error") created.error = { code: "unknown", message: "Unknown", retryable: false };
    if (kind === "no error") created.error = null;
    if (kind === "single cancelled") created.job_type = "single";
    harness({ create: () => created });
    await start();
    expect(screen.queryAllByRole("button", { name: "Retry" })).toHaveLength(kind === "cancelled eligible" ? 1 : 0);
  });

  it("enriches equal-time parent SSE without regressing it and fetches final children after polling stops", async () => {
    const first = deferred();
    const final = deferred();
    let count = 0;
    const fetch = harness({ read: () => ++count === 1 ? first.promise : final.promise });
    await start();
    const live = snapshot({ completed_items: 2, progress: { ...progress, percent: 200 / 3 } });
    act(() => Stream.current.emit(live));
    expect(within(region()).getByText(/last-known details/)).toBeInTheDocument();
    await act(async () => first.resolve(json({ ...live, title: "Equal-time title must not replace parent", items: [item("a", 0, "completed"), item("b", 4, "completed")] })));
    expect(screen.getByRole("heading", { name: "Tracked album" })).toBeInTheDocument();
    expect(within(region()).getByRole("list", { name: "Current item details" })).toBeInTheDocument();
    const terminal = snapshot({ status: "failed", completed_items: 2, failed_items: 1,
      progress: { ...progress, percent: 200 / 3 }, updated_at: "2026-10-05T12:00:03Z", error: failure().error });
    act(() => Stream.current.emit(terminal));
    expect(reads(fetch)).toHaveLength(2);
    await act(async () => final.resolve(json({ ...terminal, items: [item("a", 0, "completed"), item("b", 4, "completed"), item("c", 9, "failed")] })));
    expect(within(region()).getByText("download_failed: Reason c")).toBeInTheDocument();
    expect(within(region()).queryByText(/not current/)).not.toBeInTheDocument();
  });

  it("coalesces SSE bursts and rejects old REST parent and child facts", async () => {
    const old = deferred();
    const latest = deferred();
    let count = 0;
    const fetch = harness({ read: () => ++count === 1 ? old.promise : latest.promise });
    await start();
    act(() => {
      Stream.current.emit(snapshot());
      Stream.current.emit(snapshot({ updated_at: "2026-10-05T12:00:03Z", completed_items: 2 }));
      Stream.current.emit(snapshot({ updated_at: "2026-10-05T12:00:04Z", completed_items: 2, status: "failed", error: failure().error }));
    });
    expect(reads(fetch)).toHaveLength(1);
    await act(async () => old.resolve(json({ ...snapshot(), items: [item("old", 0, "failed")] })));
    expect(reads(fetch)).toHaveLength(2);
    expect(within(region()).queryByText(/Result old/)).not.toBeInTheDocument();
    const current = snapshot({ updated_at: "2026-10-05T12:00:04Z", completed_items: 2, status: "failed", error: failure().error });
    await act(async () => latest.resolve(json({ ...current, items: [item("a", 0, "completed")] })));
    expect(within(region()).getByRole("list", { name: "Current item details" })).toBeInTheDocument();
    expect(reads(fetch)).toHaveLength(2);
  });

  it("preserves Retry counters and new SSE attempt before queued POST, excluding old GET/SSE", async () => {
    const old = deferred();
    const fresh = deferred();
    const retry = deferred();
    let count = 0;
    const fetch = harness({ create: () => failure("cancelled"), read: () => ++count === 1 ? old.promise : fresh.promise, retry: () => retry.promise });
    await start();
    act(() => Stream.current.onopen?.());
    const button = screen.getByRole("button", { name: "Retry" });
    act(() => { button.click(); button.click(); });
    expect(posts(fetch, "retry")).toHaveLength(1);
    expect(within(region()).getByText("Completed: 1 / Failed: 0 / Total: 3")).toBeInTheDocument();
    expect(within(region()).queryByText(/Result b/)).not.toBeInTheDocument();
    const live = snapshot({ completed_items: 2, progress: { ...progress, percent: 200 / 3 } });
    act(() => Stream.current.emit(live));
    await act(async () => old.resolve(json({ ...failure(), updated_at: "2026-10-05T12:00:09Z", items: [item("old", 0, "failed")] })));
    act(() => Stream.current.emit(failure()));
    await act(async () => retry.resolve(json(snapshot({ status: "queued", completed_items: 1 }))));
    expect(within(region()).getByText("Completed: 2 / Failed: 0 / Total: 3")).toBeInTheDocument();
    expect(screen.getByText("66.7%")).toBeInTheDocument();
    await act(async () => fresh.resolve(json({ ...live, items: [item("a", 0, "completed"), item("b", 4, "completed")] })));
    expect(within(region()).queryByText(/Result old/)).not.toBeInTheDocument();
    expect(within(region()).getAllByRole("listitem")).toHaveLength(2);
  });

  it("marks failed detail reads unavailable without automatic reads or mutations", async () => {
    const fetch = harness({ read: () => Promise.reject(new Error("offline")) });
    await start();
    act(() => Stream.current.emit(snapshot({ status: "failed", error: failure().error })));
    await screen.findByText(/Current item details unavailable/);
    expect(within(region()).getByText(/not current/)).toBeInTheDocument();
    expect(reads(fetch)).toHaveLength(1);
    expect(posts(fetch, "retry")).toHaveLength(0);
    expect(posts(fetch, "cancel")).toHaveLength(0);
  });

  it("reconciles a lost Retry response with backend facts, never repeating POST", async () => {
    const live = snapshot({ status: "queued", completed_items: 1, failed_items: 0 });
    const fetch = harness({ create: () => failure(), retry: () => Promise.reject(new Error("lost")),
      read: () => Promise.resolve(json({ ...live, items: [item("a", 0, "completed"), item("b", 4, "queued")] })) });
    const { user } = await start();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(within(region()).getAllByRole("listitem")[1]).toHaveTextContent("queued"));
    expect(screen.queryByText(/Could not retry/)).not.toBeInTheDocument();
    expect(posts(fetch, "retry")).toHaveLength(1);
    expect(within(region()).getByText("Completed: 1 / Failed: 0 / Total: 3")).toBeInTheDocument();
  });

  it.each(["switch", "unmount"])("ignores old child GET and cancel replies after %s", async (action) => {
    const old = deferred();
    const cancel = deferred();
    let creates = 0;
    const fetch = harness({ create: () => ++creates === 1 ? base : { ...base, id: "next" }, read: () => old.promise, cancel: () => cancel.promise });
    const { user, unmount } = await start();
    const stream = Stream.current;
    act(() => stream.emit(snapshot()));
    const button = screen.getByRole("button", { name: "Cancel" });
    act(() => { button.click(); button.click(); });
    expect(posts(fetch, "cancel")).toHaveLength(1);
    if (action === "unmount") unmount();
    else await user.click(screen.getByRole("button", { name: "Download" }));
    await act(async () => {
      old.resolve(json(snapshot({ updated_at: "2026-10-05T12:00:09Z", items: [item("old", 0, "failed")] })));
      cancel.resolve(json(snapshot({ status: "cancelled", updated_at: "2026-10-05T12:00:09Z" })));
      stream.emit(snapshot({ status: "failed", updated_at: "2026-10-05T12:00:10Z" }));
    });
    if (action === "switch") {
      expect(within(region()).queryByText(/Result old/)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    }
    expect(posts(fetch, "cancel")).toHaveLength(1);
  });
});
