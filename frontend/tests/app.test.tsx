import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/app/App";

class FakeEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  addEventListener() {}
  close() {}
}

const okCheck = {
  status: "completed",
  results: [
    { target: "staging", ok: true, reason: null },
    { target: "app_library", ok: true, reason: null },
  ],
};

const failedCheck = {
  status: "completed",
  results: [
    { target: "staging", ok: false, reason: "create_failed" },
    { target: "app_library", ok: true, reason: null },
  ],
};

function settingsBody(startupCheck: unknown) {
  return {
    max_concurrent_jobs: 2,
    default_destination: "app_library",
    conflict_policy: "skip",
    destinations: [
      { target: "app_library", configured: true, selectable: true, display_path: null },
      { target: "plex_music", configured: false, selectable: false, display_path: null },
      { target: "plex_video", configured: false, selectable: false, display_path: null },
    ],
    startup_storage_check: startupCheck,
  };
}

type SettingsStub = () => Promise<Response>;

const settingsOk: SettingsStub = async () =>
  new Response(JSON.stringify(settingsBody(okCheck)), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

function stubHealthOk(settings: SettingsStub = settingsOk) {
  // The Queue page subscribes to the single global SSE stream on mount.
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/settings") {
        return settings();
      }
      // Navigating to Library now loads the catalog list; keep the shell
      // tests on a valid empty first page instead of the health body.
      if (url === "/api/v1/library?limit=50&offset=0") {
        return new Response(
          JSON.stringify({ items: [], limit: 50, offset: 0, has_more: false }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url === "/api/v1/jobs?scope=active" || url === "/api/v1/jobs?scope=history") {
        return new Response(JSON.stringify({ jobs: [], next_cursor: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ status: "ok", database: "ok" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

describe("App shell", () => {
  it("renders primary navigation and the default page", async () => {
    stubHealthOk();
    render(<App />);

    expect(screen.getByRole("heading", { name: "New Download" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Queue" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Library" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
    expect(await screen.findByText("Backend online")).toBeInTheDocument();
  });

  it("marks the active navigation link with aria-current", async () => {
    stubHealthOk();
    render(<App />);

    expect(screen.getByRole("link", { name: "New Download" })).toHaveAttribute("aria-current", "page");

    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "New Download" })).not.toHaveAttribute("aria-current");
  });

  it("navigates between pages", async () => {
    stubHealthOk();
    render(<App />);

    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Queue" }));
    expect(screen.getByRole("heading", { name: "Queue" })).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Library" }));
    expect(screen.getByRole("heading", { name: "Library" })).toBeInTheDocument();
  });
});

const WARNING = "Storage write check failed at startup.";

describe("Startup storage warning", () => {
  it("stays visible on every page for a stored failed target, without dismiss", async () => {
    stubHealthOk(async () =>
      new Response(JSON.stringify(settingsBody(failedCheck)), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    render(<App />);
    const user = userEvent.setup();

    const warning = await screen.findByText(WARNING);
    const banner = warning.closest("[role='alert']");
    expect(banner).not.toBeNull();
    expect(banner).toHaveTextContent("Staging — could not create a test file");
    expect(banner).not.toHaveTextContent("App Library");
    expect(banner?.querySelector("button")).toBeNull();

    for (const page of ["Queue", "Library", "Settings", "New Download"]) {
      await user.click(screen.getByRole("link", { name: page }));
      expect(screen.getByRole("heading", { name: page, level: 1 })).toBeInTheDocument();
      expect(screen.getByText(WARNING)).toBeInTheDocument();
    }
  });

  it("keeps the seen warning on a read error and clears it on a healthy snapshot", async () => {
    let call = 0;
    const sequence: SettingsStub[] = [
      async () =>
        new Response(JSON.stringify(settingsBody(failedCheck)), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      () => Promise.reject(new TypeError("Failed to fetch")),
      settingsOk,
    ];
    stubHealthOk(() => sequence[Math.min(call++, sequence.length - 1)]());
    render(<App />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("link", { name: "New Download" }));
    expect(await screen.findByText(WARNING)).toBeInTheDocument();

    // Opening Settings re-reads the shared query; the network error is a
    // read error, not a new disk failure, and keeps the last valid warning.
    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(await screen.findByText(/Could not load settings\./)).toBeInTheDocument();
    expect(screen.getByText(WARNING)).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Queue" }));
    expect(screen.getByText(WARNING)).toBeInTheDocument();

    // A later healthy snapshot (normal re-read on opening Settings again)
    // removes the old warning everywhere.
    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(await screen.findAllByText("Writable at startup")).toHaveLength(2);
    expect(screen.queryByText(WARNING)).not.toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Library" }));
    expect(screen.queryByText(WARNING)).not.toBeInTheDocument();
    expect(call).toBe(3);
  });

  it.each([
    ["a successful check", settingsOk],
    [
      "a missing snapshot",
      async () =>
        new Response(JSON.stringify(settingsBody({ status: "unavailable", results: [] })), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    ],
    ["a network error", () => Promise.reject(new TypeError("Failed to fetch"))],
    ["a server error", async () => new Response("unavailable", { status: 503 })],
  ] satisfies [string, SettingsStub][])("is not shown for %s", async (_label, settings) => {
    stubHealthOk(settings);
    render(<App />);

    expect(await screen.findByText("Backend online")).toBeInTheDocument();
    await waitFor(() =>
      expect(vi.mocked(fetch).mock.calls.map(([input]) => String(input))).toContain(
        "/api/v1/settings",
      ),
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    const settled = await screen.findAllByText(
      /Writable at startup|No startup check result|Could not load settings/,
    );
    expect(settled.length).toBeGreaterThan(0);
    expect(screen.queryByText(WARNING)).not.toBeInTheDocument();
  });
});
