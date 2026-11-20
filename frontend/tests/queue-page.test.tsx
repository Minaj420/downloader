import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ActivityEvent } from "../src/api/activity";
import type { Job, JobItem, ScopedJobList } from "../src/api/jobs";
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
const row = (title: string) => within(screen.getByRole("listitem", { name: title }));
const rowTitles = (region: ReturnType<typeof within>) =>
  region.queryAllByRole("listitem").map((item: HTMLElement) => item.getAttribute("aria-label"));

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

  it("ignores a stale detail response after the selection changes", async () => {
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
    slowJob.resolve(json(job("x", { status: "completed", title: "Late x", updated_at: at(40) })));
    slowActivity.resolve(json({ events: [activity(9, "x", "Late")] }));
    await settle();
    const detail = within(screen.getByRole("region", { name: "Job details" }));
    expect(detail.getByRole("heading", { name: "Job y" })).toBeInTheDocument();
    expect(screen.queryByText("Late x")).not.toBeInTheDocument();
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
});
