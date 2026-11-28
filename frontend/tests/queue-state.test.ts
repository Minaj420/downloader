import type { Job, JobItem } from "../src/api/jobs";
import {
  applyHistoryPage,
  applyMerge,
  applyReadSnapshot,
  applyReset,
  applySnapshot,
  decideSnapshot,
  EMPTY_QUEUE_LISTS,
  removeJob,
  snapshotTime,
  type QueueLists,
} from "../src/features/queue/queueState";

const progress = { percent: 0, downloaded_bytes: null, total_bytes: null, speed_bytes_per_second: null, eta_seconds: null };

function job(id: string, overrides: Partial<Job> = {}): Job {
  return {
    id, analysis_id: "a", variant_id: "v", status: "queued", title: `Job ${id}`,
    source_url: "https://example.com", destination: "app_library", progress,
    final_path: null, error: null,
    created_at: "2026-11-20T10:00:00.000000+00:00",
    updated_at: "2026-11-20T10:00:00.000001+00:00",
    started_at: null, completed_at: null, ...overrides,
  };
}

function item(id: string): JobItem {
  return {
    id, item_id: `sel-${id}`, source_order: 0, source_id: null, source_url: "https://example.com",
    title: id, playlist_index: null, track_number: null, disc_number: null, status: "queued",
    progress, final_path: null, error: null,
  };
}

const ids = (jobs: Job[]) => jobs.map((entry) => entry.id);
const lists = (active: Job[], history: Job[], nextCursor: string | null = null): QueueLists =>
  ({ active, history, nextCursor });

describe("snapshotTime", () => {
  it("keeps microsecond precision like the NewDownload helper", () => {
    expect(snapshotTime(job("x", { updated_at: "2026-11-20T10:00:00.000001Z" })) >
      snapshotTime(job("x", { updated_at: "2026-11-20T10:00:00.000000+00:00" }))).toBe(true);
    expect(snapshotTime(job("x", { updated_at: "2026-11-20T10:00:00Z" })))
      .toBe(snapshotTime(job("x", { updated_at: "2026-11-20T10:00:00.000000+00:00" })));
  });
});

describe("decideSnapshot", () => {
  const current = job("c", { job_type: "collection", status: "downloading", items: [item("old")] });

  it("rejects stale and equal snapshots", () => {
    expect(decideSnapshot(current, { ...current, updated_at: "2026-11-20T10:00:00Z" }, false)).toBeNull();
    expect(decideSnapshot(current, { ...current, title: "Other" }, false)).toBeNull();
    expect(decideSnapshot(current, { ...current, title: "Other", items: null }, true)).toBeNull();
  });

  it("allows equal-time items enrichment only from same-status REST", () => {
    const rest = { ...current, title: "Ignored", items: [item("new")] };
    const enriched = decideSnapshot(current, rest, true);
    expect(enriched?.items?.map((entry) => entry.id)).toEqual(["new"]);
    expect(enriched?.title).toBe("Job c");
    expect(decideSnapshot(current, rest, false)).toBeNull();
    expect(decideSnapshot(current, { ...rest, status: "failed" }, true)).toBeNull();
  });

  it("never deletes loaded items with an items:null snapshot", () => {
    const next = decideSnapshot(current, { ...current, items: null, updated_at: "2026-11-20T10:00:01Z" }, false);
    expect(next?.items?.map((entry) => entry.id)).toEqual(["old"]);
  });
});

describe("applySnapshot", () => {
  it("moves active → history and history → active, keeping lists disjoint and ordered", () => {
    const a = job("a", { created_at: "2026-11-20T09:00:00Z" });
    const b = job("b", { created_at: "2026-11-20T09:00:00Z" });
    const h = job("h", { status: "completed", created_at: "2026-11-20T08:00:00Z" });
    let state = lists([b, a], [h]);
    state = applySnapshot(state, { ...a, status: "failed", updated_at: "2026-11-20T11:00:00Z" }, { rich: false, insert: false }).lists;
    expect(ids(state.active)).toEqual(["b"]);
    expect(ids(state.history)).toEqual(["a", "h"]);
    state = applySnapshot(state, { ...h, status: "queued", updated_at: "2026-11-20T11:00:00Z" }, { rich: false, insert: false }).lists;
    expect(ids(state.active)).toEqual(["h", "b"]);
    expect(ids(state.history)).toEqual(["a"]);
  });

  it("orders active by created_at ASC, id ASC and history DESC", () => {
    const state = applyHistoryPage(EMPTY_QUEUE_LISTS, {
      jobs: [
        job("b", { status: "failed", created_at: "2026-11-20T09:00:00.000001Z" }),
        job("a", { status: "failed", created_at: "2026-11-20T09:00:00.000001Z" }),
        job("c", { status: "completed", created_at: "2026-11-20T09:00:00.000002Z" }),
        job("y", { created_at: "2026-11-20T09:00:00.000001Z" }),
        job("x", { created_at: "2026-11-20T09:00:00.000001Z" }),
      ],
      next_cursor: null,
    });
    expect(ids(state.history)).toEqual(["c", "b", "a"]);
    expect(ids(state.active)).toEqual(["x", "y"]);
  });

  it("never inserts an unknown ID when insert is false", () => {
    const result = applySnapshot(EMPTY_QUEUE_LISTS, job("u"), { rich: false, insert: false });
    expect(result.known).toBe(false);
    expect(result.lists).toBe(EMPTY_QUEUE_LISTS);
  });

  it("removeJob drops the row from either list", () => {
    expect(removeJob(lists([job("a")], []), "a").active).toEqual([]);
  });
});

describe("reconcile", () => {
  const h1 = job("h1", { status: "completed", created_at: "2026-11-20T09:00:03Z" });
  const h2 = job("h2", { status: "completed", created_at: "2026-11-20T09:00:02Z" });
  const deep = job("deep", { status: "failed", created_at: "2026-11-20T09:00:01Z", error: { code: "x", message: "m", retryable: true } });

  it("reset replaces history with exactly the head and cursor and drops deeper pages", () => {
    const local = lists([], [h1, h2, deep], null);
    const result = applyReset(local, [], { jobs: [h1, h2], next_cursor: "c2" });
    expect(ids(result.lists.history)).toEqual(["h1", "h2"]);
    expect(result.lists.nextCursor).toBe("c2");
    expect(result.reset).toBe(true);
  });

  it("reset applies newest-wins: a newer local SSE snapshot beats a stale head row", () => {
    const retried = { ...h1, status: "queued" as const, updated_at: "2026-11-20T12:00:00Z" };
    const result = applyReset(lists([retried], [h2]), [], { jobs: [h1, h2], next_cursor: null });
    expect(ids(result.lists.active)).toEqual(["h1"]);
    expect(ids(result.lists.history)).toEqual(["h2"]);
    // Present in the fresh head, so no extra GET is needed.
    expect(result.missingActiveIds).toEqual([]);
  });

  it("reset drops a newer local terminal outside the head instead of adding it to history", () => {
    const stale = job("a", { status: "downloading", updated_at: "2026-11-20T10:00:05Z" });
    const local = job("a", { status: "failed", updated_at: "2026-11-20T10:00:08Z" });
    const result = applyReset(lists([], [local, h1]), [stale], { jobs: [h1, h2], next_cursor: "c2" });
    expect(result.lists.active).toEqual([]);
    expect(ids(result.lists.history)).toEqual(["h1", "h2"]);
    expect(result.lists.nextCursor).toBe("c2");
    expect(result.missingActiveIds).toEqual([]);
  });

  it("read snapshots keep reset history membership unless a current merge allows the move", () => {
    const state = lists([job("a", { status: "downloading" })], [h1], "c2");
    const terminal = job("a", { status: "completed", updated_at: "2026-11-20T11:00:00Z" });
    const reset = applyReadSnapshot(state, terminal, false);
    expect(reset.active).toEqual([]);
    expect(ids(reset.history)).toEqual(["h1"]);
    expect(reset.nextCursor).toBe("c2");
    const merged = applyReadSnapshot(state, terminal, true);
    expect(ids(merged.history)).toEqual(["a", "h1"]);
    // Older snapshots lose; active snapshots and unknown IDs follow rule 1.
    expect(applyReadSnapshot(state, { ...terminal, updated_at: "2026-11-20T09:00:00Z" }, false)).toBe(state);
    expect(ids(applyReadSnapshot(state, job("a", { status: "downloading", updated_at: "2026-11-20T11:00:00Z" }), false).active)).toEqual(["a"]);
    expect(applyReadSnapshot(state, job("zz", { status: "completed" }), true)).toBe(state);
    const updated = applyReadSnapshot(state, { ...h1, title: "Fresh", updated_at: "2026-11-20T11:00:00Z" }, false);
    expect(updated.history[0].title).toBe("Fresh");
  });

  it("reports locally active IDs missing from both fresh responses", () => {
    const result = applyReset(lists([job("gone")], []), [], { jobs: [], next_cursor: null });
    expect(result.missingActiveIds).toEqual(["gone"]);
    expect(ids(result.lists.active)).toEqual(["gone"]);
  });

  it("merge keeps loaded pages and the cursor when the head last row is loaded", () => {
    const fresh = job("new", { status: "completed", created_at: "2026-11-20T09:00:04Z" });
    const result = applyMerge(lists([], [h1, h2, deep], "c3"), [], { jobs: [fresh, h1], next_cursor: "cx" });
    expect(result.reset).toBe(false);
    expect(ids(result.lists.history)).toEqual(["new", "h1", "h2", "deep"]);
    expect(result.lists.nextCursor).toBe("c3");
  });

  it("merge with a possible gap resets to the fresh head", () => {
    const n1 = job("n1", { status: "completed", created_at: "2026-11-20T09:00:06Z" });
    const n2 = job("n2", { status: "completed", created_at: "2026-11-20T09:00:05Z" });
    const result = applyMerge(lists([], [h1, h2, deep], "c3"), [], { jobs: [n1, n2], next_cursor: "cn" });
    expect(result.reset).toBe(true);
    expect(ids(result.lists.history)).toEqual(["n1", "n2"]);
    expect(result.lists.nextCursor).toBe("cn");
  });

  it("merge without a head cursor never resets", () => {
    const result = applyMerge(lists([], [], null), [job("a")], { jobs: [h1], next_cursor: null });
    expect(result.reset).toBe(false);
    expect(ids(result.lists.active)).toEqual(["a"]);
    expect(ids(result.lists.history)).toEqual(["h1"]);
  });

  it("history page dedups, keeps newest-wins and takes the new cursor", () => {
    const newer = { ...h2, title: "Newer", updated_at: "2026-11-20T12:00:00Z" };
    const result = applyHistoryPage(lists([], [h1, newer], "c2"), { jobs: [h2, deep], next_cursor: null });
    expect(ids(result.history)).toEqual(["h1", "h2", "deep"]);
    expect(result.history[1].title).toBe("Newer");
    expect(result.nextCursor).toBeNull();
  });

  describe("several open-detail priors", () => {
    const newer = (base: Job) => ({ ...base, status: "completed" as const, error: null, updated_at: "2026-11-20T12:00:00Z" });
    const f = job("f", { status: "failed", created_at: "2026-11-20T09:00:00.5Z", error: { code: "x", message: "m", retryable: true } });
    const g = job("g", { status: "failed", created_at: "2026-11-20T09:00:00.4Z", error: { code: "x", message: "m", retryable: true } });
    const ghost = job("ghost", { status: "completed", updated_at: "2026-11-20T12:00:00Z" });
    const priors = [newer(f), newer(g), ghost];

    it("reset compares every prior and never adds a prior-only ID", () => {
      const result = applyReset(lists([], [h1]), [], { jobs: [h1, f, g], next_cursor: null }, priors);
      expect(ids(result.lists.history)).toEqual(["h1", "f", "g"]);
      expect(result.lists.history.slice(1).map((entry) => entry.status)).toEqual(["completed", "completed"]);
      expect(ids([...result.lists.active, ...result.lists.history])).not.toContain("ghost");
    });

    it("merge aligns held rows and fresh rows to every newer prior without inserting others", () => {
      const result = applyMerge(lists([], [h1, f]), [], { jobs: [h1, g], next_cursor: null }, priors);
      expect(result.reset).toBe(false);
      expect(ids(result.lists.history)).toEqual(["h1", "f", "g"]);
      expect(result.lists.history.slice(1).every((entry) => entry.status === "completed")).toBe(true);
      expect(ids(result.lists.history)).not.toContain("ghost");
    });

    it("history page keeps every newer prior and drops no rows", () => {
      const result = applyHistoryPage(lists([], [h1], "c2"), { jobs: [f, g], next_cursor: null }, priors);
      expect(ids(result.history)).toEqual(["h1", "f", "g"]);
      expect(result.history.slice(1).every((entry) => entry.status === "completed")).toBe(true);
      expect(ids(result.history)).not.toContain("ghost");
    });

    it("an older prior never wins over a newer fresh row", () => {
      const fresh = { ...f, status: "queued" as const, error: null, updated_at: "2026-11-20T13:00:00Z" };
      const result = applyReset(EMPTY_QUEUE_LISTS, [fresh], { jobs: [], next_cursor: null }, priors);
      expect(ids(result.lists.active)).toEqual(["f"]);
    });
  });

  it("disjointness: a job in both fresh responses lands in one list", () => {
    const moved = { ...h1, status: "queued" as const, updated_at: "2026-11-20T12:00:00Z" };
    const result = applyReset(EMPTY_QUEUE_LISTS, [moved], { jobs: [h1], next_cursor: null });
    expect(ids(result.lists.active)).toEqual(["h1"]);
    expect(result.lists.history).toEqual([]);
  });
});
