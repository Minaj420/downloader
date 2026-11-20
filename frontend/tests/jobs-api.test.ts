import { ApiRequestError } from "../src/api/client";
import {
  cancelJob,
  getJob,
  listActiveJobs,
  listHistoryJobs,
  retryJob,
  type Job,
} from "../src/api/jobs";

function makeJob(id: string, overrides: Partial<Job> = {}): Job {
  return {
    id,
    analysis_id: "analysis-1",
    variant_id: "variant-1",
    status: "queued",
    title: `Job ${id}`,
    source_url: "https://example.com/watch",
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
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    started_at: null,
    completed_at: null,
    ...overrides,
  };
}

function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function requestedUrl(fetchMock: ReturnType<typeof stubFetch>): URL {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  return new URL(fetchMock.mock.calls[0][0], "http://localhost");
}

describe("listActiveJobs", () => {
  it("calls only scope=active with a GET and returns the envelope", async () => {
    const fetchMock = stubFetch({ jobs: [makeJob("a")], next_cursor: null });

    const result = await listActiveJobs();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/jobs?scope=active");
    const init = fetchMock.mock.calls[0][1];
    expect(init?.method).toBeUndefined();
    expect(init?.body).toBeUndefined();
    expect(result.jobs.map((job) => job.id)).toEqual(["a"]);
    expect(result.next_cursor).toBeNull();
  });

  it("keeps null collection items untouched", async () => {
    stubFetch({
      jobs: [makeJob("c", { job_type: "collection", items: null })],
      next_cursor: null,
    });

    const result = await listActiveJobs();

    expect(result.jobs[0].items).toBeNull();
  });
});

describe("listHistoryJobs", () => {
  it("sends only scope=history by default", async () => {
    const fetchMock = stubFetch({ jobs: [], next_cursor: null });

    await listHistoryJobs();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/jobs?scope=history");
    expect(fetchMock.mock.calls[0][1]?.method).toBeUndefined();
  });

  it("sends the limit as a string", async () => {
    const fetchMock = stubFetch({ jobs: [], next_cursor: null });

    await listHistoryJobs({ limit: 25 });

    const url = requestedUrl(fetchMock);
    expect(url.searchParams.get("scope")).toBe("history");
    expect(url.searchParams.get("limit")).toBe("25");
    expect(url.searchParams.has("cursor")).toBe(false);
  });

  it("round-trips an opaque cursor with special characters", async () => {
    const fetchMock = stubFetch({ jobs: [], next_cursor: null });
    const cursor = "ab-_=+/ &?#%é";

    await listHistoryJobs({ cursor, limit: 10 });

    const url = requestedUrl(fetchMock);
    expect(url.searchParams.get("cursor")).toBe(cursor);
    expect(url.searchParams.get("limit")).toBe("10");
    expect(url.searchParams.getAll("cursor")).toHaveLength(1);
  });

  it("omits a null cursor but forwards an empty one unchanged", async () => {
    const nullFetch = stubFetch({ jobs: [], next_cursor: null });
    await listHistoryJobs({ cursor: null });
    expect(requestedUrl(nullFetch).searchParams.has("cursor")).toBe(false);

    const emptyFetch = stubFetch({ jobs: [], next_cursor: null });
    await listHistoryJobs({ cursor: "" });
    const url = requestedUrl(emptyFetch);
    expect(url.searchParams.has("cursor")).toBe(true);
    expect(url.searchParams.get("cursor")).toBe("");
  });

  it("returns a non-null next_cursor verbatim", async () => {
    stubFetch({ jobs: [makeJob("h", { status: "completed" })], next_cursor: "Zm9v-_" });

    const result = await listHistoryJobs();

    expect(result.next_cursor).toBe("Zm9v-_");
    expect(result.jobs).toHaveLength(1);
  });

  it("preserves the backend 422 error fields", async () => {
    stubFetch(
      {
        error: {
          code: "invalid_request",
          message: "The request payload is invalid.",
          retryable: false,
          analysis_id: null,
        },
      },
      422,
    );

    const error = await listHistoryJobs({ limit: 0 }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiRequestError);
    const requestError = error as ApiRequestError;
    expect(requestError.status).toBe(422);
    expect(requestError.code).toBe("invalid_request");
    expect(requestError.message).toBe("The request payload is invalid.");
    expect(requestError.retryable).toBe(false);
    expect(requestError.analysisId).toBeNull();
  });
});

describe("existing job calls", () => {
  it("getJob reads the job by id", async () => {
    const fetchMock = stubFetch(makeJob("j1"));

    const job = await getJob("j1");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/jobs/j1");
    expect(job.id).toBe("j1");
  });

  it("cancelJob and retryJob post to their endpoints", async () => {
    const cancelFetch = stubFetch(makeJob("j2", { status: "cancelled" }));
    await cancelJob("j2");
    expect(cancelFetch.mock.calls[0][0]).toBe("/api/v1/jobs/j2/cancel");
    expect(cancelFetch.mock.calls[0][1]?.method).toBe("POST");

    const retryFetch = stubFetch(makeJob("j2"));
    await retryJob("j2");
    expect(retryFetch.mock.calls[0][0]).toBe("/api/v1/jobs/j2/retry");
    expect(retryFetch.mock.calls[0][1]?.method).toBe("POST");
  });
});
