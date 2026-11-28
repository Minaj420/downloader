import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { StartupStorageWarning } from "../src/components/StartupStorageWarning";
import { SettingsPage } from "../src/pages/SettingsPage";

const FORMAT_NOTE =
  "Format and quality are remembered in your browser from your last choice; Original on first use.";

function settingsBody(overrides: Record<string, unknown> = {}) {
  return {
    max_concurrent_jobs: 3,
    default_destination: "app_library",
    conflict_policy: "skip",
    destinations: [
      { target: "app_library", configured: true, selectable: true, display_path: null },
      { target: "plex_music", configured: true, selectable: false, display_path: "/volume1/Music" },
      { target: "plex_video", configured: false, selectable: false, display_path: null },
    ],
    startup_storage_check: {
      status: "completed",
      results: [
        { target: "staging", ok: true, reason: null },
        { target: "app_library", ok: true, reason: null },
        { target: "plex_music", ok: true, reason: null },
      ],
    },
    ...overrides,
  };
}

function stubSettings(response: () => Promise<Response>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    expect(String(input)).toBe("/api/v1/settings");
    expect(init?.method ?? "GET").toBe("GET");
    return response();
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function jsonResponse(body: unknown, status = 200) {
  return async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
}

function renderWithClient(ui: ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
  return queryClient;
}

function renderPageAndWarning() {
  return renderWithClient(
    <>
      <StartupStorageWarning />
      <SettingsPage />
    </>,
  );
}

const failedCheck = {
  status: "completed",
  results: [
    { target: "staging", ok: true, reason: null },
    { target: "app_library", ok: false, reason: "write_failed" },
    { target: "plex_music", ok: false, reason: "root_unavailable" },
  ],
};

describe("SettingsPage", () => {
  it("shows effective read-only values, destinations and the startup check", async () => {
    const fetchMock = stubSettings(jsonResponse(settingsBody()));
    const queryClient = renderPageAndWarning();

    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading settings…");

    const downloads = await screen.findByRole("region", { name: "Downloads" });
    expect(within(downloads).getByText("Maximum concurrent jobs").nextSibling).toHaveTextContent("3");
    expect(within(downloads).getByText("Default destination").nextSibling).toHaveTextContent("App Library");
    expect(within(downloads).getByText("Existing files").nextSibling).toHaveTextContent("Skip Existing");
    expect(within(downloads).getByText(FORMAT_NOTE)).toBeInTheDocument();

    expect(screen.getByText(/Maximum concurrent jobs and the Plex destinations come from/)).toBeInTheDocument();
    expect(screen.getByText(/default destination and existing-file handling are fixed/)).toBeInTheDocument();
    for (const name of ["Destinations", "Startup storage check"]) {
      expect(within(screen.getByRole("region", { name })).getByRole("list")).toHaveAttribute("role", "list");
    }

    const destinations = within(screen.getByRole("region", { name: "Destinations" })).getAllByRole("listitem");
    expect(destinations.map((item) => item.textContent)).toEqual([
      "App LibraryConfigured",
      "Plex MusicConfigured — not selectable yet/volume1/Music",
      "Plex VideosNot configured",
    ]);

    const check = screen.getByRole("region", { name: "Startup storage check" });
    expect(within(check).getAllByText("Writable at startup")).toHaveLength(3);
    expect(within(check).getByText(/does not run a new check/)).toBeInTheDocument();

    // Nothing is editable and success shows no warning.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    // Shared single read, no polling.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const query = queryClient.getQueryCache().find({ queryKey: ["settings"] });
    expect(query?.observers.length).toBe(2);
    for (const observer of query?.observers ?? []) {
      expect(observer.options.refetchInterval).toBeUndefined();
    }
  });

  it("shows failed startup entries and the warning", async () => {
    stubSettings(jsonResponse(settingsBody({ startup_storage_check: failedCheck })));
    renderPageAndWarning();

    const check = await screen.findByRole("region", { name: "Startup storage check" });
    expect(within(check).getByText("Failed — could not write a test file")).toBeInTheDocument();
    expect(within(check).getByText("Failed — folder was unavailable")).toBeInTheDocument();
    expect(within(check).getByText("Writable at startup")).toBeInTheDocument();

    const warning = screen.getByRole("alert");
    expect(warning).toHaveTextContent("Storage write check failed at startup.");
    expect(within(warning).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "App Library — could not write a test file",
      "Plex Music — folder was unavailable",
    ]);
    expect(within(warning).queryByRole("button")).not.toBeInTheDocument();
  });

  it("keeps the last valid warning across a read error until a healthy snapshot", async () => {
    const responses = [
      jsonResponse(settingsBody({ startup_storage_check: failedCheck })),
      () => Promise.reject(new TypeError("Failed to fetch")),
      jsonResponse({ status: "ok", database: "ok" }),
      jsonResponse(settingsBody()),
    ];
    let call = 0;
    const fetchMock = stubSettings(() => responses[Math.min(call++, responses.length - 1)]());
    const queryClient = renderPageAndWarning();
    const warningText = "Storage write check failed at startup.";

    expect(await screen.findByText(warningText)).toBeInTheDocument();

    // Network error on the same shared query: read error shown, warning kept.
    await queryClient.refetchQueries({ queryKey: ["settings"] });
    expect(await screen.findByText(/Could not load settings\./)).toBeInTheDocument();
    expect(screen.getByText(warningText)).toBeInTheDocument();

    // Malformed body: still a read error, never a new or cleared disk warning.
    await queryClient.refetchQueries({ queryKey: ["settings"] });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(await screen.findByText(/not in the expected format/)).toBeInTheDocument();
    expect(screen.getByText(warningText)).toBeInTheDocument();

    // A real healthy snapshot replaces the old warning.
    await queryClient.refetchQueries({ queryKey: ["settings"] });
    expect(await screen.findAllByText("Writable at startup")).toHaveLength(3);
    expect(screen.queryByText(warningText)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("reports a missing startup snapshot as unavailable, not success or failure", async () => {
    stubSettings(
      jsonResponse(settingsBody({ startup_storage_check: { status: "unavailable", results: [] } })),
    );
    renderPageAndWarning();

    const check = await screen.findByRole("region", { name: "Startup storage check" });
    expect(within(check).getByText("No startup check result is available.")).toBeInTheDocument();
    expect(within(check).queryByText("Writable at startup")).not.toBeInTheDocument();
    expect(within(check).queryByText(/^Failed/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([
    ["a network error", () => Promise.reject(new TypeError("Failed to fetch"))],
    ["a server error", jsonResponse({ detail: "boom" }, 500)],
    ["a malformed body", jsonResponse({ status: "ok", database: "ok" })],
  ])("shows a load error without a disk warning on %s", async (_label, response) => {
    stubSettings(response);
    renderPageAndWarning();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load settings.");
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.queryByText("Storage write check failed at startup.")).not.toBeInTheDocument();
    expect(screen.queryByText("Writable at startup")).not.toBeInTheDocument();
  });
});
