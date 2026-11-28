import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ActivityEvent } from "../src/api/activity";
import type { Job, JobItem, ScopedJobList } from "../src/api/jobs";
import { QueueController, type QueueView } from "../src/features/queue/useQueue";
import { QueuePage } from "../src/pages/QueuePage";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
const notFound = () => json({ error: { code: "not_found", message: "Job not found.", retryable: false } }, 404);
const serverError = () => json({ error: { code: "internal", message: "Server exploded.", retryable: false } }, 500);

function deferred() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((res) => { resolve = res; });
  return { promise, resolve };
}

const progress = { percent: 0, downloaded_bytes: null, total_bytes: null, speed_bytes_per_second: null, eta_seconds: null };
const at = (second: number) => `2026-11-20T10:00:${String(second).padStart(2, "0")}.000000+00:00`;
const retryable = { code: "download_failed", message: "Download failed.", retryable: true };

function job(id: string, overrides: Partial<Job> = {}): Job {
  return {
    id, analysis_id: "a", variant_id: "v", status: "queued", title: `Job ${id}`,
    source_url: "https://example.com", destination: "app_library", progress,
    final_path: null, error: null, created_at: at(0), updated_at: at(1),
    started_at: null, completed_at: null, ...overrides,
  };
}

function childItem(id: string): JobItem {
  return {
    id, item_id: `sel-${id}`, source_order: 0, source_id: null, source_url: "https://example.com",
    title: `Child ${id}`, playlist_index: null, track_number: null, disc_number: null,
    status: "queued", progress, final_path: null, error: null,
  };
}

function activity(id: number, jobId: string, message: string): ActivityEvent {
  return { id, job_id: jobId, level: "info", event_type: "job", message, created_at: "2026-11-20T10:00:00Z" };
}

let fetchCallsAtSubscribe = -1;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  readonly url: string;
  private readonly listeners = new Map<string, EventListener[]>();

  constructor(url: string | URL) {
    this.url = String(url);
    fetchCallsAtSubscribe = server.fetch.mock.calls.length;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: EventListener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  emit(type: string, data: unknown) {
    act(() => {
      for (const listener of this.listeners.get(type) ?? []) {
        listener(new MessageEvent(type, { data: JSON.stringify(data) }));
      }
    });
  }
  open() { act(() => this.onopen?.()); }
  fail() { act(() => this.onerror?.()); }
  close() { this.closed = true; }
}

interface Once { match: (url: string, method: string) => boolean; respond: () => Promise<Response> }

const server = {
  active: [] as Job[],
  pages: new Map<string, ScopedJobList>(),
  jobs: new Map<string, Job>(),
  activity: new Map<string, ActivityEvent[]>(),
  actions: new Map<string, () => Promise<Response>>(),
  once: [] as Once[],
  fetch: vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(),
};

function route(url: string, method: string): Promise<Response> {
  const index = server.once.findIndex((entry) => entry.match(url, method));
  if (index >= 0) return server.once.splice(index, 1)[0].respond();
  if (method === "POST") {
    const action = server.actions.get(url);
    return action ? action() : Promise.reject(new Error(`Unexpected POST ${url}`));
  }
  if (url === "/api/v1/jobs?scope=active") return Promise.resolve(json({ jobs: server.active, next_cursor: null }));
  if (url.startsWith("/api/v1/jobs?scope=history")) {
    const cursor = new URL(url, "http://localhost").searchParams.get("cursor") ?? "";
    const page = server.pages.get(cursor);
    return Promise.resolve(page ? json(page) : json({ error: { code: "invalid_request" } }, 422));
  }
  const activityMatch = url.match(/^\/api\/v1\/jobs\/([^/?]+)\/activity$/);
  if (activityMatch) {
    const events = server.activity.get(activityMatch[1]);
    return Promise.resolve(events ? json({ events }) : notFound());
  }
  const jobMatch = url.match(/^\/api\/v1\/jobs\/([^/?]+)$/);
  if (jobMatch) {
    const found = server.jobs.get(jobMatch[1]);
    return Promise.resolve(found ? json(found) : notFound());
  }
  return Promise.reject(new Error(`Unexpected fetch ${url}`));
}

function onceFor(match: (url: string, method: string) => boolean) {
  const pending = deferred();
  server.once.push({ match, respond: () => pending.promise });
  return pending;
}

const urls = () => server.fetch.mock.calls.map(([input]) => String(input));
const count = (predicate: (url: string) => boolean) => urls().filter(predicate).length;
const activeCalls = () => count((url) => url === "/api/v1/jobs?scope=active");
const headCalls = () => count((url) => url === "/api/v1/jobs?scope=history");
const isHistoryCursor = (cursor: string) => (url: string) =>
  url.startsWith("/api/v1/jobs?scope=history") && new URL(url, "http://localhost").searchParams.get("cursor") === cursor;

async function settle() {
  await act(async () => {
    for (let index = 0; index < 8; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function tick(ms = 2000) {
  act(() => { vi.advanceTimersByTime(ms); });
  await settle();
}

async function mount({ open = true } = {}) {
  const view = render(<QueuePage />);
  await settle();
  const stream = FakeEventSource.instances[0];
  if (open) {
    stream.open();
    await settle();
  }
  return { ...view, stream, user: userEvent.setup() };
}

const activeRegion = () => within(screen.getByRole("region", { name: "Active and queued" }));
const historyRegion = () => within(screen.getByRole("region", { name: "History" }));
const rowItem = (title: string) => screen.getByRole("listitem", { name: title });
// Scoped to the row summary/action area so an inline detail panel inside the
// same list item never makes role queries (Retry, alert, status text) ambiguous.
const row = (title: string) => {
  const summary = rowItem(title).querySelector<HTMLElement>(":scope > .queue-row-summary");
  if (!summary) throw new Error(`No summary for ${title}`);
  return within(summary);
};
// Only queue rows: nested Activity/collection list items are not rows.
const rowTitles = (region: ReturnType<typeof within>) =>
  region.queryAllByRole("listitem")
    .filter((item: HTMLElement) => item.classList.contains("queue-row"))
    .map((item: HTMLElement) => item.getAttribute("aria-label"));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  FakeEventSource.instances = [];
  fetchCallsAtSubscribe = -1;
  server.active = [];
  server.pages = new Map([["", { jobs: [], next_cursor: null }]]);
  server.jobs = new Map();
  server.activity = new Map();
  server.actions = new Map();
  server.once = [];
  server.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => route(String(input), init?.method ?? "GET"));
  vi.stubGlobal("fetch", server.fetch);
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("QueuePage lists", () => {
  it("subscribes once before any REST and shows server order without per-row requests", async () => {
    server.active = [
      job("b", { created_at: at(5), status: "downloading", progress: { ...progress, percent: 42.4 } }),
      job("a", { created_at: at(5) }),
    ];
    server.pages.set("", { jobs: [job("h", { status: "completed" })], next_cursor: null });
    await mount();

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe("/api/v1/events");
    expect(fetchCallsAtSubscribe).toBe(0);
    expect(rowTitles(activeRegion())).toEqual(["Job a", "Job b"]);
    expect(row("Job b").getByText("42%")).toBeInTheDocument();
    expect(rowTitles(historyRegion())).toEqual(["Job h"]);
    expect(count((url) => url.includes("/activity") || /\/api\/v1\/jobs\/[^?]/.test(url))).toBe(0);
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("applies known SSE snapshots newest-wins and moves rows between disjoint lists", async () => {
    server.active = [job("a", { status: "downloading", updated_at: at(10), progress: { ...progress, percent: 10 } })];
    const { stream } = await mount();
    const snapshot = (second: number, percent: number, extra: Partial<Job> = {}) =>
      job("a", { status: "downloading", updated_at: at(second), progress: { ...progress, percent }, ...extra });

    stream.emit("job.updated", snapshot(9, 50));
    stream.emit("job.updated", snapshot(10, 60));
    expect(row("Job a").getByText("10%")).toBeInTheDocument();
    stream.emit("job.updated", snapshot(11, 70));
    expect(row("Job a").getByText("70%")).toBeInTheDocument();

    stream.emit("job.failed", snapshot(12, 70, { status: "failed", error: retryable }));
    expect(rowTitles(activeRegion())).toEqual([]);
    expect(rowTitles(historyRegion())).toEqual(["Job a"]);
    expect(screen.getAllByRole("listitem", { name: "Job a" })).toHaveLength(1);
    stream.emit("job.updated", snapshot(13, 0, { status: "queued" }));
    expect(rowTitles(activeRegion())).toEqual(["Job a"]);
    expect(rowTitles(historyRegion())).toEqual([]);
  });

  it("discards an older-generation reconcile history while passing only known active rows", async () => {
    const firstHead = onceFor((url) => url === "/api/v1/jobs?scope=history");
    server.active = [job("a")];
    const { stream } = await mount({ open: false });
    stream.open();
    firstHead.resolve(json({ jobs: [job("stale", { status: "failed", error: retryable })], next_cursor: null }));
    server.pages.set("", { jobs: [job("h", { status: "completed" })], next_cursor: null });
    await settle();

    expect(activeCalls()).toBe(2);
    expect(headCalls()).toBe(2);
    expect(rowTitles(activeRegion())).toEqual(["Job a"]);
    expect(rowTitles(historyRegion())).toEqual(["Job h"]);
    expect(screen.queryByText("Job stale")).not.toBeInTheDocument();
  });
});

describe("QueuePage reconcile", () => {
  it("never inserts unknown SSE jobs/activity and coalesces them into one merge plus one trailing", async () => {
    const { stream } = await mount();
    const before = activeCalls();
    const inFlight = onceFor((url) => url === "/api/v1/jobs?scope=active");

    stream.emit("job.updated", job("u"));
    stream.emit("job.updated", job("u", { updated_at: at(2) }));
    stream.emit("activity.created", activity(1, "v", "Started"));
    expect(screen.queryByText("Job u")).not.toBeInTheDocument();
    expect(activeCalls()).toBe(before + 1);

    server.active = [job("u", { updated_at: at(2) })];
    inFlight.resolve(json({ jobs: [], next_cursor: null }));
    await settle();
    expect(activeCalls()).toBe(before + 2);
    expect(rowTitles(activeRegion())).toEqual(["Job u"]);
    await settle();
    expect(activeCalls()).toBe(before + 2);
  });

  it("polls every 2000 ms while SSE is closed even with an empty list; open SSE polls only with active rows", async () => {
    const { stream } = await mount({ open: false });
    expect(screen.getByText("No active or queued downloads.")).toBeInTheDocument();
    server.active = [job("n")];
    await tick();
    expect(rowTitles(activeRegion())).toEqual(["Job n"]);

    stream.open();
    await settle();
    const opened = activeCalls();
    await tick();
    expect(activeCalls()).toBe(opened + 1);

    server.active = [];
    server.pages.set("", { jobs: [job("n", { status: "completed", updated_at: at(9) })], next_cursor: null });
    await tick();
    expect(rowTitles(historyRegion())).toEqual(["Job n"]);
    const idle = activeCalls();
    await tick(6000);
    expect(activeCalls()).toBe(idle);
  });

  it("merges once on SSE error and re-reads missing actives on reconnect (404 removes)", async () => {
    server.active = [job("a", { status: "downloading" }), job("b", { created_at: at(1) })];
    const { stream } = await mount();
    const before = activeCalls();
    stream.fail();
    stream.fail();
    await settle();
    expect(activeCalls()).toBe(before + 1);

    server.active = [];
    server.pages.set("", { jobs: [job("h", { status: "completed", created_at: at(9) })], next_cursor: "c2" });
    server.jobs.set("a", job("a", { status: "completed", updated_at: at(20) }));
    stream.open();
    await settle();
    expect(count((url) => url === "/api/v1/jobs/a")).toBe(1);
    expect(count((url) => url === "/api/v1/jobs/b")).toBe(1);
    // A terminal re-read outside the reset head leaves both lists.
    expect(rowTitles(activeRegion())).toEqual([]);
    expect(rowTitles(historyRegion())).toEqual(["Job h"]);
    expect(screen.queryByText("Job a")).not.toBeInTheDocument();
    expect(screen.queryByText("Job b")).not.toBeInTheDocument();
    server.pages.set("c2", { jobs: [], next_cursor: null });
    await userEvent.setup().click(screen.getByRole("button", { name: "Load more" }));
    await settle();
    expect(urls().at(-1)).toBe("/api/v1/jobs?scope=history&cursor=c2");
  });

  it("does not let a delayed reset-related missing GET change a newer reset head", async () => {
    server.active = [job("a", { status: "downloading" })];
    const { stream } = await mount();
    server.active = [];
    server.pages.set("", { jobs: [job("h1", { status: "completed", created_at: at(9) })], next_cursor: "c1" });
    const held = onceFor((url) => url === "/api/v1/jobs/a");
    stream.open();
    await settle();
    expect(count((url) => url === "/api/v1/jobs/a")).toBe(1);

    server.pages.set("", { jobs: [job("h2", { status: "completed", created_at: at(10) })], next_cursor: "c9" });
    stream.open();
    await settle();
    held.resolve(json(job("a", { status: "failed", error: retryable, updated_at: at(30) })));
    await settle();
    expect(rowTitles(historyRegion())).toEqual(["Job h2"]);
    expect(rowTitles(activeRegion())).toEqual([]);
    expect(screen.queryByText("Job a")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
  });

  it("drops a newer local terminal outside the head on reset while the open detail keeps it", async () => {
    server.active = [job("a", { status: "downloading", updated_at: at(5) })];
    server.jobs.set("a", job("a", { status: "downloading", updated_at: at(5) }));
    server.activity.set("a", []);
    const { stream, user } = await mount();
    await user.click(row("Job a").getByRole("button", { name: "Details" }));
    await settle();
    stream.emit("job.failed", job("a", { status: "failed", error: retryable, updated_at: at(8) }));
    expect(rowTitles(historyRegion())).toEqual(["Job a"]);

    // The fresh active list still serves the older active snapshot.
    server.pages.set("", { jobs: [job("h", { status: "completed", created_at: at(9) })], next_cursor: "c2" });
    stream.open();
    await settle();
    expect(rowTitles(activeRegion())).toEqual([]);
    expect(rowTitles(historyRegion())).toEqual(["Job h"]);
    expect(screen.queryByRole("listitem", { name: "Job a" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
    const detail = within(screen.getByRole("region", { name: "Job details" }));
    expect(detail.getByText("Failed")).toBeInTheDocument();
  });

  it("keeps a current-generation merge re-read moving a terminal row into history", async () => {
    server.active = [job("a", { status: "downloading" })];
    await mount({ open: false });
    server.active = [];
    server.jobs.set("a", job("a", { status: "completed", updated_at: at(20) }));
    await tick();
    expect(count((url) => url === "/api/v1/jobs/a")).toBe(1);
    expect(rowTitles(activeRegion())).toEqual([]);
    expect(rowTitles(historyRegion())).toEqual(["Job a"]);
  });

  it("merges immediately on an error before the first open, once per outage", async () => {
    const { stream } = await mount({ open: false });
    const before = activeCalls();
    stream.fail();
    await settle();
    expect(activeCalls()).toBe(before + 1);
    stream.fail();
    stream.fail();
    await settle();
    expect(activeCalls()).toBe(before + 1);

    stream.open();
    await settle();
    const opened = activeCalls();
    stream.fail();
    await settle();
    expect(activeCalls()).toBe(opened + 1);
    stream.fail();
    await settle();
    expect(activeCalls()).toBe(opened + 1);
  });

  it("drops a stale failed second-page row retried to completed elsewhere after the onOpen reset", async () => {
    server.pages.set("", { jobs: [job("h1", { status: "completed", created_at: at(9) })], next_cursor: "c2" });
    server.pages.set("c2", { jobs: [job("f", { status: "failed", error: retryable, created_at: at(3) })], next_cursor: null });
    const { stream, user } = await mount();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await settle();
    expect(row("Job f").getByRole("button", { name: "Retry" })).toBeInTheDocument();

    stream.fail();
    await settle();
    server.pages.set("c2", { jobs: [job("f", { status: "completed", created_at: at(3), updated_at: at(30) })], next_cursor: null });
    stream.open();
    await settle();
    expect(rowTitles(historyRegion())).toEqual(["Job h1"]);
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load more" }));
    await settle();
    expect(urls().at(-1)).toBe("/api/v1/jobs?scope=history&cursor=c2");
    expect(rowTitles(historyRegion())).toEqual(["Job h1", "Job f"]);
    expect(row("Job f").getByText("Completed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("discards a delayed Load more response after a reset without changing rows or cursor", async () => {
    server.pages.set("", { jobs: [job("h1", { status: "completed" })], next_cursor: "c2" });
    const { stream, user } = await mount();
    const delayed = onceFor((url) => isHistoryCursor("c2")(url));
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(screen.getByRole("button", { name: "Loading…" })).toBeDisabled();

    stream.open();
    await settle();
    delayed.resolve(json({ jobs: [job("old", { status: "failed" })], next_cursor: "c3" }));
    await settle();
    expect(screen.queryByText("Job old")).not.toBeInTheDocument();

    server.pages.set("c2", { jobs: [], next_cursor: null });
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await settle();
    expect(urls().at(-1)).toBe("/api/v1/jobs?scope=history&cursor=c2");
    expect(count(isHistoryCursor("c3"))).toBe(0);
  });

  it("poll merge keeps loaded pages and cursor; a head gap resets to the fresh head", async () => {
    server.pages.set("", { jobs: [job("h1", { status: "completed", created_at: at(9) })], next_cursor: "c2" });
    server.pages.set("c2", { jobs: [job("h2", { status: "completed", created_at: at(8) })], next_cursor: null });
    const { user } = await mount({ open: false });
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await settle();
    expect(rowTitles(historyRegion())).toEqual(["Job h1", "Job h2"]);

    server.pages.set("", {
      jobs: [job("n1", { status: "completed", created_at: at(10) }), job("h1", { status: "completed", created_at: at(9) })],
      next_cursor: "cx",
    });
    await tick();
    expect(rowTitles(historyRegion())).toEqual(["Job n1", "Job h1", "Job h2"]);
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();

    server.pages.set("", {
      jobs: [job("n3", { status: "completed", created_at: at(12) }), job("n2", { status: "completed", created_at: at(11) })],
      next_cursor: "cy",
    });
    server.pages.set("cy", { jobs: [], next_cursor: null });
    await tick();
    expect(rowTitles(historyRegion())).toEqual(["Job n3", "Job n2"]);
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await settle();
    expect(urls().at(-1)).toBe("/api/v1/jobs?scope=history&cursor=cy");
  });

  it("keeps the cursor on a Load more error for a single-flight manual retry", async () => {
    server.pages.set("", { jobs: [job("h1", { status: "completed", created_at: at(9) })], next_cursor: "c2" });
    server.pages.set("c2", { jobs: [job("h2", { status: "completed", created_at: at(8) })], next_cursor: null });
    const { user } = await mount();
    const failing = onceFor((url) => isHistoryCursor("c2")(url));
    const button = screen.getByRole("button", { name: "Load more" });
    act(() => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(count(isHistoryCursor("c2"))).toBe(1);
    failing.resolve(serverError());
    await settle();
    expect(screen.getByText("Could not load more history.")).toBeInTheDocument();
    expect(count(isHistoryCursor("c2"))).toBe(1);

    await user.click(screen.getByRole("button", { name: "Load more" }));
    await settle();
    expect(rowTitles(historyRegion())).toEqual(["Job h1", "Job h2"]);
  });

  it("cleans up the subscription and timers on unmount", async () => {
    const { unmount, stream } = await mount({ open: false });
    const calls = server.fetch.mock.calls.length;
    unmount();
    expect(stream.closed).toBe(true);
    await tick(6000);
    expect(server.fetch.mock.calls.length).toBe(calls);
  });
});

describe("QueuePage detail", () => {
  it("reads a past job and its Activity on demand, dedups SSE activity and re-reads on reset", async () => {
    const past = job("c", { status: "completed", created_at: at(3), final_path: "/library/c.opus" });
    server.pages.set("", { jobs: [job("h1", { status: "completed", created_at: at(9) })], next_cursor: "c2" });
    server.pages.set("c2", { jobs: [past], next_cursor: null });
    server.jobs.set("c", past);
    server.activity.set("c", [activity(1, "c", "Queued"), activity(2, "c", "Saved")]);
    const { stream, user } = await mount();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await settle();

    await user.click(row("Job c").getByRole("button", { name: "Details" }));
    await settle();
    const detail = () => within(screen.getByRole("region", { name: "Job details" }));
    expect(detail().getByRole("heading", { name: "Job c" })).toBeInTheDocument();
    expect(count((url) => url === "/api/v1/jobs/c")).toBe(1);
    expect(count((url) => url === "/api/v1/jobs/c/activity")).toBe(1);
    expect(count((url) => url === "/api/v1/jobs/h1/activity")).toBe(0);
    await user.click(detail().getByRole("button", { name: /Activity/ }));
    stream.emit("activity.created", activity(2, "c", "Saved"));
    stream.emit("activity.created", activity(3, "c", "Done"));
    stream.emit("activity.created", activity(4, "h1", "Other job"));
    expect(within(detail().getByRole("list")).getAllByRole("listitem").map((item) => item.textContent))
      .toEqual([expect.stringContaining("Queued"), expect.stringContaining("Saved"), expect.stringContaining("Done")]);

    // The reset drops page 2, but the open detail is keyed by ID and re-read.
    stream.open();
    await settle();
    expect(screen.queryByRole("listitem", { name: "Job c" })).not.toBeInTheDocument();
    expect(detail().getByRole("heading", { name: "Job c" })).toBeInTheDocument();
    expect(count((url) => url === "/api/v1/jobs/c")).toBe(2);
    expect(count((url) => url === "/api/v1/jobs/c/activity")).toBe(2);

    server.jobs.delete("c");
    stream.open();
    await settle();
    expect(screen.queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
  });

  // Previously "after the selection changes": with several History details
  // open, opening y no longer closes x, so the stale case is x's own close.
  it("ignores a stale detail response after its detail closed, leaving another open detail alone", async () => {
    server.pages.set("", {
      jobs: [job("x", { status: "completed", created_at: at(5) }), job("y", { status: "completed", created_at: at(4) })],
      next_cursor: null,
    });
    server.jobs.set("y", job("y", { status: "completed", created_at: at(4) }));
    server.activity.set("y", []);
    const slowJob = onceFor((url) => url === "/api/v1/jobs/x");
    const slowActivity = onceFor((url) => url === "/api/v1/jobs/x/activity");
    const { user } = await mount();
    await user.click(row("Job x").getByRole("button", { name: "Details" }));
    await user.click(row("Job y").getByRole("button", { name: "Details" }));
    await user.click(row("Job x").getByRole("button", { name: "Hide details" }));
    slowJob.resolve(json(job("x", { status: "completed", title: "Late x", updated_at: at(40) })));
    slowActivity.resolve(json({ events: [activity(9, "x", "Late")] }));
    await settle();
    expect(screen.getAllByRole("region", { name: "Job details" })).toHaveLength(1);
    const detail = within(screen.getByRole("region", { name: "Job details" }));
    expect(detail.getByRole("heading", { name: "Job y" })).toBeInTheDocument();
    expect(screen.queryByText("Late x")).not.toBeInTheDocument();
    expect(row("Job x").getByRole("button", { name: "Details" })).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps collection items on items:null snapshots and coalesces detail refreshes", async () => {
    const parent = (second: number, items: JobItem[] | null) => job("p", {
      job_type: "collection", status: "downloading", updated_at: at(second),
      total_items: 1, completed_items: 0, failed_items: 0, items,
    });
    server.active = [parent(1, null)];
    server.jobs.set("p", parent(1, [childItem("one")]));
    server.activity.set("p", []);
    const { stream, user } = await mount();
    await user.click(row("Job p").getByRole("button", { name: "Details" }));
    await settle();
    const results = () => within(screen.getByRole("region", { name: "Collection results" }));
    expect(results().getByText(/Child one/)).toBeInTheDocument();

    const held = onceFor((url) => url === "/api/v1/jobs/p");
    stream.emit("job.updated", parent(2, null));
    stream.emit("job.updated", parent(3, null));
    stream.emit("job.updated", parent(4, null));
    expect(results().getByText(/Child one/)).toBeInTheDocument();
    expect(count((url) => url === "/api/v1/jobs/p")).toBe(2);

    server.jobs.set("p", parent(4, [childItem("two")]));
    held.resolve(json(parent(2, [childItem("stale")])));
    await settle();
    expect(count((url) => url === "/api/v1/jobs/p")).toBe(3);
    expect(results().getByText(/Child two/)).toBeInTheDocument();
    expect(screen.queryByText(/Child stale/)).not.toBeInTheDocument();
  });
});

describe("QueuePage detail freshness", () => {
  it("does not let an older failed detail read mark a newer rich items snapshot as error", async () => {
    const parent = (second: number, items: JobItem[] | null) => job("p", {
      job_type: "collection", status: "downloading", updated_at: at(second),
      total_items: 1, completed_items: 0, failed_items: 0, items,
    });
    server.active = [parent(1, null)];
    server.activity.set("p", []);
    server.actions.set("/api/v1/jobs/p/cancel", () => Promise.resolve(json(parent(3, [childItem("fresh")]))));
    const first = onceFor((url) => url === "/api/v1/jobs/p");
    const trailing = onceFor((url) => url === "/api/v1/jobs/p");
    const { stream, user } = await mount();
    await user.click(row("Job p").getByRole("button", { name: "Details" }));
    stream.emit("job.updated", parent(2, null));
    await user.click(row("Job p").getByRole("button", { name: "Cancel" }));
    await settle();
    const results = () => within(screen.getByRole("region", { name: "Collection results" }));
    expect(results().getByText(/Child fresh/)).toBeInTheDocument();

    first.resolve(serverError());
    await settle();
    expect(screen.queryByText(/Current item details unavailable/)).not.toBeInTheDocument();
    expect(results().getByText(/Child fresh/)).toBeInTheDocument();
    expect(results().getByRole("list", { name: "Current item details" })).toBeInTheDocument();

    trailing.resolve(json(parent(3, [childItem("fresh")])));
    await settle();
    expect(count((url) => url === "/api/v1/jobs/p")).toBe(2);
    expect(screen.queryByText(/Current item details unavailable/)).not.toBeInTheDocument();
  });

  it("still flags a current detail read failure", async () => {
    server.active = [job("p", { job_type: "collection", status: "downloading", items: null, total_items: 1 })];
    server.activity.set("p", []);
    const failing = onceFor((url) => url === "/api/v1/jobs/p");
    const { user } = await mount();
    await user.click(row("Job p").getByRole("button", { name: "Details" }));
    failing.resolve(serverError());
    await settle();
    expect(screen.getByText(/Current item details unavailable/)).toBeInTheDocument();
  });
});

describe("QueuePage actions", () => {
  it("blocks a double Cancel, never repeats on error, reads state once and clears the error on a newer snapshot", async () => {
    server.active = [job("a", { status: "downloading", updated_at: at(5) })];
    server.jobs.set("a", job("a", { status: "downloading", updated_at: at(5) }));
    const cancel = deferred();
    server.actions.set("/api/v1/jobs/a/cancel", () => cancel.promise);
    const { stream } = await mount();
    const button = row("Job a").getByRole("button", { name: "Cancel" });
    act(() => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(count((url) => url === "/api/v1/jobs/a/cancel")).toBe(1);
    expect(row("Job a").getByRole("button", { name: "Cancelling…" })).toBeDisabled();

    cancel.resolve(serverError());
    await settle();
    expect(row("Job a").getByRole("alert")).toHaveTextContent("Server exploded.");
    expect(count((url) => url === "/api/v1/jobs/a/cancel")).toBe(1);
    expect(count((url) => url === "/api/v1/jobs/a")).toBe(1);
    expect(row("Job a").getByRole("button", { name: "Cancel" })).toBeEnabled();

    stream.emit("job.updated", job("a", { status: "downloading", updated_at: at(6) }));
    expect(row("Job a").queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not let a stale Retry response override a newer SSE snapshot", async () => {
    const failed = job("f", { status: "failed", error: retryable, updated_at: at(5) });
    server.pages.set("", { jobs: [failed], next_cursor: null });
    const retry = deferred();
    server.actions.set("/api/v1/jobs/f/retry", () => retry.promise);
    const { stream, user } = await mount();
    await user.click(row("Job f").getByRole("button", { name: "Retry" }));
    expect(rowTitles(historyRegion())).toEqual(["Job f"]);

    stream.emit("job.updated", job("f", { status: "downloading", updated_at: at(7) }));
    stream.emit("job.completed", job("f", { status: "completed", updated_at: at(8) }));
    retry.resolve(json(job("f", { status: "queued", updated_at: at(6) })));
    await settle();
    expect(rowTitles(activeRegion())).toEqual([]);
    expect(row("Job f").getByText("Completed")).toBeInTheDocument();
    expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
  });

  it("keeps Retry disabled while a long Cancel is pending even after a terminal SSE snapshot", async () => {
    server.active = [job("a", { status: "downloading", updated_at: at(5) })];
    const cancel = deferred();
    server.actions.set("/api/v1/jobs/a/cancel", () => cancel.promise);
    server.actions.set("/api/v1/jobs/a/retry", () => Promise.resolve(json(job("a", { updated_at: at(9) }))));
    const { stream, user } = await mount();
    await user.click(row("Job a").getByRole("button", { name: "Cancel" }));

    stream.emit("job.failed", job("a", { status: "failed", error: retryable, updated_at: at(6) }));
    expect(rowTitles(historyRegion())).toEqual(["Job a"]);
    const retryButton = row("Job a").getByRole("button", { name: "Retry" });
    expect(retryButton).toBeDisabled();
    fireEvent.click(retryButton);
    expect(count((url) => url.endsWith("/retry"))).toBe(0);

    cancel.resolve(json(job("a", { status: "downloading", updated_at: at(5) })));
    await settle();
    expect(row("Job a").getByText("Failed")).toBeInTheDocument();
    await user.click(row("Job a").getByRole("button", { name: "Retry" }));
    await settle();
    expect(count((url) => url.endsWith("/retry"))).toBe(1);
    expect(rowTitles(activeRegion())).toEqual(["Job a"]);
  });

  it("offers Retry on cancelled single history rows only when the backend marks them retryable", async () => {
    const cancelledError = (flag: boolean) => ({ code: "cancelled", message: "The download was cancelled.", retryable: flag });
    server.pages.set("", {
      jobs: [
        job("c1", { status: "cancelled", job_type: "single", error: cancelledError(true), created_at: at(9) }),
        job("c2", { status: "cancelled", job_type: "single", error: cancelledError(false), created_at: at(8) }),
      ],
      next_cursor: null,
    });
    server.actions.set("/api/v1/jobs/c1/retry", () => Promise.resolve(json(job("c1", { status: "queued", created_at: at(9), updated_at: at(20) }))));
    const { user } = await mount();
    expect(row("Job c2").queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    // Explicit only: nothing is retried before the user clicks.
    expect(count((url) => url.endsWith("/retry"))).toBe(0);

    await user.click(row("Job c1").getByRole("button", { name: "Retry" }));
    await settle();
    expect(count((url) => url === "/api/v1/jobs/c1/retry")).toBe(1);
    expect(rowTitles(activeRegion())).toEqual(["Job c1"]);
    expect(rowTitles(historyRegion())).toEqual(["Job c2"]);
  });

  it("shows Retry after cancelling an active single job once the cancelled snapshot is retryable", async () => {
    server.active = [job("a", { status: "downloading", job_type: "single", updated_at: at(5) })];
    server.actions.set("/api/v1/jobs/a/cancel", () => Promise.resolve(json(job("a", { status: "downloading", updated_at: at(5) }))));
    server.actions.set("/api/v1/jobs/a/retry", () => Promise.resolve(json(job("a", { status: "queued", updated_at: at(9) }))));
    const { stream, user } = await mount();
    await user.click(row("Job a").getByRole("button", { name: "Cancel" }));
    await settle();
    stream.emit("job.cancelled", job("a", {
      status: "cancelled", job_type: "single", updated_at: at(6),
      error: { code: "cancelled", message: "The download was cancelled.", retryable: true },
    }));
    expect(rowTitles(historyRegion())).toEqual(["Job a"]);
    expect(count((url) => url.endsWith("/retry"))).toBe(0);
    await user.click(row("Job a").getByRole("button", { name: "Retry" }));
    await settle();
    expect(count((url) => url === "/api/v1/jobs/a/retry")).toBe(1);
    expect(rowTitles(activeRegion())).toEqual(["Job a"]);
  });
});

describe("QueuePage inline History details", () => {
  const detailRegion = () => within(screen.getByRole("region", { name: "Job details" }));
  const failedJob = (id: string, overrides: Partial<Job> = {}) =>
    job(id, { status: "failed", error: retryable, updated_at: at(5), ...overrides });

  function seed() {
    const items = [
      failedJob("f", { created_at: at(9) }),
      job("g", { status: "completed", created_at: at(8) }),
    ];
    server.pages.set("", { jobs: items, next_cursor: null });
    for (const entry of items) {
      server.jobs.set(entry.id, entry);
      server.activity.set(entry.id, []);
    }
  }

  it("renders the History panel inside the selected row only, with a linked toggle; another row opens its own", async () => {
    seed();
    const { user } = await mount();
    await user.click(row("Job f").getByRole("button", { name: "Details" }));
    await settle();

    const panel = screen.getByRole("region", { name: "Job details" });
    expect(screen.getAllByRole("region", { name: "Job details" })).toHaveLength(1);
    expect(document.querySelectorAll("#queue-detail-f")).toHaveLength(1);
    expect(rowItem("Job f")).toContainElement(panel);
    expect(rowItem("Job g")).not.toContainElement(panel);
    expect(within(rowItem("Job f")).getAllByRole("region", { name: "Job details" })).toHaveLength(1);
    expect(within(rowItem("Job g")).queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    // Directly beneath the row summary, after it in DOM order.
    expect(rowItem("Job f").lastElementChild).toBe(panel);
    expect(rowTitles(historyRegion())).toEqual(["Job f", "Job g"]);

    const toggle = row("Job f").getByRole("button", { name: "Hide details" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-controls", "queue-detail-f");
    expect(toggle.getAttribute("aria-controls")).toBe(panel.id);
    expect(row("Job g").getByRole("button", { name: "Details" })).toHaveAttribute("aria-expanded", "false");

    // Previously the single panel moved; now another History row opens its
    // own inline panel and the first one stays open.
    await user.click(row("Job g").getByRole("button", { name: "Details" }));
    await settle();
    expect(screen.getAllByRole("region", { name: "Job details" })).toHaveLength(2);
    expect(within(rowItem("Job g")).getAllByRole("region", { name: "Job details" })).toHaveLength(1);
    expect(within(rowItem("Job f")).getAllByRole("region", { name: "Job details" })).toHaveLength(1);
    expect(row("Job f").getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-expanded", "true");
    expect(row("Job g").getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-controls", "queue-detail-g");
  });

  it("keeps the Active panel at the top, outside the row", async () => {
    server.active = [job("a", { status: "downloading" })];
    server.jobs.set("a", job("a", { status: "downloading" }));
    server.activity.set("a", []);
    const { user } = await mount();
    await user.click(row("Job a").getByRole("button", { name: "Details" }));
    await settle();

    const panel = screen.getByRole("region", { name: "Job details" });
    expect(rowItem("Job a")).not.toContainElement(panel);
    expect(within(activeRegion().getByRole("list")).queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    expect(panel.compareDocumentPosition(screen.getByRole("region", { name: "Active and queued" })))
      .toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(row("Job a").getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-controls", "queue-detail-a");
    expect(panel.id).toBe("queue-detail-a");
  });

  it("closes inline via the row Hide details only, keeps focus on it and keeps on-demand reads single", async () => {
    seed();
    const { user } = await mount();
    expect(count((url) => url.includes("/activity"))).toBe(0);
    await user.click(row("Job f").getByRole("button", { name: "Details" }));
    await settle();
    expect(count((url) => url === "/api/v1/jobs/f/activity")).toBe(1);
    expect(count((url) => url === "/api/v1/jobs/g/activity")).toBe(0);

    // No duplicate close control inside the inline panel.
    expect(detailRegion().queryByRole("button", { name: "Close details" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close details" })).not.toBeInTheDocument();
    const hide = row("Job f").getByRole("button", { name: "Hide details" });
    expect(hide).toHaveAttribute("aria-expanded", "true");
    expect(hide).toHaveAttribute("aria-controls", "queue-detail-f");

    await user.click(hide);
    expect(screen.queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    expect(row("Job f").getByRole("button", { name: "Details" })).toHaveFocus();
    expect(row("Job f").getByRole("button", { name: "Details" })).not.toHaveAttribute("aria-controls");
  });

  it("keeps a usable Close details on the top Active panel", async () => {
    server.active = [job("a", { status: "downloading" })];
    server.jobs.set("a", job("a", { status: "downloading" }));
    server.activity.set("a", []);
    const { user } = await mount();
    await user.click(row("Job a").getByRole("button", { name: "Details" }));
    await settle();
    expect(screen.getAllByRole("button", { name: "Close details" })).toHaveLength(1);

    await user.click(detailRegion().getByRole("button", { name: "Close details" }));
    expect(screen.queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    expect(row("Job a").getByRole("button", { name: "Details" })).toHaveAttribute("aria-expanded", "false");
  });

  it("closes via keyboard on the focused Hide details and keeps focus on the same toggle", async () => {
    seed();
    const { user } = await mount();
    await user.click(row("Job f").getByRole("button", { name: "Details" }));
    await settle();
    const hide = row("Job f").getByRole("button", { name: "Hide details" });
    hide.focus();
    expect(hide).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(screen.queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    const toggle = row("Job f").getByRole("button", { name: "Details" });
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).not.toHaveAttribute("aria-controls");

    // Space reopens and closes again on the same control.
    await user.keyboard(" ");
    await settle();
    expect(row("Job f").getByRole("button", { name: "Hide details" })).toHaveFocus();
    await user.keyboard(" ");
    expect(screen.queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    expect(row("Job f").getByRole("button", { name: "Details" })).toHaveFocus();
    expect(row("Job f").getByRole("button", { name: "Details" })).toHaveAttribute("aria-expanded", "false");
  });

  it("does not steal focus on background updates", async () => {
    seed();
    const { stream, user } = await mount();
    await user.click(row("Job g").getByRole("button", { name: "Details" }));
    await settle();
    const other = row("Job f").getByRole("button", { name: "Retry" });
    other.focus();

    stream.emit("job.updated", failedJob("f", { updated_at: at(20), title: "Job f" }));
    stream.open();
    await settle();
    expect(rowItem("Job g")).toContainElement(screen.getByRole("region", { name: "Job details" }));
    expect(row("Job f").getByRole("button", { name: "Retry" })).toHaveFocus();
  });

  it("falls back to the single top panel when a reset drops the row, and returns inline when it is back", async () => {
    seed();
    const { stream, user } = await mount();
    await user.click(row("Job f").getByRole("button", { name: "Details" }));
    await settle();
    expect(rowItem("Job f")).toContainElement(detailRegion().getByRole("heading", { name: "Job f" }));

    server.pages.set("", { jobs: [job("h", { status: "completed", created_at: at(9) })], next_cursor: "c2" });
    stream.open();
    await settle();
    expect(screen.queryByRole("listitem", { name: "Job f" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("region", { name: "Job details" })).toHaveLength(1);
    const top = screen.getByRole("region", { name: "Job details" });
    expect(historyRegion().queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    expect(top.compareDocumentPosition(screen.getByRole("region", { name: "Active and queued" })))
      .toBe(Node.DOCUMENT_POSITION_FOLLOWING);

    // Row back in History: rendered only inline, never twice.
    server.pages.set("", { jobs: [failedJob("f", { created_at: at(9) })], next_cursor: null });
    stream.open();
    await settle();
    expect(screen.getAllByRole("region", { name: "Job details" })).toHaveLength(1);
    expect(rowItem("Job f")).toContainElement(screen.getByRole("region", { name: "Job details" }));
    expect(document.querySelectorAll("#queue-detail-f")).toHaveLength(1);
  });

  it("relocates the panel to the top on inline Retry, keeps one panel and moves focus to it", async () => {
    seed();
    server.actions.set("/api/v1/jobs/f/retry", () => Promise.resolve(json(job("f", { status: "queued", updated_at: at(9) }))));
    const { user } = await mount();
    await user.click(row("Job f").getByRole("button", { name: "Details" }));
    await settle();
    expect(rowItem("Job f")).toContainElement(screen.getByRole("region", { name: "Job details" }));

    await user.click(detailRegion().getByRole("button", { name: "Retry" }));
    await settle();
    expect(rowTitles(activeRegion())).toEqual(["Job f"]);
    expect(rowTitles(historyRegion())).toEqual(["Job g"]);
    expect(screen.getAllByRole("region", { name: "Job details" })).toHaveLength(1);
    const panel = screen.getByRole("region", { name: "Job details" });
    expect(rowItem("Job f")).not.toContainElement(panel);
    expect(panel).toHaveFocus();
    expect(detailRegion().getByText("Queued")).toBeInTheDocument();
    expect(row("Job f").getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-controls", "queue-detail-f");
  });

  it("shows an inline panel-started Retry error once, inside the panel and not in the row summary", async () => {
    seed();
    server.actions.set("/api/v1/jobs/f/retry", () => Promise.resolve(serverError()));
    const { user } = await mount();
    await user.click(row("Job f").getByRole("button", { name: "Details" }));
    await settle();
    await user.click(detailRegion().getByRole("button", { name: "Retry" }));
    await settle();

    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(detailRegion().getByRole("alert")).toHaveTextContent("Server exploded.");
    expect(row("Job f").queryByRole("alert")).not.toBeInTheDocument();
    expect(rowItem("Job f")).toContainElement(screen.getByRole("alert"));
    expect(screen.getByRole("region", { name: "Job details" })).not.toHaveFocus();
  });
});

describe("QueuePage Activity expanded state and Retry focus", () => {
  const detailRegion = () => within(screen.getByRole("region", { name: "Job details" }));
  const failedJob = (id: string, overrides: Partial<Job> = {}) =>
    job(id, { status: "failed", error: retryable, updated_at: at(5), ...overrides });
  const activityToggle = () => detailRegion().getByRole("button", { name: /Activity/ });

  function seed() {
    const items = [
      failedJob("f", { created_at: at(9) }),
      job("g", { status: "completed", created_at: at(8) }),
    ];
    server.pages.set("", { jobs: items, next_cursor: null });
    for (const entry of items) {
      server.jobs.set(entry.id, entry);
      server.activity.set(entry.id, [activity(1, entry.id, `Saw ${entry.id}`)]);
    }
  }

  async function openExpanded(user: ReturnType<typeof userEvent.setup>, title: string) {
    await user.click(row(title).getByRole("button", { name: "Details" }));
    await settle();
    expect(activityToggle()).toHaveAttribute("aria-expanded", "false");
    await user.click(activityToggle());
    expect(activityToggle()).toHaveAttribute("aria-expanded", "true");
  }

  it("keeps Activity expanded and visible when a reset moves the panel inline → top → inline", async () => {
    seed();
    const { stream, user } = await mount();
    await openExpanded(user, "Job f");
    expect(detailRegion().getByText("Saw f")).toBeInTheDocument();

    server.pages.set("", { jobs: [job("h", { status: "completed", created_at: at(9) })], next_cursor: "c2" });
    stream.open();
    await settle();
    expect(screen.queryByRole("listitem", { name: "Job f" })).not.toBeInTheDocument();
    expect(activityToggle()).toHaveAttribute("aria-expanded", "true");
    expect(detailRegion().getByText("Saw f")).toBeInTheDocument();

    server.pages.set("", { jobs: [failedJob("f", { created_at: at(9) })], next_cursor: null });
    stream.open();
    await settle();
    expect(rowItem("Job f")).toContainElement(screen.getByRole("region", { name: "Job details" }));
    expect(activityToggle()).toHaveAttribute("aria-expanded", "true");
    expect(detailRegion().getByText("Saw f")).toBeInTheDocument();
  });

  it("keeps Activity expanded when inline Retry relocates the panel to the top", async () => {
    seed();
    server.actions.set("/api/v1/jobs/f/retry", () => Promise.resolve(json(job("f", { status: "queued", updated_at: at(9) }))));
    const { user } = await mount();
    await openExpanded(user, "Job f");

    await user.click(detailRegion().getByRole("button", { name: "Retry" }));
    await settle();
    expect(rowTitles(activeRegion())).toEqual(["Job f"]);
    expect(rowItem("Job f")).not.toContainElement(screen.getByRole("region", { name: "Job details" }));
    expect(activityToggle()).toHaveAttribute("aria-expanded", "true");
    expect(detailRegion().getByText("Saw f")).toBeInTheDocument();
  });

  // Previously a second job replaced the first; now both stay open with
  // independent Activity state, and every close/reopen still collapses.
  it("keeps Activity per open detail: another job starts collapsed, close/reopen (same ID, 404) resets", async () => {
    seed();
    const { stream, user } = await mount();
    await openExpanded(user, "Job f");
    const toggleIn = (title: string) =>
      within(within(rowItem(title)).getByRole("region", { name: "Job details" })).getByRole("button", { name: /Activity/ });

    await user.click(row("Job g").getByRole("button", { name: "Details" }));
    await settle();
    expect(toggleIn("Job g")).toHaveAttribute("aria-expanded", "false");
    expect(toggleIn("Job f")).toHaveAttribute("aria-expanded", "true");

    await user.click(toggleIn("Job g"));
    await user.click(row("Job g").getByRole("button", { name: "Hide details" }));
    expect(toggleIn("Job f")).toHaveAttribute("aria-expanded", "true");
    await user.click(row("Job g").getByRole("button", { name: "Details" }));
    await settle();
    expect(toggleIn("Job g")).toHaveAttribute("aria-expanded", "false");

    await user.click(toggleIn("Job g"));
    await user.click(row("Job g").getByRole("button", { name: "Hide details" }));
    await user.click(row("Job g").getByRole("button", { name: "Details" }));
    await settle();
    expect(toggleIn("Job g")).toHaveAttribute("aria-expanded", "false");

    // A 404 close also resets, and only for that job.
    await user.click(toggleIn("Job g"));
    server.jobs.delete("g");
    stream.open();
    await settle();
    expect(within(rowItem("Job g")).queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    expect(toggleIn("Job f")).toHaveAttribute("aria-expanded", "true");
    server.jobs.set("g", job("g", { status: "completed", created_at: at(8) }));
    await user.click(row("Job g").getByRole("button", { name: "Details" }));
    await settle();
    expect(toggleIn("Job g")).toHaveAttribute("aria-expanded", "false");
    expect(toggleIn("Job f")).toHaveAttribute("aria-expanded", "true");
  });

  it("keeps focus on another control chosen while the inline Retry is pending", async () => {
    seed();
    const retry = deferred();
    server.actions.set("/api/v1/jobs/f/retry", () => retry.promise);
    const { user } = await mount();
    await user.click(row("Job f").getByRole("button", { name: "Details" }));
    await settle();
    await user.click(detailRegion().getByRole("button", { name: "Retry" }));
    const other = row("Job g").getByRole("button", { name: "Details" });
    other.focus();
    expect(other).toHaveFocus();

    retry.resolve(json(job("f", { status: "queued", updated_at: at(9) })));
    await settle();
    expect(rowTitles(activeRegion())).toEqual(["Job f"]);
    expect(screen.getAllByRole("region", { name: "Job details" })).toHaveLength(1);
    expect(other).toHaveFocus();
    expect(screen.getByRole("region", { name: "Job details" })).not.toHaveFocus();
  });
});

describe("QueuePage detail Retry", () => {
  const detailRegion = () => within(screen.getByRole("region", { name: "Job details" }));
  const failedJob = (id = "f", overrides: Partial<Job> = {}) =>
    job(id, { status: "failed", error: retryable, updated_at: at(5), ...overrides });

  async function openDetail(user: ReturnType<typeof userEvent.setup>, title: string) {
    await user.click(row(title).getByRole("button", { name: "Details" }));
    await settle();
  }

  function seedFailed(id = "f") {
    const failed = failedJob(id);
    server.pages.set("", { jobs: [failed], next_cursor: null });
    server.jobs.set(id, failed);
    server.activity.set(id, []);
    return failed;
  }

  it("offers Retry in the detail only for retryable failed/cancelled jobs", async () => {
    const cancelledRetryable = job("c1", {
      status: "cancelled", created_at: at(9),
      error: { code: "cancelled", message: "Cancelled.", retryable: true },
    });
    const items = [
      failedJob("f", { created_at: at(10) }),
      cancelledRetryable,
      failedJob("n", { created_at: at(8), error: { ...retryable, retryable: false } }),
      job("done", { status: "completed", created_at: at(7) }),
    ];
    server.active = [job("act", { status: "downloading" })];
    server.pages.set("", { jobs: items, next_cursor: null });
    for (const entry of [...items, ...server.active]) {
      server.jobs.set(entry.id, entry);
      server.activity.set(entry.id, []);
    }
    const { user } = await mount();
    // Several History details are open at once: scope to each job's panel.
    const panelOf = (id: string) => within(document.getElementById(`queue-detail-${id}`) as HTMLElement);

    await openDetail(user, "Job f");
    expect(panelOf("f").getByRole("button", { name: "Retry" })).toBeEnabled();
    await openDetail(user, "Job c1");
    expect(panelOf("c1").getByRole("button", { name: "Retry" })).toBeEnabled();
    for (const [title, id] of [["Job n", "n"], ["Job done", "done"], ["Job act", "act"]]) {
      await openDetail(user, title);
      expect(panelOf(id).queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    }
    expect(screen.getAllByRole("region", { name: "Job details" })).toHaveLength(5);
    expect(count((url) => url.endsWith("/retry"))).toBe(0);
    // The History row toggle stays as is (still open: History details stay open together).
    expect(row("Job f").getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-controls", "queue-detail-f");
  });

  it("shares pending with the row, sends one POST on a double trigger and moves the job to active", async () => {
    seedFailed();
    const retry = deferred();
    server.actions.set("/api/v1/jobs/f/retry", () => retry.promise);
    const { user } = await mount();
    await openDetail(user, "Job f");
    const button = detailRegion().getByRole("button", { name: "Retry" });
    act(() => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
    expect(detailRegion().getByRole("button", { name: "Retrying…" })).toBeDisabled();
    expect(row("Job f").getByRole("button", { name: "Retrying…" })).toBeDisabled();

    retry.resolve(json(job("f", { status: "queued", updated_at: at(9) })));
    await settle();
    expect(rowTitles(activeRegion())).toEqual(["Job f"]);
    expect(rowTitles(historyRegion())).toEqual([]);
    expect(detailRegion().getByText("Queued")).toBeInTheDocument();
    expect(detailRegion().queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("disables the detail Retry while the row Retry is pending and the reverse", async () => {
    seedFailed();
    const retry = deferred();
    server.actions.set("/api/v1/jobs/f/retry", () => retry.promise);
    const { user } = await mount();
    await openDetail(user, "Job f");
    await user.click(row("Job f").getByRole("button", { name: "Retry" }));
    const detailButton = detailRegion().getByRole("button", { name: "Retrying…" });
    expect(detailButton).toBeDisabled();
    fireEvent.click(detailButton);
    expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
  });

  it("keeps the detail Retry disabled (not Retrying…) while a Cancel is pending", async () => {
    server.active = [job("a", { status: "downloading", updated_at: at(5) })];
    server.jobs.set("a", job("a", { status: "downloading", updated_at: at(5) }));
    server.activity.set("a", []);
    const cancel = deferred();
    server.actions.set("/api/v1/jobs/a/cancel", () => cancel.promise);
    const { stream, user } = await mount();
    await openDetail(user, "Job a");
    await user.click(row("Job a").getByRole("button", { name: "Cancel" }));

    stream.emit("job.failed", failedJob("a", { updated_at: at(6) }));
    const retryButton = detailRegion().getByRole("button", { name: "Retry" });
    expect(retryButton).toBeDisabled();
    fireEvent.click(retryButton);
    expect(count((url) => url.endsWith("/retry"))).toBe(0);
    cancel.resolve(json(job("a", { status: "downloading", updated_at: at(5) })));
    await settle();
    expect(detailRegion().getByRole("button", { name: "Retry" })).toBeEnabled();
  });

  it("does not let a stale Retry response override a newer SSE snapshot from the detail", async () => {
    seedFailed();
    const retry = deferred();
    server.actions.set("/api/v1/jobs/f/retry", () => retry.promise);
    const { stream, user } = await mount();
    await openDetail(user, "Job f");
    await user.click(detailRegion().getByRole("button", { name: "Retry" }));

    stream.emit("job.completed", job("f", { status: "completed", updated_at: at(8) }));
    retry.resolve(json(job("f", { status: "queued", updated_at: at(6) })));
    await settle();
    expect(rowTitles(activeRegion())).toEqual([]);
    expect(detailRegion().getByText("Completed")).toBeInTheDocument();
    expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
  });

  it("shows a detail Retry error once, makes no repeat POST and allows a manual retry", async () => {
    seedFailed();
    const retry = deferred();
    server.actions.set("/api/v1/jobs/f/retry", () => retry.promise);
    const { user } = await mount();
    await openDetail(user, "Job f");
    const reads = count((url) => url === "/api/v1/jobs/f");
    await user.click(detailRegion().getByRole("button", { name: "Retry" }));
    retry.resolve(serverError());
    await settle();

    // Started from the panel: the single alert is in the panel, not the row.
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(detailRegion().getByRole("alert")).toHaveTextContent("Server exploded.");
    expect(row("Job f").queryByRole("alert")).not.toBeInTheDocument();
    expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
    expect(count((url) => url === "/api/v1/jobs/f")).toBe(reads + 1);
    expect(detailRegion().getByRole("button", { name: "Retry" })).toBeEnabled();

    server.actions.set("/api/v1/jobs/f/retry", () => Promise.resolve(json(job("f", { status: "queued", updated_at: at(9) }))));
    await user.click(detailRegion().getByRole("button", { name: "Retry" }));
    await settle();
    expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(2);
    expect(rowTitles(activeRegion())).toEqual(["Job f"]);
  });

  it("retries from the detail after a reset dropped the row and lists the job again via merge", async () => {
    seedFailed();
    server.actions.set("/api/v1/jobs/f/retry", () => Promise.resolve(json(job("f", { status: "queued", updated_at: at(9) }))));
    const { stream, user } = await mount();
    await openDetail(user, "Job f");

    server.pages.set("", { jobs: [job("h", { status: "completed", created_at: at(9) })], next_cursor: "c2" });
    stream.open();
    await settle();
    expect(screen.queryByRole("listitem", { name: "Job f" })).not.toBeInTheDocument();

    // Row-less failure: the panel itself carries the single alert.
    server.actions.set("/api/v1/jobs/f/retry", () => Promise.resolve(serverError()));
    await user.click(detailRegion().getByRole("button", { name: "Retry" }));
    await settle();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(detailRegion().getByRole("alert")).toHaveTextContent("Server exploded.");
    expect(detailRegion().getByRole("button", { name: "Retry" })).toBeEnabled();

    server.actions.set("/api/v1/jobs/f/retry", () => Promise.resolve(json(job("f", { status: "queued", updated_at: at(9) }))));
    server.active = [job("f", { status: "queued", updated_at: at(9) })];
    await user.click(detailRegion().getByRole("button", { name: "Retry" }));
    await settle();
    expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(2);
    expect(rowTitles(activeRegion())).toEqual(["Job f"]);
    expect(rowTitles(historyRegion())).toEqual(["Job h"]);
  });

  // Drops the row of "f" with a reset while its detail stays open (keyed by ID).
  async function dropRow(stream: FakeEventSource) {
    server.pages.set("", { jobs: [job("h", { status: "completed", created_at: at(9) })], next_cursor: "c2" });
    stream.open();
    await settle();
    expect(screen.queryByRole("listitem", { name: "Job f" })).not.toBeInTheDocument();
  }

  describe("error placement", () => {
    beforeEach(() => {
      seedFailed();
      server.actions.set("/api/v1/jobs/f/retry", () => Promise.resolve(serverError()));
    });

    it("shows a row-started error only in the row and keeps it when the details close", async () => {
      const { user } = await mount();
      await openDetail(user, "Job f");
      await user.click(row("Job f").getByRole("button", { name: "Retry" }));
      await settle();
      expect(screen.getAllByRole("alert")).toHaveLength(1);
      expect(row("Job f").getByRole("alert")).toHaveTextContent("Server exploded.");
      expect(detailRegion().queryByRole("alert")).not.toBeInTheDocument();

      await user.click(row("Job f").getByRole("button", { name: "Hide details" }));
      expect(screen.getAllByRole("alert")).toHaveLength(1);
      expect(row("Job f").getByRole("alert")).toHaveTextContent("Server exploded.");
      expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
    });

    it("moves a panel-started error to the row when the details close instead of losing it", async () => {
      const { user } = await mount();
      await openDetail(user, "Job f");
      await user.click(detailRegion().getByRole("button", { name: "Retry" }));
      await settle();
      expect(detailRegion().getByRole("alert")).toHaveTextContent("Server exploded.");
      expect(row("Job f").queryByRole("alert")).not.toBeInTheDocument();

      await user.click(row("Job f").getByRole("button", { name: "Hide details" }));
      expect(screen.getAllByRole("alert")).toHaveLength(1);
      expect(row("Job f").getByRole("alert")).toHaveTextContent("Server exploded.");
    });

    it("keeps a usable Close details on the rowless fallback panel", async () => {
      const { stream, user } = await mount();
      await openDetail(user, "Job f");
      expect(detailRegion().queryByRole("button", { name: "Close details" })).not.toBeInTheDocument();

      await dropRow(stream);
      await user.click(detailRegion().getByRole("button", { name: "Close details" }));
      expect(screen.queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    });

    it("falls back to the panel when the row drops and does not jump back or double when it returns", async () => {
      const { stream, user } = await mount();
      await openDetail(user, "Job f");
      await user.click(row("Job f").getByRole("button", { name: "Retry" }));
      await settle();
      expect(row("Job f").getByRole("alert")).toBeInTheDocument();

      await dropRow(stream);
      expect(screen.getAllByRole("alert")).toHaveLength(1);
      expect(detailRegion().getByRole("alert")).toHaveTextContent("Server exploded.");

      server.pages.set("", { jobs: [failedJob("f")], next_cursor: null });
      stream.open();
      await settle();
      expect(row("Job f").getByRole("button", { name: "Retry" })).toBeInTheDocument();
      expect(screen.getAllByRole("alert")).toHaveLength(1);
      expect(detailRegion().getByRole("alert")).toHaveTextContent("Server exploded.");
      expect(row("Job f").queryByRole("alert")).not.toBeInTheDocument();
      expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
    });

    it("keeps a Cancel error in the row only", async () => {
      server.active = [job("a", { status: "downloading", updated_at: at(5) })];
      server.jobs.set("a", job("a", { status: "downloading", updated_at: at(5) }));
      server.activity.set("a", []);
      server.actions.set("/api/v1/jobs/a/cancel", () => Promise.resolve(serverError()));
      const { user } = await mount();
      await openDetail(user, "Job a");
      await user.click(row("Job a").getByRole("button", { name: "Cancel" }));
      await settle();
      expect(screen.getAllByRole("alert")).toHaveLength(1);
      expect(row("Job a").getByRole("alert")).toHaveTextContent("Server exploded.");
      expect(detailRegion().queryByRole("alert")).not.toBeInTheDocument();
    });
  });

  describe("snapshot winner", () => {
    const stale = () => failedJob("f", { updated_at: at(5) });

    async function openDroppedDetail() {
      seedFailed();
      const mounted = await mount();
      await openDetail(mounted.user, "Job f");
      await dropRow(mounted.stream);
      return mounted;
    }

    function expectCompletedWithoutRetry() {
      expect(detailRegion().getByText("Completed")).toBeInTheDocument();
      expect(screen.queryAllByRole("button", { name: "Retry" })).toHaveLength(0);
    }

    it("keeps Completed when a delayed older merge response re-adds the Failed row", async () => {
      const { stream } = await openDroppedDetail();
      const held = onceFor((url) => url === "/api/v1/jobs?scope=history");
      stream.emit("job.completed", job("f", { status: "completed", updated_at: at(8) }));
      expect(screen.queryByRole("listitem", { name: "Job f" })).not.toBeInTheDocument();
      expect(detailRegion().getByText("Completed")).toBeInTheDocument();

      held.resolve(json({ jobs: [stale(), job("h", { status: "completed", created_at: at(9) })], next_cursor: "c2" }));
      await settle();
      expect(row("Job f").getByText("Completed")).toBeInTheDocument();
      expectCompletedWithoutRetry();
    });

    it("keeps Completed when a delayed older Load more page re-adds the Failed row", async () => {
      const { stream, user } = await openDroppedDetail();
      const held = onceFor((url) => isHistoryCursor("c2")(url));
      await user.click(screen.getByRole("button", { name: "Load more" }));
      stream.emit("job.completed", job("f", { status: "completed", updated_at: at(8) }));
      await settle();

      held.resolve(json({ jobs: [stale()], next_cursor: null }));
      await settle();
      expect(row("Job f").getByText("Completed")).toBeInTheDocument();
      expectCompletedWithoutRetry();
    });

    it("keeps Completed when an older reset head re-adds the Failed row", async () => {
      const { stream } = await openDroppedDetail();
      stream.emit("job.completed", job("f", { status: "completed", updated_at: at(8) }));
      await settle();
      server.pages.set("", { jobs: [stale()], next_cursor: null });
      server.jobs.set("f", stale());
      stream.open();
      await settle();
      expect(row("Job f").getByText("Completed")).toBeInTheDocument();
      expectCompletedWithoutRetry();
    });

    it("reset: an older active Queued answer outside the head leaves the ID in neither list", async () => {
      const { stream } = await openDroppedDetail();
      stream.emit("job.completed", job("f", { status: "completed", updated_at: at(8) }));
      await settle();
      server.jobs.set("f", job("f", { status: "completed", updated_at: at(8) }));
      server.active = [job("f", { status: "queued", updated_at: at(6) })];
      stream.open();
      await settle();

      expect(screen.queryByRole("listitem", { name: "Job f" })).not.toBeInTheDocument();
      expect(rowTitles(activeRegion())).toEqual([]);
      expect(rowTitles(historyRegion())).toEqual(["Job h"]);
      expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
      expectCompletedWithoutRetry();
    });

    it("reset: a Failed head row with the same update time keeps the known Completed everywhere", async () => {
      const { stream } = await openDroppedDetail();
      stream.emit("job.completed", job("f", { status: "completed", updated_at: at(8) }));
      await settle();
      server.jobs.set("f", job("f", { status: "completed", updated_at: at(8) }));
      server.pages.set("", {
        jobs: [job("h", { status: "completed", created_at: at(9) }), failedJob("f", { updated_at: at(8) })],
        next_cursor: "c2",
      });
      stream.open();
      await settle();

      expect(rowTitles(historyRegion())).toEqual(["Job h", "Job f"]);
      expect(row("Job f").getByText("Completed")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();
      expectCompletedWithoutRetry();
    });

    it("keeps loaded collection children when the returning row carries items: null", async () => {
      const parent = job("f", {
        job_type: "collection", status: "failed", error: retryable, updated_at: at(5),
        total_items: 1, completed_items: 0, failed_items: 1, items: null,
      });
      server.pages.set("", { jobs: [parent], next_cursor: null });
      server.jobs.set("f", { ...parent, items: [childItem("one")] });
      server.activity.set("f", []);
      const { stream, user } = await mount();
      await openDetail(user, "Job f");
      const results = () => within(screen.getByRole("region", { name: "Collection results" }));
      expect(results().getByText(/Child one/)).toBeInTheDocument();

      await dropRow(stream);
      expect(results().getByText(/Child one/)).toBeInTheDocument();
      // A merge (no detail re-read) brings the same snapshot back with items: null.
      server.pages.set("", { jobs: [parent], next_cursor: null });
      const reads = count((url) => url === "/api/v1/jobs/f");
      stream.fail();
      await settle();
      expect(row("Job f").getByRole("button", { name: "Retry" })).toBeInTheDocument();
      expect(results().getByText(/Child one/)).toBeInTheDocument();
      expect(count((url) => url === "/api/v1/jobs/f")).toBe(reads);
    });
  });

  describe("recovery", () => {
    it("does not insert the row from a successful Retry POST until a scoped response lists it", async () => {
      seedFailed();
      server.actions.set("/api/v1/jobs/f/retry", () => Promise.resolve(json(job("f", { status: "queued", updated_at: at(9) }))));
      const { stream, user } = await mount();
      await openDetail(user, "Job f");
      await dropRow(stream);

      const heldActive = onceFor((url) => url === "/api/v1/jobs?scope=active");
      await user.click(detailRegion().getByRole("button", { name: "Retry" }));
      await settle();
      expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
      expect(detailRegion().getByText("Queued")).toBeInTheDocument();
      expect(screen.queryByRole("listitem", { name: "Job f" })).not.toBeInTheDocument();
      expect(rowTitles(activeRegion())).toEqual([]);

      server.active = [job("f", { status: "queued", updated_at: at(9) })];
      heldActive.resolve(json({ jobs: server.active, next_cursor: null }));
      await settle();
      expect(rowTitles(activeRegion())).toEqual(["Job f"]);
    });

    it("recovers a lost Retry response with one GET and polling, without a second POST", async () => {
      seedFailed();
      server.actions.set("/api/v1/jobs/f/retry", () => Promise.reject(new TypeError("Failed to fetch")));
      const { stream, user } = await mount();
      stream.fail();
      await settle();
      await openDetail(user, "Job f");
      // The server accepted the Retry although the response was lost.
      const queued = job("f", { status: "queued", updated_at: at(9) });
      server.jobs.set("f", queued);
      server.active = [queued];
      const reads = count((url) => url === "/api/v1/jobs/f");

      await user.click(row("Job f").getByRole("button", { name: "Retry" }));
      await settle();
      expect(count((url) => url === "/api/v1/jobs/f")).toBe(reads + 1);
      expect(rowTitles(activeRegion())).toEqual(["Job f"]);
      expect(screen.queryAllByRole("alert")).toHaveLength(0);

      await tick(4000);
      expect(rowTitles(activeRegion())).toEqual(["Job f"]);
      expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
    });
  });
});

describe("QueuePage multiple History details", () => {
  const failedJob = (id: string, overrides: Partial<Job> = {}) =>
    job(id, { status: "failed", error: retryable, updated_at: at(5), ...overrides });
  const panelEl = (id: string) => document.getElementById(`queue-detail-${id}`);
  const panel = (id: string) => {
    const element = panelEl(id);
    if (!element) throw new Error(`No detail panel for ${id}`);
    return within(element);
  };
  const regions = () => screen.queryAllByRole("region", { name: "Job details" });
  // The single upper panel: outside both list sections.
  const topPanels = () => regions().filter((element) =>
    !screen.getByRole("region", { name: "History" }).contains(element) &&
    !screen.getByRole("region", { name: "Active and queued" }).contains(element));
  const inlineIn = (title: string) => within(rowItem(title)).queryAllByRole("region", { name: "Job details" });

  function seed(history: Job[], active: Job[] = []) {
    server.active = active;
    server.pages.set("", { jobs: history, next_cursor: null });
    for (const entry of [...history, ...active]) {
      server.jobs.set(entry.id, entry);
      server.activity.set(entry.id, [activity(1, entry.id, `Saw ${entry.id}`)]);
    }
  }
  const threeHistory = () => [
    failedJob("f", { created_at: at(9) }),
    job("g", { status: "completed", created_at: at(8) }),
    job("k", { status: "completed", created_at: at(7) }),
  ];

  async function open(user: ReturnType<typeof userEvent.setup>, title: string) {
    await user.click(row(title).getByRole("button", { name: "Details" }));
    await settle();
  }

  async function reset(stream: FakeEventSource, history: Job[], nextCursor: string | null = "c2") {
    server.pages.set("", { jobs: history, next_cursor: nextCursor });
    stream.open();
    await settle();
  }

  it("opens several History details inline and closes each independently with its own toggle", async () => {
    seed(threeHistory());
    const { user } = await mount();
    for (const title of ["Job f", "Job g", "Job k"]) await open(user, title);

    expect(regions()).toHaveLength(3);
    expect(topPanels()).toHaveLength(0);
    for (const id of ["f", "g", "k"]) {
      expect(inlineIn(`Job ${id}`)).toHaveLength(1);
      expect(count((url) => url === `/api/v1/jobs/${id}`)).toBe(1);
      expect(count((url) => url === `/api/v1/jobs/${id}/activity`)).toBe(1);
    }
    expect(screen.queryByRole("button", { name: "Close details" })).not.toBeInTheDocument();

    await user.click(row("Job g").getByRole("button", { name: "Hide details" }));
    expect(row("Job g").getByRole("button", { name: "Details" })).toHaveFocus();
    expect(inlineIn("Job g")).toHaveLength(0);
    expect(inlineIn("Job f")).toHaveLength(1);
    expect(inlineIn("Job k")).toHaveLength(1);
    await settle();
    // Closing one never re-reads or reloads the others.
    for (const id of ["f", "k"]) {
      expect(count((url) => url === `/api/v1/jobs/${id}`)).toBe(1);
      expect(count((url) => url === `/api/v1/jobs/${id}/activity`)).toBe(1);
    }
    await user.click(row("Job f").getByRole("button", { name: "Hide details" }));
    expect(regions()).toHaveLength(1);
    expect(inlineIn("Job k")).toHaveLength(1);
  });

  it("uses unique detail/Activity DOM ids with matching aria-controls and independent Activity expansion", async () => {
    seed(threeHistory().slice(0, 2));
    const { stream, user } = await mount();
    await open(user, "Job f");
    await open(user, "Job g");

    const ids = regions().map((element) => element.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const id of ["f", "g"]) {
      expect(row(`Job ${id}`).getByRole("button", { name: "Hide details" }).getAttribute("aria-controls")).toBe(`queue-detail-${id}`);
      expect(panelEl(id)).not.toBeNull();
    }
    const activityToggle = (id: string) => panel(id).getByRole("button", { name: /Activity/ });
    expect(activityToggle("f").getAttribute("aria-controls")).toBe("queue-activity-f");
    expect(activityToggle("g").getAttribute("aria-controls")).toBe("queue-activity-g");

    await user.click(activityToggle("f"));
    expect(activityToggle("f")).toHaveAttribute("aria-expanded", "true");
    expect(activityToggle("g")).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById("queue-activity-f")).toHaveTextContent("Saw f");
    expect(document.getElementById("queue-activity-g")).toBeNull();
    await user.click(activityToggle("g"));
    expect(document.getElementById("queue-activity-g")).toHaveTextContent("Saw g");
    expect(document.querySelectorAll("#activity-list")).toHaveLength(0);

    // Live Activity lands only in its own job's panel.
    stream.emit("activity.created", activity(2, "g", "Only g"));
    expect(panel("g").getByText("Only g")).toBeInTheDocument();
    expect(panel("f").queryByText("Only g")).not.toBeInTheDocument();
    await user.click(activityToggle("f"));
    expect(activityToggle("f")).toHaveAttribute("aria-expanded", "false");
    expect(activityToggle("g")).toHaveAttribute("aria-expanded", "true");
  });

  it("isolates overlapping per-ID reads and ignores old responses after a same-ID close/reopen", async () => {
    seed(threeHistory().slice(0, 2));
    const slowF = onceFor((url) => url === "/api/v1/jobs/f");
    const slowFActivity = onceFor((url) => url === "/api/v1/jobs/f/activity");
    const slowG = onceFor((url) => url === "/api/v1/jobs/g");
    const { user } = await mount();
    await open(user, "Job f");
    await open(user, "Job g");
    expect(count((url) => url === "/api/v1/jobs/g")).toBe(1);

    // g's read resolves while f's is still in flight: each applies to its own ID.
    slowG.resolve(json(job("g", { status: "completed", created_at: at(8), title: "Fresh g", updated_at: at(30) })));
    await settle();
    expect(panel("g").getByRole("heading", { name: "Fresh g" })).toBeInTheDocument();
    expect(panel("f").getByRole("heading", { name: "Job f" })).toBeInTheDocument();

    await user.click(row("Job f").getByRole("button", { name: "Hide details" }));
    await open(user, "Job f");
    expect(count((url) => url === "/api/v1/jobs/f")).toBe(2);
    await user.click(panel("f").getByRole("button", { name: /Activity/ }));
    expect(panel("f").getByText("Saw f")).toBeInTheDocument();

    slowF.resolve(json(failedJob("f", { created_at: at(9), title: "Late f", updated_at: at(40) })));
    slowFActivity.resolve(json({ events: [activity(9, "f", "Late")] }));
    await settle();
    expect(screen.queryByText("Late f")).not.toBeInTheDocument();
    expect(panel("f").queryByText("Late")).not.toBeInTheDocument();
    expect(panel("f").getByRole("heading", { name: "Job f" })).toBeInTheDocument();
    expect(panel("g").getByRole("heading", { name: "Fresh g" })).toBeInTheDocument();
    expect(regions()).toHaveLength(2);
  });

  it("on reset shows only the most recently opened dropped detail at the top; Close and 404 never promote", async () => {
    seed(threeHistory());
    const { stream, user } = await mount();
    for (const title of ["Job f", "Job g", "Job k"]) await open(user, title);

    await reset(stream, [job("h", { status: "completed", created_at: at(10) })]);
    expect(rowTitles(historyRegion())).toEqual(["Job h"]);
    expect(regions()).toHaveLength(1);
    expect(topPanels()).toHaveLength(1);
    expect(panel("k").getByRole("heading", { name: "Job k" })).toBeInTheDocument();
    expect(panel("k").getByRole("button", { name: "Close details" })).toBeInTheDocument();
    // Every open detail (hidden ones too) is re-read on the reset.
    for (const id of ["f", "g", "k"]) {
      expect(count((url) => url === `/api/v1/jobs/${id}`)).toBe(2);
      expect(count((url) => url === `/api/v1/jobs/${id}/activity`)).toBe(2);
    }

    // Manual Close frees the top without promoting another hidden detail.
    await user.click(panel("k").getByRole("button", { name: "Close details" }));
    await settle();
    expect(regions()).toHaveLength(0);

    // The next reset promotes the most recently opened remaining one.
    await reset(stream, [job("h", { status: "completed", created_at: at(10) })]);
    expect(regions()).toHaveLength(1);
    expect(panel("g").getByRole("heading", { name: "Job g" })).toBeInTheDocument();

    // A 404 close of the top does not promote f within the same reset.
    server.jobs.delete("g");
    await reset(stream, [job("h", { status: "completed", created_at: at(10) })]);
    expect(regions()).toHaveLength(0);
    expect(panelEl("g")).toBeNull();

    // f was kept selected: its row returns with its still-open panel inline.
    await reset(stream, [failedJob("f", { created_at: at(9) })], null);
    expect(regions()).toHaveLength(1);
    expect(inlineIn("Job f")).toHaveLength(1);
    expect(topPanels()).toHaveLength(0);
    expect(panel("f").queryByRole("button", { name: "Close details" })).not.toBeInTheDocument();
    expect(row("Job f").getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-controls", "queue-detail-f");
  });

  it("keeps dropped History selections while an Active top is open and returns them inline", async () => {
    const history = threeHistory().slice(0, 2);
    seed(history, [job("a", { status: "downloading" })]);
    const { stream, user } = await mount();
    for (const title of ["Job a", "Job f", "Job g"]) await open(user, title);
    expect(topPanels()).toHaveLength(1);
    expect(regions()).toHaveLength(3);

    await reset(stream, [job("h", { status: "completed", created_at: at(10) })]);
    expect(regions()).toHaveLength(1);
    expect(panel("a").getByRole("button", { name: "Close details" })).toBeInTheDocument();

    await reset(stream, history, null);
    expect(regions()).toHaveLength(3);
    expect(inlineIn("Job f")).toHaveLength(1);
    expect(inlineIn("Job g")).toHaveLength(1);
    expect(topPanels()).toEqual([panelEl("a")]);
  });

  it("moves a delayed inline Retry into the top, closes the replaced Active detail and keeps other History details", async () => {
    seed(threeHistory().slice(0, 2), [job("a", { status: "downloading" })]);
    const retry = deferred();
    server.actions.set("/api/v1/jobs/f/retry", () => retry.promise);
    const { stream, user } = await mount();
    for (const title of ["Job a", "Job f", "Job g"]) await open(user, title);

    await user.click(panel("f").getByRole("button", { name: "Retry" }));
    expect(topPanels()).toEqual([panelEl("a")]);
    expect(inlineIn("Job f")).toHaveLength(1);
    retry.resolve(json(job("f", { status: "queued", created_at: at(9), updated_at: at(9) })));
    await settle();

    expect(rowTitles(activeRegion())).toEqual(["Job a", "Job f"]);
    expect(topPanels()).toEqual([panelEl("f")]);
    expect(panel("f").getByRole("button", { name: "Close details" })).toBeInTheDocument();
    expect(panelEl("f")).toHaveFocus();
    expect(panelEl("a")).toBeNull();
    expect(row("Job a").getByRole("button", { name: "Details" })).toHaveAttribute("aria-expanded", "false");
    expect(inlineIn("Job g")).toHaveLength(1);
    expect(regions()).toHaveLength(2);
    expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);

    // Back to History: inline again; the closed Active detail does not pop back.
    stream.emit("job.completed", job("f", { status: "completed", created_at: at(9), updated_at: at(20) }));
    expect(inlineIn("Job f")).toHaveLength(1);
    expect(topPanels()).toHaveLength(0);
    expect(panelEl("a")).toBeNull();
    expect(regions()).toHaveLength(2);
  });

  it("follows an authoritative SSE or reconcile move to Active into the single top panel", async () => {
    seed(threeHistory(), [job("a", { status: "downloading" })]);
    const { stream, user } = await mount();
    await open(user, "Job a");
    await open(user, "Job g");
    await open(user, "Job k");

    stream.emit("job.updated", job("g", { status: "queued", created_at: at(8), updated_at: at(20) }));
    expect(topPanels()).toEqual([panelEl("g")]);
    expect(panelEl("a")).toBeNull();
    expect(inlineIn("Job k")).toHaveLength(1);

    const queuedK = job("k", { status: "queued", created_at: at(7), updated_at: at(30) });
    server.active = [job("a", { status: "downloading" }), queuedK];
    server.jobs.set("g", job("g", { status: "queued", created_at: at(8), updated_at: at(20) }));
    server.jobs.set("k", queuedK);
    await reset(stream, [failedJob("f", { created_at: at(9) })], null);
    expect(topPanels()).toEqual([panelEl("k")]);
    expect(panelEl("g")).toBeNull();
    expect(regions()).toHaveLength(1);
    // No mutation was sent for these authoritative moves.
    expect(count((url) => url.endsWith("/retry"))).toBe(0);
  });

  it("keeps one Active top panel: opening another Active row replaces it", async () => {
    seed([], [job("a", { status: "downloading" }), job("b", { status: "downloading", created_at: at(1) })]);
    const { user } = await mount();
    await open(user, "Job a");
    await open(user, "Job b");
    expect(regions()).toHaveLength(1);
    expect(topPanels()).toEqual([panelEl("b")]);
    expect(row("Job a").getByRole("button", { name: "Details" })).toHaveAttribute("aria-expanded", "false");
    expect(row("Job b").getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-controls", "queue-detail-b");
    await user.click(panel("b").getByRole("button", { name: "Close details" }));
    expect(regions()).toHaveLength(0);
  });

  it("keeps a displaced terminal fallback (Activity expanded) hidden after a Retry takes the top, without returning after Close/404", async () => {
    const x = failedJob("x", { created_at: at(9) });
    const t = job("t", { status: "completed", created_at: at(8) });
    seed([x, t]);
    const queuedX = job("x", { status: "queued", created_at: at(9), updated_at: at(9) });
    server.actions.set("/api/v1/jobs/x/retry", () => Promise.resolve(json(queuedX)));
    const { stream, user } = await mount();
    await open(user, "Job t");
    await open(user, "Job x");
    // t's row drops: it becomes the terminal fallback on top.
    await reset(stream, [x], null);
    expect(topPanels()).toEqual([panelEl("t")]);
    await user.click(panel("t").getByRole("button", { name: /Activity/ }));
    expect(panel("t").getByText("Saw t")).toBeInTheDocument();

    server.active = [queuedX];
    server.jobs.set("x", queuedX);
    await user.click(panel("x").getByRole("button", { name: "Retry" }));
    await settle();
    expect(topPanels()).toEqual([panelEl("x")]);
    expect(regions()).toHaveLength(1);
    // Hidden, not closed: no panel for t is rendered anywhere.
    expect(panelEl("t")).toBeNull();

    // Manual Close of the new top does not bring t back.
    await user.click(panel("x").getByRole("button", { name: "Close details" }));
    await settle();
    expect(regions()).toHaveLength(0);

    // Reopened x closes via 404 at a reset that still lacks t's row: t stays hidden.
    await open(user, "Job x");
    expect(topPanels()).toEqual([panelEl("x")]);
    server.jobs.delete("x");
    await reset(stream, [], null);
    expect(regions()).toHaveLength(0);

    // t's row returns: its still-open panel is inline with Activity data and expanded state kept.
    await reset(stream, [t], null);
    expect(inlineIn("Job t")).toHaveLength(1);
    expect(topPanels()).toHaveLength(0);
    expect(panel("t").getByRole("button", { name: /Activity/ })).toHaveAttribute("aria-expanded", "true");
    expect(panel("t").getByText("Saw t")).toBeInTheDocument();
    expect(count((url) => url === "/api/v1/jobs/x/retry")).toBe(1);
  });

  it("puts a rowless detail on top from its winning Active SSE/GET snapshot with the listing held, one top throughout", async () => {
    seed([failedJob("f", { created_at: at(9) }), job("g", { status: "completed", created_at: at(8) })]);
    const { stream, user } = await mount();
    await open(user, "Job f");
    await open(user, "Job g");
    await reset(stream, [job("h", { status: "completed", created_at: at(10) })]);
    expect(topPanels()).toEqual([panelEl("g")]);

    // SSE reports f (rowless, hidden) active while the scoped listing is held.
    const heldActive = onceFor((url) => url === "/api/v1/jobs?scope=active");
    stream.emit("job.updated", job("f", { status: "queued", created_at: at(9), updated_at: at(20) }));
    expect(topPanels()).toEqual([panelEl("f")]);
    expect(regions()).toHaveLength(1);
    expect(panel("f").getByText("Queued")).toBeInTheDocument();
    // No list membership from SSE; the displaced terminal g stays hidden.
    expect(rowTitles(activeRegion())).toEqual([]);
    expect(panelEl("g")).toBeNull();

    heldActive.resolve(json({ jobs: [], next_cursor: null }));
    await settle();
    expect(topPanels()).toEqual([panelEl("f")]);
    expect(rowTitles(activeRegion())).toEqual([]);

    // A reset's detail GET reports g active: g takes the top, the active f selection closes.
    server.jobs.set("g", job("g", { status: "queued", created_at: at(8), updated_at: at(30) }));
    await reset(stream, [job("h", { status: "completed", created_at: at(10) })]);
    expect(topPanels()).toEqual([panelEl("g")]);
    expect(regions()).toHaveLength(1);
    expect(panelEl("f")).toBeNull();
    expect(rowTitles(activeRegion())).toEqual([]);

    // A stale terminal snapshot never reverses the placement.
    stream.emit("job.failed", failedJob("g", { created_at: at(8), updated_at: at(6) }));
    expect(topPanels()).toEqual([panelEl("g")]);
    expect(panel("g").getByText("Queued")).toBeInTheDocument();
  });

  it("keeps one top through reversed concurrent Retries of separate IDs with one POST each", async () => {
    seed(threeHistory(), [job("a", { status: "downloading" })]);
    server.pages.set("", {
      jobs: [failedJob("f", { created_at: at(9) }), failedJob("g", { created_at: at(8) }), job("k", { status: "completed", created_at: at(7) })],
      next_cursor: null,
    });
    const retryF = deferred();
    const retryG = deferred();
    server.actions.set("/api/v1/jobs/f/retry", () => retryF.promise);
    server.actions.set("/api/v1/jobs/g/retry", () => retryG.promise);
    const { user } = await mount();
    for (const title of ["Job a", "Job f", "Job g", "Job k"]) await open(user, title);

    await user.click(panel("f").getByRole("button", { name: "Retry" }));
    await user.click(panel("g").getByRole("button", { name: "Retry" }));
    expect(topPanels()).toEqual([panelEl("a")]);

    retryG.resolve(json(job("g", { status: "queued", created_at: at(8), updated_at: at(20) })));
    await settle();
    expect(topPanels()).toEqual([panelEl("g")]);
    expect(panelEl("a")).toBeNull();
    expect(inlineIn("Job f")).toHaveLength(1);

    retryF.resolve(json(job("f", { status: "queued", created_at: at(9), updated_at: at(21) })));
    await settle();
    expect(topPanels()).toEqual([panelEl("f")]);
    expect(panelEl("g")).toBeNull();
    expect(panelEl("a")).toBeNull();
    expect(inlineIn("Job k")).toHaveLength(1);
    expect(regions()).toHaveLength(2);
    expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
    expect(count((url) => url === "/api/v1/jobs/g/retry")).toBe(1);
  });

  it("keeps a single top when one scoped reply moves several open History details to Active", async () => {
    seed(threeHistory(), [job("a", { status: "downloading" })]);
    const { stream, user } = await mount();
    for (const title of ["Job a", "Job f", "Job g", "Job k"]) await open(user, title);

    const moved = [
      job("f", { status: "queued", created_at: at(9), updated_at: at(20) }),
      job("g", { status: "queued", created_at: at(8), updated_at: at(20) }),
    ];
    server.active = [job("a", { status: "downloading" }), ...moved];
    for (const entry of moved) server.jobs.set(entry.id, entry);
    await reset(stream, [job("k", { status: "completed", created_at: at(7) })], null);

    // Invariant only: exactly one of the moved details is on top, the others closed.
    const tops = topPanels();
    expect(tops).toHaveLength(1);
    expect(["queue-detail-f", "queue-detail-g"]).toContain(tops[0].id);
    expect([panelEl("f"), panelEl("g")].filter((element) => element != null)).toHaveLength(1);
    expect(panelEl("a")).toBeNull();
    expect(inlineIn("Job k")).toHaveLength(1);
    expect(regions()).toHaveLength(2);
  });

  it("does not show a hidden detail's Retry error anywhere and shows it once on the right surface when its row returns", async () => {
    seed([failedJob("f", { created_at: at(9) }), job("g", { status: "completed", created_at: at(8) })]);
    server.actions.set("/api/v1/jobs/f/retry", () => Promise.resolve(serverError()));
    const { stream, user } = await mount();
    await open(user, "Job g");
    await open(user, "Job f");
    await reset(stream, [job("h", { status: "completed", created_at: at(10) })]);
    expect(topPanels()).toEqual([panelEl("f")]);

    await user.click(panel("f").getByRole("button", { name: "Retry" }));
    await settle();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(panel("f").getByRole("alert")).toHaveTextContent("Server exploded.");

    // g (rowless) goes Active via SSE: f is displaced and hidden.
    stream.emit("job.updated", job("g", { status: "queued", created_at: at(8), updated_at: at(20) }));
    await settle();
    expect(topPanels()).toEqual([panelEl("g")]);
    expect(panelEl("f")).toBeNull();
    expect(screen.queryAllByRole("alert")).toHaveLength(0);

    await reset(stream, [failedJob("f", { created_at: at(9) })], null);
    expect(inlineIn("Job f")).toHaveLength(1);
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(panel("f").getByRole("alert")).toHaveTextContent("Server exploded.");
    expect(row("Job f").queryByRole("alert")).not.toBeInTheDocument();
    expect(count((url) => url === "/api/v1/jobs/f/retry")).toBe(1);
  });

  describe("newest-wins across several open detail snapshots", () => {
    async function openTwoDropped() {
      seed([failedJob("f", { created_at: at(9) }), failedJob("g", { created_at: at(8) })]);
      const mounted = await mount();
      await open(mounted.user, "Job f");
      await open(mounted.user, "Job g");
      await reset(mounted.stream, [job("h", { status: "completed", created_at: at(10) })]);
      expect(rowTitles(historyRegion())).toEqual(["Job h"]);
      return mounted;
    }
    const stale = () => [failedJob("f", { created_at: at(9) }), failedJob("g", { created_at: at(8) })];
    const completedBoth = (stream: FakeEventSource) => {
      for (const [id, created] of [["f", 9], ["g", 8]] as const) {
        stream.emit("job.completed", job(id, { status: "completed", created_at: at(created), updated_at: at(20) }));
      }
    };
    function expectBothCompleted() {
      expect(row("Job f").getByText("Completed")).toBeInTheDocument();
      expect(row("Job g").getByText("Completed")).toBeInTheDocument();
      expect(screen.queryAllByRole("button", { name: "Retry" })).toHaveLength(0);
    }

    it("merge: a delayed older head re-adding both rows keeps both newer snapshots", async () => {
      const { stream } = await openTwoDropped();
      const held = onceFor((url) => url === "/api/v1/jobs?scope=history");
      completedBoth(stream);
      expect(screen.queryByRole("listitem", { name: "Job f" })).not.toBeInTheDocument();
      held.resolve(json({ jobs: [job("h", { status: "completed", created_at: at(10) }), ...stale()], next_cursor: "c2" }));
      await settle();
      expectBothCompleted();
    });

    it("reset: an older head keeps both newer snapshots and returns both inline", async () => {
      const { stream } = await openTwoDropped();
      completedBoth(stream);
      await settle();
      for (const entry of stale()) server.jobs.set(entry.id, entry);
      await reset(stream, stale(), null);
      expectBothCompleted();
      expect(inlineIn("Job f")).toHaveLength(1);
      expect(inlineIn("Job g")).toHaveLength(1);
    });

    it("Load more: a delayed older page keeps both newer snapshots", async () => {
      const { stream, user } = await openTwoDropped();
      const held = onceFor((url) => isHistoryCursor("c2")(url));
      await user.click(screen.getByRole("button", { name: "Load more" }));
      completedBoth(stream);
      await settle();
      held.resolve(json({ jobs: stale(), next_cursor: null }));
      await settle();
      expectBothCompleted();
    });

    it("prior snapshots never create list membership", async () => {
      const { stream } = await openTwoDropped();
      completedBoth(stream);
      await settle();
      await reset(stream, [job("h", { status: "completed", created_at: at(10) })]);
      expect(rowTitles(historyRegion())).toEqual(["Job h"]);
      expect(rowTitles(activeRegion())).toEqual([]);
      expect(screen.queryByRole("listitem", { name: "Job f" })).not.toBeInTheDocument();
      expect(screen.queryByRole("listitem", { name: "Job g" })).not.toBeInTheDocument();
    });
  });
});

describe("QueuePage Clear History", () => {
  const PREVIEW = "/api/v1/jobs/clear-history/preview";
  const CONFIRM = "/api/v1/jobs/clear-history/confirm";
  const bodies = (url: string) => server.fetch.mock.calls
    .filter(([input, init]) => String(input) === url && init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init?.body)) as unknown);
  const group = () => within(screen.getByRole("group", { name: "Clear History" }));
  const trigger = () => group().getByRole("button", { name: "Clear History" });
  const refreshButton = () => group().queryByRole("button", { name: "Refresh queue" });
  const checkbox = () => group().getByRole("checkbox", { name: "Include cancelled jobs" });
  const keep = () => group().getByRole("button", { name: "Keep history" });
  const confirmButton = () => group().queryByRole("button", { name: /^(Delete \d+ jobs?|Deleting…)$/ });
  const previewOk = (previewId: string, jobCount: number) => () =>
    Promise.resolve(json({ preview_id: previewId, job_count: jobCount }));
  const expired = () => json({
    error: { code: "clear_preview_expired", message: "This Clear History preview has expired. Preview again.", retryable: false, job_id: null },
  }, 409);
  const clearFailed = () => json({
    error: { code: "clear_history_failed", message: "Clear History could not finish. Refresh and preview again.", retryable: false, job_id: null },
  }, 500);

  function seed() {
    const items = [
      job("x", { status: "completed", created_at: at(9) }),
      job("y", { status: "failed", error: retryable, created_at: at(8) }),
    ];
    server.pages.set("", { jobs: items, next_cursor: null });
    for (const entry of items) {
      server.jobs.set(entry.id, entry);
      server.activity.set(entry.id, []);
    }
  }

  /** Server-side removal: the next reset head no longer lists, and GET 404s for, these ids. */
  function serverDeletes(...ids: string[]) {
    const page = server.pages.get("") ?? { jobs: [], next_cursor: null };
    server.pages.set("", { ...page, jobs: page.jobs.filter((entry) => !ids.includes(entry.id)) });
    for (const id of ids) {
      server.jobs.delete(id);
      server.activity.delete(id);
    }
  }

  it("starts unchecked, previews false by default, shows only the server count and never confirms by itself", async () => {
    seed();
    server.actions.set(PREVIEW, previewOk("p1", 7));
    const { user } = await mount();
    expect(checkbox()).not.toBeChecked();
    expect(bodies(PREVIEW)).toEqual([]);

    await user.click(trigger());
    await settle();
    expect(bodies(PREVIEW)).toEqual([{ include_cancelled: false }]);
    // The server count (7), not the two visible History rows.
    expect(group().getByText("Clear History will delete 7 jobs. This cannot be undone.")).toBeInTheDocument();
    expect(confirmButton()).toHaveAccessibleName("Delete 7 jobs");
    expect(confirmButton()).toHaveAccessibleDescription("Clear History will delete 7 jobs. This cannot be undone.");
    expect(rowTitles(historyRegion())).toEqual(["Job x", "Job y"]);
    expect(group().queryByText(/byte|MiB|GiB|Job x/)).not.toBeInTheDocument();
    expect(checkbox()).toBeDisabled();
    expect(trigger()).toBeDisabled();

    await tick(6000);
    expect(bodies(CONFIRM)).toEqual([]);
    expect(bodies(PREVIEW)).toHaveLength(1);
  });

  it("opts in with include_cancelled=true, locks it while pending and dismisses without deleting; the next attempt previews fresh", async () => {
    seed();
    const pending = deferred();
    server.once.push({ match: (url, method) => url === PREVIEW && method === "POST", respond: () => pending.promise });
    server.actions.set(PREVIEW, previewOk("p2", 3));
    server.actions.set(CONFIRM, () => Promise.resolve(json({ deleted_count: 3, failed_count: 0 })));
    const { user } = await mount();

    await user.click(checkbox());
    expect(checkbox()).toBeChecked();
    await user.click(trigger());
    expect(bodies(PREVIEW)).toEqual([{ include_cancelled: true }]);
    expect(checkbox()).toBeDisabled();
    expect(trigger()).toBeDisabled();
    expect(group().getByRole("status")).toHaveTextContent("Counting history jobs…");
    pending.resolve(json({ preview_id: "p1", job_count: 5 }));
    await settle();
    expect(confirmButton()).toHaveAccessibleName("Delete 5 jobs");
    fireEvent.click(checkbox());
    expect(checkbox()).toBeChecked();

    await user.click(keep());
    expect(confirmButton()).not.toBeInTheDocument();
    expect(group().queryByText(/will delete/)).not.toBeInTheDocument();
    expect(checkbox()).toBeEnabled();
    expect(bodies(CONFIRM)).toEqual([]);

    await user.click(trigger());
    await settle();
    expect(bodies(PREVIEW)).toEqual([{ include_cancelled: true }, { include_cancelled: true }]);
    expect(confirmButton()).toHaveAccessibleName("Delete 3 jobs");
    await user.click(confirmButton()!);
    await settle();
    expect(bodies(CONFIRM)).toEqual([{ preview_id: "p2" }]);
  });

  it("offers no confirm for a zero preview", async () => {
    seed();
    server.actions.set(PREVIEW, previewOk("p0", 0));
    const { user } = await mount();
    await user.click(trigger());
    await settle();
    expect(confirmButton()).not.toBeInTheDocument();
    expect(group().queryByRole("button", { name: "Keep history" })).not.toBeInTheDocument();
    expect(group().getByRole("status")).toHaveTextContent("There are no history jobs to clear.");
    expect(checkbox()).toBeEnabled();
    expect(trigger()).toBeEnabled();
    expect(bodies(CONFIRM)).toEqual([]);
  });

  it("shows a fixed message on a preview failure without repeating it", async () => {
    seed();
    server.actions.set(PREVIEW, () => Promise.resolve(serverError()));
    const { user } = await mount();
    await user.click(trigger());
    await settle();
    expect(group().getByRole("alert")).toHaveTextContent("Could not preview Clear History. Try again.");
    expect(screen.queryByText("Server exploded.")).not.toBeInTheDocument();
    await tick(6000);
    expect(bodies(PREVIEW)).toHaveLength(1);
    expect(confirmButton()).not.toBeInTheDocument();
  });

  it("ignores a late preview after dismissal and requires a fresh one", async () => {
    seed();
    const late = deferred();
    server.once.push({ match: (url, method) => url === PREVIEW && method === "POST", respond: () => late.promise });
    const { user } = await mount();
    await user.click(trigger());
    await user.click(keep());
    late.resolve(json({ preview_id: "stale", job_count: 4 }));
    await settle();
    expect(confirmButton()).not.toBeInTheDocument();
    expect(group().queryByText(/will delete/)).not.toBeInTheDocument();
    expect(checkbox()).toBeEnabled();

    server.actions.set(PREVIEW, previewOk("fresh", 2));
    server.actions.set(CONFIRM, () => Promise.resolve(json({ deleted_count: 2, failed_count: 0 })));
    await user.click(trigger());
    await settle();
    await user.click(confirmButton()!);
    await settle();
    expect(bodies(CONFIRM)).toEqual([{ preview_id: "fresh" }]);
  });

  it("sends a double confirm click once, consuming the token before the request settles", async () => {
    seed();
    server.actions.set(PREVIEW, previewOk("p1", 2));
    const held = deferred();
    server.actions.set(CONFIRM, () => held.promise);
    const { user } = await mount();
    await user.click(trigger());
    await settle();
    const button = confirmButton()!;
    act(() => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(bodies(CONFIRM)).toEqual([{ preview_id: "p1" }]);
    expect(confirmButton()).toHaveAccessibleName("Deleting…");
    expect(confirmButton()).toBeDisabled();
    expect(keep()).toBeDisabled();
    expect(checkbox()).toBeDisabled();
    fireEvent.click(keep());
    expect(confirmButton()).toBeInTheDocument();

    held.resolve(json({ deleted_count: 2, failed_count: 0 }));
    await settle();
    fireEvent.click(trigger());
    await settle();
    expect(bodies(CONFIRM)).toEqual([{ preview_id: "p1" }]);
    expect(bodies(PREVIEW)).toHaveLength(2);
  });

  it("shows the actual deleted count and removes rows only via the server reset", async () => {
    seed();
    server.actions.set(PREVIEW, previewOk("p1", 3));
    const held = deferred();
    server.actions.set(CONFIRM, () => held.promise);
    const { user } = await mount();
    await user.click(trigger());
    await settle();
    await user.click(confirmButton()!);
    // No optimistic removal while pending.
    expect(rowTitles(historyRegion())).toEqual(["Job x", "Job y"]);

    const resetsBefore = headCalls();
    serverDeletes("x", "y");
    held.resolve(json({ deleted_count: 2, failed_count: 0 }));
    await settle();
    expect(group().getByRole("status")).toHaveTextContent("Deleted 2 jobs.");
    expect(headCalls()).toBe(resetsBefore + 1);
    expect(rowTitles(historyRegion())).toEqual([]);
    expect(checkbox()).toBeEnabled();
    expect(bodies(CONFIRM)).toHaveLength(1);
  });

  it.each([
    [1, 2, "Deleted 1 job. 2 jobs could not be deleted."],
    [0, 1, "Deleted 0 jobs. 1 job could not be deleted."],
  ])("reports a partial failure (deleted %i, failed %i) and refreshes", async (deleted, failed, text) => {
    seed();
    server.actions.set(PREVIEW, previewOk("p1", 3));
    server.actions.set(CONFIRM, () => Promise.resolve(json({ deleted_count: deleted, failed_count: failed })));
    const { user } = await mount();
    await user.click(trigger());
    await settle();
    if (deleted > 0) serverDeletes("x");
    await user.click(confirmButton()!);
    await settle();
    expect(group().getByRole("alert")).toHaveTextContent(text);
    expect(rowTitles(historyRegion())).toEqual(deleted > 0 ? ["Job y"] : ["Job x", "Job y"]);
    expect(bodies(CONFIRM)).toHaveLength(1);
  });

  it.each([
    ["expired preview (409)", () => Promise.resolve(expired()), "This preview has expired. Preview Clear History again.", false],
    ["server failure (500)", () => Promise.resolve(clearFailed()), "Clear History could not finish. Check the list and preview again.", true],
    ["network rejection", () => Promise.reject(new TypeError("Failed to fetch")), "Clear History could not finish. Check the list and preview again.", true],
  ])("%s: fixed message, one reset that removes server-deleted rows and closes the open detail by 404, no repeat", async (_label, respond, text, unknown) => {
    seed();
    server.actions.set(PREVIEW, previewOk("p1", 2));
    server.actions.set(CONFIRM, respond);
    const { user } = await mount();
    await user.click(row("Job x").getByRole("button", { name: "Details" }));
    await settle();
    expect(within(rowItem("Job x")).getByRole("region", { name: "Job details" })).toBeInTheDocument();
    await user.click(trigger());
    await settle();

    // Partial commit before the error: the server already removed x.
    serverDeletes("x");
    const detailReads = count((url) => url === "/api/v1/jobs/x");
    await user.click(confirmButton()!);
    await settle();
    expect(group().getByRole("alert")).toHaveTextContent(text);
    // Manual refresh only where the outcome is unknown, not on a proven expiry.
    if (unknown) expect(refreshButton()).toBeInTheDocument();
    else expect(refreshButton()).not.toBeInTheDocument();
    expect(screen.queryByText(/Server|Refresh and preview again|Failed to fetch/)).not.toBeInTheDocument();
    expect(count((url) => url === "/api/v1/jobs/x")).toBe(detailReads + 1);
    expect(screen.queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    expect(rowTitles(historyRegion())).toEqual(["Job y"]);

    await tick(6000);
    expect(bodies(CONFIRM)).toEqual([{ preview_id: "p1" }]);
    expect(bodies(PREVIEW)).toHaveLength(1);
    expect(confirmButton()).not.toBeInTheDocument();
  });

  it("keeps Retry and Cancel working beside Clear History", async () => {
    seed();
    server.active = [job("a", { status: "downloading", updated_at: at(5) })];
    server.actions.set("/api/v1/jobs/a/cancel", () => Promise.resolve(json(job("a", { status: "cancelled", updated_at: at(6) }))));
    server.actions.set("/api/v1/jobs/y/retry", () => Promise.resolve(json(job("y", { status: "queued", created_at: at(8), updated_at: at(9) }))));
    server.actions.set(PREVIEW, previewOk("p1", 2));
    const { user } = await mount();
    await user.click(trigger());
    await settle();
    await user.click(row("Job y").getByRole("button", { name: "Retry" }));
    await settle();
    expect(count((url) => url === "/api/v1/jobs/y/retry")).toBe(1);
    expect(rowTitles(activeRegion())).toContain("Job y");
    await user.click(row("Job a").getByRole("button", { name: "Cancel" }));
    await settle();
    expect(count((url) => url === "/api/v1/jobs/a/cancel")).toBe(1);
    // The preview stays as shown until the user confirms or dismisses.
    expect(confirmButton()).toHaveAccessibleName("Delete 2 jobs");
    expect(bodies(CONFIRM)).toEqual([]);
  });

  it("moves keyboard focus to Keep history on start and back to the trigger when the attempt ends", async () => {
    seed();
    server.actions.set(PREVIEW, previewOk("p1", 2));
    server.actions.set(CONFIRM, () => Promise.resolve(json({ deleted_count: 2, failed_count: 0 })));
    const { user } = await mount();
    trigger().focus();
    await user.keyboard("{Enter}");
    expect(keep()).toHaveFocus();
    await settle();
    expect(keep()).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(trigger()).toHaveFocus();

    await user.keyboard(" ");
    await settle();
    expect(keep()).toHaveFocus();
    await user.tab();
    expect(confirmButton()).toHaveFocus();
    await user.keyboard("{Enter}");
    await settle();
    expect(trigger()).toHaveFocus();
    expect(group().getByRole("status")).toHaveTextContent("Deleted 2 jobs.");
  });

  it("sends nothing after unmount and a remount starts with a clean, unchecked state", async () => {
    seed();
    server.actions.set(PREVIEW, previewOk("p1", 2));
    const held = deferred();
    server.actions.set(CONFIRM, () => held.promise);
    const { user, unmount } = await mount();
    await user.click(checkbox());
    await user.click(trigger());
    await settle();
    await user.click(confirmButton()!);
    unmount();
    const calls = server.fetch.mock.calls.length;
    held.resolve(json({ deleted_count: 2, failed_count: 0 }));
    await settle();
    expect(server.fetch.mock.calls.length).toBe(calls);

    FakeEventSource.instances = [];
    await mount();
    expect(checkbox()).not.toBeChecked();
    expect(confirmButton()).not.toBeInTheDocument();
    expect(group().getByRole("status")).toHaveTextContent(/^$/);
    expect(group().queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps the same Keep history button in the first action row while a deferred preview resolves; Delete comes below and next in Tab order", async () => {
    seed();
    const pending = deferred();
    server.once.push({ match: (url, method) => url === PREVIEW && method === "POST", respond: () => pending.promise });
    const { user } = await mount();
    trigger().focus();
    await user.keyboard("{Enter}");
    const keepBefore = keep();
    const status = group().getByRole("status");
    expect(keepBefore).toHaveFocus();
    expect(status).toHaveTextContent("Counting history jobs…");
    const actionRow = keepBefore.parentElement!;
    const rowTexts = () => Array.from(actionRow.children).map((child) => child.textContent);
    expect(rowTexts()).toEqual(["Clear History", "Keep history"]);

    pending.resolve(json({ preview_id: "p1", job_count: 4 }));
    await settle();
    // Same DOM button, same row, same preceding controls and texts.
    expect(keep()).toBe(keepBefore);
    expect(keepBefore.parentElement).toBe(actionRow);
    expect(rowTexts()).toEqual(["Clear History", "Keep history"]);
    expect(keepBefore).toHaveFocus();
    // The same persistent live region now carries the server count.
    expect(group().getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("Clear History will delete 4 jobs. This cannot be undone.");
    // Variable content after the Keep row; Delete in its own later row.
    const deleteButton = confirmButton()!;
    expect(actionRow).not.toContainElement(deleteButton);
    expect(actionRow.compareDocumentPosition(status)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(status.compareDocumentPosition(deleteButton)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);

    await user.tab();
    expect(deleteButton).toHaveFocus();
    await user.tab({ shift: true });
    expect(keepBefore).toHaveFocus();
    await user.click(keepBefore);
    expect(bodies(CONFIRM)).toEqual([]);
    expect(confirmButton()).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
  });

  it("offers a manual Refresh queue after an unknown confirm outcome that catches a delayed server deletion", async () => {
    seed();
    server.actions.set(PREVIEW, previewOk("p1", 2));
    server.actions.set(CONFIRM, () => Promise.resolve(clearFailed()));
    const { user } = await mount();
    await user.click(row("Job x").getByRole("button", { name: "Details" }));
    await settle();
    await user.click(trigger());
    await settle();
    await user.click(confirmButton()!);
    await settle();
    // The automatic reset still saw the old History and detail.
    expect(rowTitles(historyRegion())).toEqual(["Job x", "Job y"]);
    expect(within(rowItem("Job x")).getByRole("region", { name: "Job details" })).toBeInTheDocument();
    expect(group().getByRole("alert")).toHaveTextContent("Clear History could not finish. Check the list and preview again.");

    // The deletion becomes visible on the server only later.
    serverDeletes("x");
    const heads = headCalls();
    await user.click(refreshButton()!);
    await settle();
    expect(headCalls()).toBe(heads + 1);
    expect(rowTitles(historyRegion())).toEqual(["Job y"]);
    expect(screen.queryByRole("region", { name: "Job details" })).not.toBeInTheDocument();
    expect(bodies(CONFIRM)).toEqual([{ preview_id: "p1" }]);
    expect(bodies(PREVIEW)).toHaveLength(1);
  });
});

describe("QueueController Clear History lifecycle", () => {
  const PREVIEW = "/api/v1/jobs/clear-history/preview";
  const CONFIRM = "/api/v1/jobs/clear-history/confirm";
  async function flush() {
    for (let index = 0; index < 8; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
  }
  function make() {
    const views: QueueView[] = [];
    const controller = new QueueController((view) => views.push(view));
    return { controller, latest: () => views[views.length - 1] };
  }

  it("ignores a preview that settles after a stop/start and resets Clear state at start", async () => {
    const held = deferred();
    server.actions.set(PREVIEW, () => held.promise);
    const { controller, latest } = make();
    controller.start();
    await flush();
    controller.setClearIncludeCancelled(true);
    controller.startClearHistory();
    expect(latest().clear.phase).toBe("previewing");

    controller.stop();
    controller.start();
    expect(latest().clear).toEqual({ includeCancelled: false, phase: "idle", jobCount: null, message: null });
    held.resolve(json({ preview_id: "old", job_count: 3 }));
    await flush();
    expect(latest().clear.phase).toBe("idle");
    controller.confirmClearHistory();
    expect(count((url) => url === CONFIRM)).toBe(0);
    controller.stop();
  });

  it("ignores a confirm result after a stop/start but still asks the live session for a reset", async () => {
    server.actions.set(PREVIEW, () => Promise.resolve(json({ preview_id: "p1", job_count: 2 })));
    const held = deferred();
    server.actions.set(CONFIRM, () => held.promise);
    const { controller, latest } = make();
    controller.start();
    await flush();
    controller.startClearHistory();
    await flush();
    expect(latest().clear).toMatchObject({ phase: "ready", jobCount: 2 });
    controller.confirmClearHistory();
    controller.confirmClearHistory();
    expect(count((url) => url === CONFIRM)).toBe(1);

    controller.stop();
    controller.start();
    await flush();
    const heads = headCalls();
    held.resolve(json({ deleted_count: 2, failed_count: 0 }));
    await flush();
    expect(latest().clear).toEqual({ includeCancelled: false, phase: "idle", jobCount: null, message: null });
    expect(headCalls()).toBe(heads + 1);
    expect(count((url) => url === CONFIRM)).toBe(1);
    controller.stop();
  });
});
