import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { App } from "../src/app/App";
import { LibraryPage } from "../src/pages/LibraryPage";
import { deleteLibraryFile } from "../src/api/library";
import { ApiRequestError } from "../src/api/client";
import type {
  LibraryItem,
  LibraryListItem,
  LibraryListResult,
  LibraryMediaFile,
} from "../src/api/library";

/* ---- Fixtures mirroring the M3-LIB-2 contract ---- */

function mediaFile(overrides: Partial<LibraryMediaFile> = {}): LibraryMediaFile {
  return {
    id: 1,
    storage_type: "app_library",
    path: "video/Sample.mp4",
    display_path: "/library/video/Sample.mp4",
    size_bytes: 12_582_912,
    created_at: "2026-09-27T14:31:00Z",
    ...overrides,
  };
}

function listItem(overrides: Partial<LibraryListItem> = {}): LibraryListItem {
  return {
    id: "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
    job_id: "e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6",
    title: "Sample Video",
    media_type: "video",
    format: "mp4",
    created_at: "2026-09-27T14:31:00Z",
    media_files: [mediaFile()],
    ...overrides,
  };
}

function libraryItem(overrides: Partial<LibraryItem> = {}): LibraryItem {
  return {
    ...listItem(),
    source_url: "https://www.youtube.com/watch?v=abc123",
    source_id: "abc123",
    platform: "youtube",
    artist: null,
    album: null,
    codec: "h264",
    bitrate: 1500,
    resolution: "1080p",
    duration: 205.4,
    ...overrides,
  };
}

function page(
  items: LibraryListItem[],
  options: Partial<Pick<LibraryListResult, "has_more" | "offset">> = {},
): LibraryListResult {
  return { items, limit: 50, offset: 0, has_more: false, ...options };
}

/* ---- Test infrastructure ---- */

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

type RouteHandler = (
  url: string,
  init?: RequestInit,
) => Response | Promise<Response> | undefined;

function stubRoutes(route: RouteHandler) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/health") {
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok" }));
    }
    const handled = route(url, init);
    if (handled !== undefined) {
      return Promise.resolve(handled);
    }
    return Promise.reject(new Error(`Unexpected fetch: ${url}`));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function noContentResponse(): Response {
  return new Response(null, { status: 204 });
}

function errorResponse(message: string, status = 503): Response {
  return jsonResponse({
    error: { code: "internal_error", message, retryable: true },
  }, status);
}

async function openLibraryPage() {
  render(<App />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("link", { name: "Library" }));
  return user;
}

/* ---- List states ---- */

describe("LibraryPage list", () => {
  it("shows an initial loading state, then renders the page", async () => {
    const pending = deferred<Response>();
    stubRoutes((url) => {
      if (url === "/api/v1/library?limit=50&offset=0") return pending.promise;
      return undefined;
    });
    await openLibraryPage();

    expect(screen.getByText("Loading library…")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();

    await act(async () => {
      pending.resolve(jsonResponse(page([listItem()])));
    });

    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Sample Video" }),
    ).toBeInTheDocument();
  });

  it("shows an accessible empty state", async () => {
    stubRoutes((url) =>
      url === "/api/v1/library?limit=50&offset=0"
        ? jsonResponse(page([]))
        : undefined,
    );
    await openLibraryPage();

    expect(
      await screen.findByText("No downloads in the Library yet."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Previous" }),
    ).not.toBeInTheDocument();
  });

  it("maps list fields to the spec columns in API order", async () => {
    const newest = listItem({
      id: "newest-item",
      title: "Newest Track",
      media_type: "audio",
      format: "m4a",
      created_at: "2026-09-28T10:05:00Z",
      media_files: [
        mediaFile({
          size_bytes: 4_194_304,
          path: "audio/Newest.m4a",
          display_path: "/library/audio/Newest.m4a",
        }),
      ],
    });
    const older = listItem({
      id: "older-item",
      title: "Older Video",
      media_type: "video",
      format: "webm",
      created_at: "2026-09-27T09:00:00Z",
      media_files: [
        mediaFile({
          id: 2,
          storage_type: "plex_video",
          size_bytes: 12_582_912,
          path: "Older.webm",
          display_path: "/plex/videos/Older.webm",
        }),
      ],
    });
    stubRoutes((url) =>
      url === "/api/v1/library?limit=50&offset=0"
        ? jsonResponse(page([newest, older]))
        : undefined,
    );
    await openLibraryPage();

    // Wait for this test's own page before reading the table: the app-level
    // QueryClient is shared across tests, so a briefly cached previous page
    // must not leak into these assertions.
    await screen.findByRole("button", { name: "Newest Track" });
    const table = screen.getByRole("table");
    const headers = within(table).getAllByRole("columnheader");
    expect(headers.map((header) => header.textContent)).toEqual([
      "Name",
      "Type",
      "Size",
      "Downloaded At",
      "Format",
      "Location",
      "Path",
    ]);

    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3); // header + two items, newest-first API order
    expect(rows[1]).toHaveTextContent("Newest Track");
    expect(rows[1]).toHaveTextContent("Audio");
    expect(rows[1]).toHaveTextContent("4.0 MiB");
    expect(rows[1]).toHaveTextContent("28 Sep 2026, 10:05 UTC");
    expect(rows[1]).toHaveTextContent("m4a");
    expect(rows[1]).toHaveTextContent("App Library");
    expect(rows[1]).toHaveTextContent("/library/audio/Newest.m4a");

    expect(rows[2]).toHaveTextContent("Older Video");
    expect(rows[2]).toHaveTextContent("Video");
    expect(rows[2]).toHaveTextContent("12.0 MiB");
    expect(rows[2]).toHaveTextContent("27 Sep 2026, 09:00 UTC");
    expect(rows[2]).toHaveTextContent("webm");
    expect(rows[2]).toHaveTextContent("Plex Videos");
    expect(rows[2]).toHaveTextContent("/plex/videos/Older.webm");

    // Every data cell carries its field name as a REAL DOM label element
    // (not pseudo-content), naming the fields in the mobile card layout
    // while the column headers keep naming them in the desktop table.
    const valueCells = within(rows[1]).getAllByRole("cell");
    expect(valueCells).toHaveLength(6);
    const cellLabels = valueCells.map(
      (cell) =>
        within(cell).getByText(/^(Type|Size|Downloaded At|Format|Location|Path)$/)
          .textContent,
    );
    expect(cellLabels).toEqual([
      "Type",
      "Size",
      "Downloaded At",
      "Format",
      "Location",
      "Path",
    ]);

    // Single-copy grid invariant: the item-level Name header is a rowgroup
    // header spanning exactly one row, Type/Downloaded At/Format span the
    // same single row, and the copy's Size/Location/Path are normal cells.
    const nameHeader = within(rows[1]).getByRole("rowheader");
    expect(nameHeader).toHaveAttribute("scope", "rowgroup");
    expect(nameHeader).toHaveAttribute("rowspan", "1");
    expect(valueCells[0]).toHaveAttribute("rowspan", "1"); // Type
    expect(valueCells[2]).toHaveAttribute("rowspan", "1"); // Downloaded At
    expect(valueCells[3]).toHaveAttribute("rowspan", "1"); // Format
    expect(valueCells[1]).not.toHaveAttribute("rowspan"); // Size
    expect(valueCells[4]).not.toHaveAttribute("rowspan"); // Location
    expect(valueCells[5]).not.toHaveAttribute("rowspan"); // Path
  });

  it("groups each physical copy's fields in one semantic row and renders copy-less items safely", async () => {
    const longPath = `/plex/music/${"very-long-directory-name-".repeat(8)}x.flac`;
    const dualCopy = listItem({
      title: "Dual Copy Album",
      media_files: [
        mediaFile({
          id: 11,
          storage_type: "app_library",
          size_bytes: 4_194_304,
          path: "a/x.flac",
          display_path: "/library/a/x.flac",
        }),
        mediaFile({
          id: 22,
          storage_type: "plex_music",
          size_bytes: 8_388_608,
          path: "x.flac",
          display_path: longPath,
        }),
      ],
    });
    const noCopies = listItem({ title: "Catalog Only Item", media_files: [] });
    stubRoutes((url) =>
      url === "/api/v1/library?limit=50&offset=0"
        ? jsonResponse(page([dualCopy, noCopies]))
        : undefined,
    );
    await openLibraryPage();

    await screen.findByRole("button", { name: "Dual Copy Album" });
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row");
    // header + item row + second-copy sub-row + copy-less item row
    expect(rows).toHaveLength(4);

    // First copy stays on the item row in stable API order.
    expect(rows[1]).toHaveTextContent("Dual Copy Album");
    expect(rows[1]).toHaveTextContent("4.0 MiB");
    expect(rows[1]).toHaveTextContent("App Library");
    expect(rows[1]).toHaveTextContent("/library/a/x.flac");

    // Multi-copy grid invariant: the rowgroup Name header and the item
    // fields Type/Downloaded At/Format span both rows (rowCount=2), while
    // Size/Location/Path stay per-row cells of their own copy.
    const nameHeader = within(rows[1]).getByRole("rowheader");
    expect(nameHeader).toHaveAttribute("scope", "rowgroup");
    expect(nameHeader).toHaveAttribute("rowspan", "2");
    const itemCells = within(rows[1]).getAllByRole("cell");
    expect(itemCells).toHaveLength(6);
    expect(itemCells[0]).toHaveAttribute("rowspan", "2"); // Type
    expect(itemCells[2]).toHaveAttribute("rowspan", "2"); // Downloaded At
    expect(itemCells[3]).toHaveAttribute("rowspan", "2"); // Format
    expect(itemCells[1]).not.toHaveAttribute("rowspan"); // Size (copy 1)
    expect(itemCells[4]).not.toHaveAttribute("rowspan"); // Location (copy 1)
    expect(itemCells[5]).not.toHaveAttribute("rowspan"); // Path (copy 1)

    // The second physical copy's row carries exactly the Size/Location/Path
    // cells (real grid columns 3/6/7) — the correspondence is DOM
    // structure, so a wrapping long path cannot detach it from its copy.
    const copyRow = rows[2];
    const copyCells = within(copyRow).getAllByRole("cell");
    expect(copyCells).toHaveLength(3);
    expect(within(copyCells[0]).getByText("Size")).toBeInTheDocument();
    expect(within(copyCells[1]).getByText("Location")).toBeInTheDocument();
    expect(within(copyCells[2]).getByText("Path")).toBeInTheDocument();
    for (const cell of copyCells) {
      expect(cell).not.toHaveAttribute("rowspan");
    }
    expect(copyRow).toHaveTextContent("8.0 MiB");
    expect(copyRow).toHaveTextContent("Plex Music");
    expect(within(copyRow).getByText(longPath)).toBeInTheDocument();
    expect(copyRow).not.toHaveTextContent("4.0 MiB");
    expect(copyRow).not.toHaveTextContent("App Library");
    expect(copyRow).not.toHaveTextContent("Dual Copy Album");

    // A catalog item without physical copies renders safely: single-row
    // rowgroup (rowspan 1), explicit placeholders, no filler copy row.
    expect(rows[3]).toHaveTextContent("Catalog Only Item");
    const noCopyHeader = within(rows[3]).getByRole("rowheader");
    expect(noCopyHeader).toHaveAttribute("rowspan", "1");
    const noCopyCells = within(rows[3]).getAllByRole("cell");
    expect(noCopyCells).toHaveLength(6);
    expect(noCopyCells.map((cell) => cell.textContent)).toEqual([
      "TypeVideo",
      "Size—",
      "Downloaded At27 Sep 2026, 14:31 UTC",
      "Formatmp4",
      "Location—",
      "Path—",
    ]);
  });

  it("surfaces API list errors in an alert and retries with a real refetch", async () => {
    let listRequests = 0;
    stubRoutes((url) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        listRequests += 1;
        return listRequests === 1
          ? jsonResponse(
              {
                error: {
                  code: "internal_error",
                  message: "Library temporarily unavailable",
                  retryable: true,
                },
              },
              503,
            )
          : jsonResponse(page([listItem()]));
      }
      return undefined;
    });
    const user = await openLibraryPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Library temporarily unavailable");
    expect(
      screen.queryByText("No downloads in the Library yet."),
    ).not.toBeInTheDocument();

    await user.click(within(alert).getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByRole("button", { name: "Sample Video" }),
    ).toBeInTheDocument();
    expect(listRequests).toBe(2);
  });

  it("disables Retry while refetching, ignores duplicate clicks, and focuses the page heading on recovery", async () => {
    const pendingRetry = deferred<Response>();
    let listRequests = 0;
    stubRoutes((url) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        listRequests += 1;
        return listRequests === 1
          ? jsonResponse(
              {
                error: {
                  code: "internal_error",
                  message: "Library unavailable",
                  retryable: true,
                },
              },
              503,
            )
          : pendingRetry.promise;
      }
      return undefined;
    });
    const user = await openLibraryPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Library unavailable");
    expect(
      screen.getByRole("heading", { name: "Page 1 failed to load" }),
    ).toBeInTheDocument();

    const retry = within(alert).getByRole("button", { name: "Retry" });
    await user.click(retry);

    // While the retry refetch is in flight the button sits disabled, so a
    // duplicate click fires no extra request.
    expect(retry).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(listRequests).toBe(2);

    await act(async () => {
      pendingRetry.resolve(
        jsonResponse(page([listItem({ title: "After Retry" })])),
      );
    });

    expect(
      await screen.findByRole("button", { name: "After Retry" }),
    ).toBeInTheDocument();
    // The recovered page moves focus to the page status heading.
    expect(screen.getByRole("heading", { name: "Page 1" })).toHaveFocus();
  });
});

describe("Delete File client", () => {
  it("encodes the item id, sends only the copy id, and never parses 204", async () => {
    const response = noContentResponse();
    const parse = vi.spyOn(response, "json");
    const fetchMock = stubRoutes(() => response);
    await expect(deleteLibraryFile("item/with space?", 42)).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/library/item%2Fwith%20space%3F/media-files/42", {
      method: "DELETE", headers: { Accept: "application/json" },
    });
    expect(parse).not.toHaveBeenCalled();
  });

  it("preserves structured error message, code and retryability", async () => {
    stubRoutes(() => jsonResponse({ error: { code: "media_file_shared", message: "Another entry references this file.", retryable: false } }, 409));
    const error = await deleteLibraryFile("item", 42).catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiRequestError);
    expect(error).toMatchObject({ status: 409, code: "media_file_shared", message: "Another entry references this file.", retryable: false });
  });
});

describe("LibraryPage Delete File", () => {
  function reconciliationSetup(id: string, copies: LibraryMediaFile[]) {
    let current: LibraryItem | null = libraryItem({ id, title: id, media_files: copies });
    let deleteResponse: () => Response | Promise<Response> = () => noContentResponse();
    let readResponse: (() => Response | Promise<Response>) | null = null;
    let deletes = 0;
    const fetchMock = stubRoutes((url, init) => {
      if (init?.method === "DELETE") {
        deletes += 1;
        return Promise.resolve(deleteResponse()).then((response) => {
          if (response.ok && current) {
            const remaining = current.media_files.filter((copy) => copy.id !== 1);
            current = remaining.length ? { ...current, media_files: remaining, artist: "Authoritative artist" } : null;
          }
          return response;
        });
      }
      if (url.includes("?limit=50")) return jsonResponse(page(current ? [current] : [], { has_more: true }));
      if (url === `/api/v1/library/${id}`) return readResponse?.() ?? (current ? jsonResponse(current) : errorResponse("Not found", 404));
      return undefined;
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const otherPageKey = ["library", "list", { limit: 50, offset: 50 }];
    client.setQueryData(otherPageKey, page([current!], { offset: 50 }));
    const view = render(<QueryClientProvider client={client}><LibraryPage /></QueryClientProvider>);
    return {
      client, otherPageKey, fetchMock, view, deletes: () => deletes,
      setCopies: (next: LibraryMediaFile[]) => { current = { ...current!, media_files: next }; },
      setDeleteResponse: (next: () => Response | Promise<Response>) => { deleteResponse = next; },
      setReadResponse: (next: (() => Response | Promise<Response>) | null) => { readResponse = next; },
    };
  }

  async function confirmTarget(id: string) {
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: id }));
    await user.click(await screen.findByRole("button", { name: "Delete File: App Library — /library/video/Sample.mp4" }));
    return user;
  }

  it.each([false, true])("updates every shared cache without mounted observers, while another QueryClient stays isolated (last copy=%s)", async (lastCopy) => {
    const id = `unmounted-cache-${lastCopy}`;
    const second = mediaFile({ id: 2, display_path: "/unmounted-survivor.mp4" });
    const state = reconciliationSetup(id, lastCopy ? [mediaFile()] : [mediaFile(), second]);
    const pending = deferred<Response>();
    state.setDeleteResponse(() => pending.promise);
    const user = await confirmTarget(id);
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    await screen.findByText("Deleting selected file…");
    state.view.unmount();
    const isolated = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={isolated}><LibraryPage /></QueryClientProvider>);
    const select = await screen.findByRole("button", { name: id });
    expect(select).toBeEnabled();
    await user.click(select);
    expect(await screen.findByRole("button", { name: "Remove from Library" })).toBeEnabled();
    await act(async () => { pending.resolve(noContentResponse()); });
    await waitFor(() => expect(state.client.isMutating({ mutationKey: ["library", "operation"] })).toBe(0));
    expect(state.client.getQueryData<LibraryListResult>(state.otherPageKey)?.items.flatMap((item) => item.media_files.map((copy) => copy.id))).toEqual(lastCopy ? [] : [2]);
    const detail = state.client.getQueryData<LibraryItem>(["library", "detail", id]);
    if (lastCopy) expect(detail).toBeUndefined();
    else expect(detail).toMatchObject({ artist: "Authoritative artist", media_files: [second] });
    expect(isolated.getQueryData<LibraryItem>(["library", "detail", id])?.media_files[0].id).toBe(1);
    expect(screen.queryByText(/Deleted file for/)).not.toBeInTheDocument();
    expect(state.deletes()).toBe(1);
    state.client.clear();
    isolated.clear();
  });

  it.each([1, 2])("reconciles changed copy counts after confirmation opens (%s initial copies) across every cached page", async (count) => {
    const second = mediaFile({ id: 2, display_path: "/new-copy.mp4" });
    const id = `changed-count-${count}`;
    const state = reconciliationSetup(id, count === 1 ? [mediaFile()] : [mediaFile(), second]);
    const user = await confirmTarget(id);
    state.setCopies(count === 1 ? [mediaFile(), second] : [mediaFile()]);
    // Background detail refresh while confirmation is open changes 1→2 or
    // 2→1. The target identity stays fixed, not the consequence snapshot.
    await act(async () => { await state.client.refetchQueries({ queryKey: ["library", "detail", id] }); });
    const confirmation = screen.getByRole("region", { name: "Confirm Delete File" });
    expect(confirmation).toHaveTextContent("If this is the last copy when deleted");
    expect(confirmation).not.toHaveTextContent("Other copies and the Library item will remain");
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    await screen.findByText(count === 1 ? /Other copies remain in the Library/ : /Library item is no longer present/);
    const cached = state.client.getQueryData<LibraryListResult>(state.otherPageKey);
    if (count === 1) {
      expect(cached?.items[0].media_files.map((copy) => copy.id)).toEqual([2]);
      expect(screen.getByLabelText("Library item details")).toHaveTextContent("Authoritative artist");
      expect(state.client.getQueryData<LibraryItem>(["library", "detail", id])?.artist).toBe("Authoritative artist");
      expect(screen.getByRole("button", { name: id })).toBeInTheDocument();
    } else {
      expect(cached?.items).toEqual([]);
      expect(state.client.getQueryData(["library", "detail", id])).toBeUndefined();
      expect(screen.queryByLabelText("Library item details")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: id })).not.toBeInTheDocument();
    }
    expect(state.deletes()).toBe(1);
    state.client.clear();
  });

  it("keeps DELETE success truthful during deferred/failed reconciliation and retries only GET with duplicate/navigation guards", async () => {
    const id = "refresh-failure-item";
    const state = reconciliationSetup(id, [mediaFile()]);
    const user = await confirmTarget(id);
    const pending = deferred<Response>();
    state.setReadResponse(() => pending.promise);
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    const notice = await screen.findByText(/Deleted file for “refresh-failure-item”/);
    await screen.findByText("Refreshing Library state…");
    expect(notice).not.toHaveTextContent(/last copy|other copies|no longer present/i);
    expect(screen.queryByText("Deleting selected file…")).not.toBeInTheDocument();
    expect(screen.queryByText("/library/video/Sample.mp4")).not.toBeInTheDocument();
    // Even a sole copy initially only loses its copy, not its entire row.
    expect(state.client.getQueryData<LibraryListResult>(state.otherPageKey)?.items[0].media_files).toEqual([]);
    for (const name of [id, "Next", "Remove from Library"]) {
      const button = screen.getByRole("button", { name });
      expect(button).toBeDisabled();
      fireEvent.click(button);
    }
    await act(async () => { pending.resolve(errorResponse("Read unavailable")); });
    expect(await screen.findByRole("alert")).toHaveTextContent("The file was deleted, but the current Library state could not be verified");
    expect(screen.queryByText(/Could not complete Delete File/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry Delete File" })).not.toBeInTheDocument();
    const retryRead = deferred<Response>();
    state.setReadResponse(() => retryRead.promise);
    const retry = screen.getByRole("button", { name: "Retry Library refresh" });
    act(() => { fireEvent.click(retry); fireEvent.click(retry); });
    await screen.findByText("Refreshing Library state…");
    expect(state.deletes()).toBe(1);
    expect(state.fetchMock.mock.calls.filter(([url]) => url === `/api/v1/library/${id}`)).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await act(async () => { retryRead.resolve(errorResponse("Not found", 404)); });
    await screen.findByText(/Library item is no longer present/);
    expect(state.deletes()).toBe(1);
    expect(screen.queryByLabelText("Library item details")).not.toBeInTheDocument();
    state.client.clear();
  });

  it("uses neutral retry confirmation after a retryable DELETE error and changed detail, preserving explicit target confirmation", async () => {
    const id = "retry-changed-detail";
    const second = mediaFile({ id: 2, display_path: "/retry-other.mp4" });
    const state = reconciliationSetup(id, [mediaFile()]);
    state.setDeleteResponse(() => errorResponse("Busy"));
    const user = await confirmTarget(id);
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    await screen.findByRole("button", { name: "Retry Delete File" });
    state.setCopies([mediaFile(), second]);
    await act(async () => { await state.client.refetchQueries({ queryKey: ["library", "detail", id] }); });
    await user.click(screen.getByRole("button", { name: "Retry Delete File" }));
    const confirmation = screen.getByRole("region", { name: "Confirm Delete File" });
    expect(confirmation).toHaveTextContent("If this is the last copy when deleted");
    expect(confirmation).not.toHaveTextContent("This is the last copy:");
    expect(confirmation).toHaveTextContent("App Library — /library/video/Sample.mp4");
    expect(state.deletes()).toBe(1);
    state.setDeleteResponse(noContentResponse);
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    await screen.findByText(/Other copies remain in the Library/);
    expect(state.deletes()).toBe(2);
    expect(screen.getByLabelText("Library item details")).toHaveTextContent("/retry-other.mp4");
    state.client.clear();
  });

  it("shares the reconciliation lock after remount and cannot overwrite a newer query-owned read", async () => {
    const id = "navigated-refresh-item";
    const second = mediaFile({ id: 2, display_path: "/surviving-copy.mp4" });
    const state = reconciliationSetup(id, [mediaFile(), second]);
    const user = await confirmTarget(id);
    const oldRead = deferred<Response>();
    state.setReadResponse(() => oldRead.promise);
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    await screen.findByText("Refreshing Library state…");
    state.view.unmount();
    state.setReadResponse(null);
    render(<QueryClientProvider client={state.client}><LibraryPage /></QueryClientProvider>);
    const select = await screen.findByRole("button", { name: id });
    expect(select).toBeDisabled();
    expect(screen.getByText("A Library operation is in progress…")).toBeInTheDocument();
    // A newer explicit read cancels the older query generation. Page remount
    // alone must no longer unlock destructive actions while GET is pending.
    await act(async () => {
      await state.client.cancelQueries({ queryKey: ["library", "detail", id] });
      await state.client.fetchQuery({ queryKey: ["library", "detail", id], queryFn: async () => {
        return libraryItem({ id, title: id, artist: "Newer authoritative artist", media_files: [second] });
      } });
    });
    await act(async () => { oldRead.resolve(errorResponse("Not found", 404)); });
    await waitFor(() => expect(select).toBeEnabled());
    await user.click(select);
    await screen.findByText("Authoritative artist");
    expect(screen.getByLabelText("Library item details")).toHaveTextContent("/surviving-copy.mp4");
    expect(state.client.getQueryData<LibraryItem>(["library", "detail", id])?.media_files.map((copy) => copy.id)).toEqual([2]);
    expect(screen.queryByText(/Library item is no longer present/)).not.toBeInTheDocument();
    expect(state.deletes()).toBe(1);
    state.client.clear();
  });

  it.each([false, true])("reconciles committed DELETE across real App route exit/return before commit (last copy=%s)", async (lastCopy) => {
    const id = `router-pending-delete-${lastCopy}`;
    const second = mediaFile({ id: 2, display_path: "/router-survivor.mp4" });
    const original = libraryItem({ id, title: id, media_files: lastCopy ? [mediaFile()] : [mediaFile(), second] });
    const remaining = { ...original, artist: "Router authoritative artist", media_files: [second] };
    const pendingDelete = deferred<Response>();
    const precommitList = deferred<Response>();
    const pendingReconciliation = deferred<Response>();
    let committed = false;
    let lists = 0;
    let deletes = 0;
    let reconcileReads = 0;
    stubRoutes((url, init) => {
      if (init?.method === "DELETE") {
        deletes += 1;
        return pendingDelete.promise.then((response) => { committed = response.ok; return response; });
      }
      if (url.includes("?limit=50")) {
        lists += 1;
        if (lists === 2) return precommitList.promise;
        return jsonResponse(page(committed ? lastCopy ? [] : [remaining] : [original], { has_more: true }));
      }
      if (url === `/api/v1/library/${id}`) {
        if (committed) { reconcileReads += 1; return pendingReconciliation.promise.then((response) => response.clone()); }
        return jsonResponse(original);
      }
      return undefined;
    });
    const user = await openLibraryPage();
    await user.click(await screen.findByRole("button", { name: id }));
    await screen.findByRole("button", { name: "Delete File: App Library — /library/video/Sample.mp4" });
    await user.click(screen.getByRole("button", { name: "Delete File: App Library — /library/video/Sample.mp4" }));
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    await screen.findByText("Deleting selected file…");
    await user.click(screen.getByRole("link", { name: "New Download" }));
    await user.click(screen.getByRole("link", { name: "Library" }));
    await waitFor(() => expect(lists).toBe(2));
    await act(async () => { precommitList.resolve(jsonResponse(page([original], { has_more: true }))); });
    const button = await screen.findByRole("button", { name: id });
    for (const control of [button, screen.getByRole("button", { name: "Next" })]) {
      expect(control).toBeDisabled();
      fireEvent.click(control);
    }
    expect(screen.queryByLabelText("Library item details")).not.toBeInTheDocument();
    expect(screen.getByText("A Library operation is in progress…")).toBeInTheDocument();
    expect(deletes).toBe(1);
    await act(async () => { pendingDelete.resolve(noContentResponse()); });
    await waitFor(() => expect(reconcileReads).toBe(1));
    // Also leave/return after the irreversible commit while its GET is pending.
    await user.click(screen.getByRole("link", { name: "New Download" }));
    await user.click(screen.getByRole("link", { name: "Library" }));
    expect(await screen.findByText("A Library operation is in progress…")).toBeInTheDocument();
    expect(screen.queryByText("/library/video/Sample.mp4")).not.toBeInTheDocument();
    await act(async () => { pendingReconciliation.resolve(lastCopy ? errorResponse("Gone", 404) : jsonResponse(remaining)); });
    await waitFor(() => expect(screen.queryByText("A Library operation is in progress…")).not.toBeInTheDocument());
    expect(reconcileReads).toBe(1);
    expect(screen.queryByText("/library/video/Sample.mp4")).not.toBeInTheDocument();
    if (lastCopy) {
      expect(screen.queryByRole("button", { name: id })).not.toBeInTheDocument();
    } else {
      await user.click(screen.getByRole("button", { name: id }));
      expect(await screen.findByLabelText("Library item details")).toHaveTextContent("Router authoritative artist");
      expect(screen.getByRole("button", { name: "Remove from Library" })).toBeEnabled();
    }
    expect(deletes).toBe(1);
  });

  function setup(options: {
    id: string;
    copies?: LibraryMediaFile[];
    response?: () => Response | Promise<Response>;
    refreshedList?: Promise<Response>;
  }) {
    const original = libraryItem({ id: options.id, title: options.id, media_files: options.copies ?? [mediaFile()] });
    let current = original;
    let removed = false;
    let deletes = 0;
    const fetchMock = stubRoutes((url, init) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        if (removed && options.refreshedList) return options.refreshedList;
        return jsonResponse(page(current.media_files.length ? [current, listItem({ id: "untouched", title: "Untouched Item" })] : [listItem({ id: "untouched", title: "Untouched Item" })], { has_more: true }));
      }
      if (url === `/api/v1/library/${encodeURIComponent(options.id)}`) return current.media_files.length ? jsonResponse(current) : errorResponse("Item not found", 404);
      if (init?.method === "DELETE" && url.startsWith(`/api/v1/library/${encodeURIComponent(options.id)}/media-files/`)) {
        deletes += 1;
        const result = options.response?.() ?? noContentResponse();
        return Promise.resolve(result).then((response) => {
          if (response.ok) {
            removed = true;
            const id = Number(url.split("/").at(-1));
            current = { ...current, media_files: current.media_files.filter((copy) => copy.id !== id) };
          }
          return response;
        });
      }
      return undefined;
    });
    return { original, fetchMock, deletes: () => deletes };
  }

  async function select(id: string) {
    const user = await openLibraryPage();
    await user.click(await screen.findByRole("button", { name: id }));
    await screen.findByRole("button", { name: /Delete File:/ });
    return user;
  }

  it("requires explicit keyboard confirmation, identifies the copy, and Cancel/Escape send nothing and restore focus", async () => {
    const { deletes } = setup({ id: "confirm-item", copies: [mediaFile({ display_path: null, path: "relative/target.mp4" })] });
    const user = await select("confirm-item");
    const trigger = screen.getByRole("button", { name: "Delete File: App Library — relative/target.mp4" });
    trigger.focus();
    await user.keyboard("{Enter}");
    const confirmation = screen.getByRole("region", { name: "Confirm Delete File" });
    expect(confirmation).toHaveTextContent("confirm-item");
    expect(confirmation).toHaveTextContent("App Library — relative/target.mp4");
    expect(confirmation).toHaveTextContent("physical file on disk");
    expect(confirmation).toHaveTextContent("last copy");
    expect(confirmation).toHaveTextContent("catalog record");
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(trigger).toHaveFocus();
    expect(deletes()).toBe(0);
    await user.keyboard("{Enter}");
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(deletes()).toBe(0);
    await user.keyboard("{Enter}");
    await user.tab();
    expect(screen.getByRole("button", { name: "Confirm Delete File" })).toHaveFocus();
    await user.keyboard("{Enter}");
    await screen.findByText(/Deleted file for “confirm-item”/);
    expect(deletes()).toBe(1);
  });

  it("removes only the chosen copy immediately and preserves the item and other copy", async () => {
    const refresh = deferred<Response>();
    setup({ id: "two-copy-item", copies: [mediaFile(), mediaFile({ id: 22, storage_type: "plex_video", display_path: "/plex/second.mp4" })], refreshedList: refresh.promise });
    const user = await openLibraryPage();
    await user.click(await screen.findByRole("button", { name: "two-copy-item" }));
    await user.click(await screen.findByRole("button", { name: "Delete File: Plex Videos — /plex/second.mp4" }));
    expect(screen.getByRole("region", { name: "Confirm Delete File" })).toHaveTextContent("If this is the last copy when deleted");
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    const notice = await screen.findByText(/Deleted file for “two-copy-item”/);
    expect(notice).toHaveAttribute("role", "status");
    expect(screen.queryByText("/plex/second.mp4")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Library item details")).toHaveTextContent("/library/video/Sample.mp4");
    expect(screen.getByRole("button", { name: "two-copy-item" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Page 1" })).toHaveFocus();
    await act(async () => { refresh.resolve(jsonResponse(page([listItem({ id: "two-copy-item", title: "two-copy-item" })]))); });
  });

  it("locks selection, pagination, remove and other deletion attempts while deferred DELETE is pending; last copy clears detail", async () => {
    const pending = deferred<Response>();
    const { deletes, fetchMock } = setup({ id: "pending-file-item", response: () => pending.promise });
    const user = await select("pending-file-item");
    await user.click(screen.getByRole("button", { name: /Delete File:/ }));
    const submit = screen.getByRole("button", { name: "Confirm Delete File" });
    act(() => { fireEvent.click(submit); fireEvent.click(submit); });
    const status = await screen.findByText("Deleting selected file…");
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveFocus();
    for (const name of ["pending-file-item", "Untouched Item", "Next", "Previous", "Remove from Library", /Delete File:/]) {
      const button = screen.getByRole("button", { name });
      expect(button).toBeDisabled();
      fireEvent.click(button);
    }
    expect(deletes()).toBe(1);
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/library/untouched" || String(url).includes("offset=50"))).toBe(false);
    await act(async () => { pending.resolve(noContentResponse()); });
    const notice = await screen.findByText(/Deleted file for “pending-file-item”/);
    await waitFor(() => expect(notice).toHaveTextContent("Library item is no longer present"));
    expect(screen.queryByLabelText("Library item details")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "pending-file-item" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Page 1" })).toHaveFocus();
  });

  it.each([
    ["shared", "media_file_shared", "Another catalog entry references this physical file.", 409],
    ["unverifiable", "media_file_reference_unverifiable", "A catalog reference could not be verified safely.", 409],
    ["postunlink", "media_file_catalog_commit_failed", "The file was deleted but catalog commit failed.", 500],
  ])("preserves catalog and server reason on nonretryable %s failure without unintended retry", async (suffix, code, message, status) => {
    const id = `blocked-file-${suffix}`;
    const { deletes } = setup({ id, response: () => jsonResponse({ error: { code, message, retryable: false } }, status) });
    const user = await select(id);
    await user.click(screen.getByRole("button", { name: /Delete File:/ }));
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(message);
    expect(alert).toHaveFocus();
    expect(screen.getByRole("button", { name: id })).toBeEnabled();
    expect(screen.getByLabelText("Library item details")).toHaveTextContent("/library/video/Sample.mp4");
    expect(screen.queryByRole("button", { name: "Retry Delete File" })).not.toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: /Delete File:/ });
    expect(trigger).toBeDisabled();
    fireEvent.click(trigger);
    expect(deletes()).toBe(1);
    if (suffix === "postunlink") {
      expect(alert).toHaveTextContent("The physical file was deleted, but the catalog is out of sync");
      expect(alert).not.toHaveTextContent(/nothing.*deleted/i);
    }
  });

  it("disables and guards every copy action while another copy's DELETE is pending", async () => {
    const pending = deferred<Response>();
    const { deletes } = setup({
      id: "pending-two-files",
      copies: [mediaFile(), mediaFile({ id: 2, display_path: "/other-copy.mp4" })],
      response: () => pending.promise,
    });
    const user = await openLibraryPage();
    await user.click(await screen.findByRole("button", { name: "pending-two-files" }));
    await user.click(await screen.findByRole("button", { name: "Delete File: App Library — /library/video/Sample.mp4" }));
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    await screen.findByText("Deleting selected file…");
    const otherCopy = screen.getByRole("button", { name: "Delete File: App Library — /other-copy.mp4" });
    expect(otherCopy).toBeDisabled();
    fireEvent.click(otherCopy);
    expect(screen.queryByRole("region", { name: "Confirm Delete File" })).not.toBeInTheDocument();
    expect(deletes()).toBe(1);
    await act(async () => { pending.resolve(errorResponse("Temporary failure")); });
    await screen.findByRole("alert");
    expect(otherCopy).toBeEnabled();
  });

  it("retryable failure requires fresh confirmation; cancelling retry never sends DELETE", async () => {
    let attempts = 0;
    const { deletes } = setup({ id: "retry-file-item", response: () => ++attempts === 1 ? errorResponse("Database is busy; try again.") : noContentResponse() });
    const user = await select("retry-file-item");
    await user.click(screen.getByRole("button", { name: /Delete File:/ }));
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Database is busy; try again.");
    await user.click(screen.getByRole("button", { name: "Retry Delete File" }));
    expect(deletes()).toBe(1);
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(deletes()).toBe(1);
    expect(screen.getByRole("button", { name: "Retry Delete File" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Retry Delete File" }));
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    await screen.findByText(/Deleted file for “retry-file-item”/);
    expect(deletes()).toBe(2);
  });

  it.each([false, true])("cancels stale pre-delete list/detail reads so deleted copy cannot return (lastCopy=%s)", async (lastCopy) => {
    const id = `stale-file-${lastCopy}`;
    const copies = lastCopy ? [mediaFile()] : [mediaFile(), mediaFile({ id: 2, display_path: "/remaining.mp4" })];
    const original = libraryItem({ id, title: id, media_files: copies });
    const staleList = deferred<Response>();
    const staleDetail = deferred<Response>();
    let deleted = false;
    let listReads = 0;
    let detailReads = 0;
    stubRoutes((url, init) => {
      if (init?.method === "DELETE") { deleted = true; return noContentResponse(); }
      const remaining = { ...original, media_files: copies.filter((copy) => copy.id !== 1) };
      if (url.includes("?limit=50")) {
        listReads += 1;
        if (listReads === 2) return staleList.promise;
        return jsonResponse(page(deleted ? lastCopy ? [] : [remaining] : [original]));
      }
      if (url === `/api/v1/library/${id}`) {
        detailReads += 1;
        if (detailReads === 2) return staleDetail.promise;
        return deleted && lastCopy ? errorResponse("Item not found", 404) : jsonResponse(deleted ? remaining : original);
      }
      return undefined;
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const otherPageKey = ["library", "list", { limit: 50, offset: 50 }];
    client.setQueryData(otherPageKey, page([original], { offset: 50 }));
    render(<QueryClientProvider client={client}><LibraryPage /></QueryClientProvider>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: id }));
    await screen.findByRole("button", { name: "Delete File: App Library — /library/video/Sample.mp4" });
    act(() => { void client.invalidateQueries({ queryKey: ["library"] }); });
    await waitFor(() => { expect(listReads).toBe(2); expect(detailReads).toBe(2); });
    await user.click(screen.getByRole("button", { name: "Delete File: App Library — /library/video/Sample.mp4" }));
    await user.click(screen.getByRole("button", { name: "Confirm Delete File" }));
    await screen.findByText(lastCopy ? /Library item is no longer present/ : /Other copies remain in the Library/);
    await act(async () => { staleList.resolve(jsonResponse(page([original]))); staleDetail.resolve(jsonResponse(original)); });
    expect(screen.queryByText("/library/video/Sample.mp4")).not.toBeInTheDocument();
    const cachedPage = client.getQueryData<LibraryListResult>(otherPageKey);
    expect(cachedPage?.items.flatMap((item) => item.media_files.map((copy) => copy.id))).toEqual(lastCopy ? [] : [2]);
    if (lastCopy) {
      expect(client.getQueryData(["library", "detail", id])).toBeUndefined();
      expect(screen.queryByLabelText("Library item details")).not.toBeInTheDocument();
    } else {
      expect(screen.getByLabelText("Library item details")).toHaveTextContent("/remaining.mp4");
      expect(client.getQueryData<LibraryItem>(["library", "detail", id])?.media_files.map((copy) => copy.id)).toEqual([2]);
    }
    client.clear();
  });
});

/* ---- Pagination ---- */

describe("LibraryPage pagination", () => {
  it("pages through the offset API and clears the selection on page change", async () => {
    const fetchMock = stubRoutes((url) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        return jsonResponse(page([listItem({ id: "page1-item", title: "Page One Item" })], { has_more: true }));
      }
      if (url === "/api/v1/library?limit=50&offset=50") {
        return jsonResponse(
          page([listItem({ id: "page2-item", title: "Page Two Item" })], {
            offset: 50,
          }),
        );
      }
      if (url === "/api/v1/library/page1-item") {
        return jsonResponse(
          libraryItem({ id: "page1-item", title: "Page One Item" }),
        );
      }
      return undefined;
    });
    const user = await openLibraryPage();

    // Wait for this test's own first page so the pagination button states
    // reflect its has_more, not a cached earlier page.
    await screen.findByRole("button", { name: "Page One Item" });
    const previous = screen.getByRole("button", { name: "Previous" });
    const next = screen.getByRole("button", { name: "Next" });
    expect(previous).toBeDisabled();
    expect(next).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Page One Item" }));
    expect(
      await screen.findByLabelText("Library item details"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Page One Item" }),
    ).toHaveAttribute("aria-pressed", "true");

    await user.click(next);
    expect(
      await screen.findByRole("button", { name: "Page Two Item" }),
    ).toBeInTheDocument();

    // Focus moves to the page status heading once the transition completes
    // (the clicked Next button is disabled while fetching).
    expect(screen.getByRole("heading", { name: "Page 2" })).toHaveFocus();

    // offset=50 requested once; previous page's selection/detail cleared.
    expect(
      fetchMock.mock.calls.filter(
        ([url]) => url === "/api/v1/library?limit=50&offset=50",
      ),
    ).toHaveLength(1);
    expect(screen.queryByLabelText("Library item details")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("keeps Previous available on an empty later page with a distinct message", async () => {
    stubRoutes((url) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        return jsonResponse(
          page([listItem({ id: "p1-item", title: "Page One Item" })], {
            has_more: true,
          }),
        );
      }
      if (url === "/api/v1/library?limit=50&offset=50") {
        return jsonResponse(page([], { offset: 50 }));
      }
      return undefined;
    });
    const user = await openLibraryPage();

    await screen.findByRole("button", { name: "Page One Item" });
    await user.click(screen.getByRole("button", { name: "Next" }));

    // Later-page empty is distinct from the first-page empty message.
    expect(
      await screen.findByText("No downloads on this page."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("No downloads in the Library yet."),
    ).not.toBeInTheDocument();
    // Not stranded: Previous stays reachable on the empty later page.
    const previous = screen.getByRole("button", { name: "Previous" });
    expect(previous).toBeEnabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    await user.click(previous);
    expect(
      await screen.findByRole("button", { name: "Page One Item" }),
    ).toBeInTheDocument();
  });

  it("keeps Previous available on a failed later page and recovers by retry", async () => {
    let pageTwoRequests = 0;
    stubRoutes((url) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        return jsonResponse(
          page([listItem({ id: "p1-item", title: "Page One Item" })], {
            has_more: true,
          }),
        );
      }
      if (url === "/api/v1/library?limit=50&offset=50") {
        pageTwoRequests += 1;
        return pageTwoRequests === 1
          ? jsonResponse(
              {
                error: {
                  code: "internal_error",
                  message: "Library page failed",
                  retryable: true,
                },
              },
              503,
            )
          : jsonResponse(
              page([listItem({ id: "p2-item", title: "Page Two Item" })], {
                offset: 50,
              }),
            );
      }
      return undefined;
    });
    const user = await openLibraryPage();

    await screen.findByRole("button", { name: "Page One Item" });
    await user.click(screen.getByRole("button", { name: "Next" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Library page failed");
    // The failed transition moves focus to the page status heading.
    expect(
      screen.getByRole("heading", { name: "Page 2 failed to load" }),
    ).toHaveFocus();
    // Not stranded: Previous stays reachable while the page failed to load.
    expect(screen.getByRole("button", { name: "Previous" })).toBeEnabled();

    await user.click(within(alert).getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByRole("button", { name: "Page Two Item" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Page 2" })).toBeInTheDocument();
  });

  it("advances only one page per transition on rapid Next clicks", async () => {
    const pendingPageTwo = deferred<Response>();
    const fetchMock = stubRoutes((url) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        return jsonResponse(
          page([listItem({ id: "p1-item", title: "Page One Item" })], {
            has_more: true,
          }),
        );
      }
      if (url === "/api/v1/library?limit=50&offset=50") {
        return pendingPageTwo.promise;
      }
      return undefined;
    });
    const user = await openLibraryPage();

    await screen.findByRole("button", { name: "Page One Item" });
    const next = screen.getByRole("button", { name: "Next" });
    await user.click(next);

    // While the next page is still in flight, the controls are in their
    // safe state: a rapid second click on the disabled button is a no-op.
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Next" }));

    await act(async () => {
      pendingPageTwo.resolve(
        jsonResponse(
          page([listItem({ id: "p2-item", title: "Page Two Item" })], {
            offset: 50,
          }),
        ),
      );
    });

    expect(
      await screen.findByRole("button", { name: "Page Two Item" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Page 2" })).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(
        ([url]) => url === "/api/v1/library?limit=50&offset=50",
      ),
    ).toHaveLength(1);
    expect(
      fetchMock.mock.calls.some(
        ([url]) => url === "/api/v1/library?limit=50&offset=100",
      ),
    ).toBe(false);
  });
});

/* ---- Selection and detail ---- */

describe("LibraryPage detail", () => {
  // The app-level QueryClient is shared across tests; every test uses a
  // distinct item id so a cached detail from one test can never satisfy
  // (or mask) another test's detail query.
  type DetailResponse =
    | Response
    | Promise<Response>
    | ((requestNumber: number) => Response);

  function stubListWithDetail(
    itemId: string,
    detail: DetailResponse,
    listOverrides: Partial<LibraryListItem> = {},
  ) {
    let requestNumber = 0;
    return stubRoutes((url) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        return jsonResponse(
          page([
            listItem({
              id: itemId,
              media_type: "audio",
              format: "m4a",
              created_at: "2026-09-27T14:31:00Z",
              ...listOverrides,
            }),
          ]),
        );
      }
      if (url === `/api/v1/library/${itemId}`) {
        requestNumber += 1;
        return typeof detail === "function" ? detail(requestNumber) : detail;
      }
      return undefined;
    });
  }

  it("loads the detail for the selected item with authoritative fields and copies", async () => {
    const itemId = "fields-item";
    stubListWithDetail(
      itemId,
      jsonResponse(
        libraryItem({
          id: itemId,
          title: "Led Zeppelin IV",
          media_type: "audio",
          format: "m4a",
          created_at: "2026-09-27T14:31:00Z",
          platform: "youtube",
          artist: "Led Zeppelin",
          album: "Led Zeppelin IV",
          codec: "alac",
          bitrate: 256,
          resolution: null,
          duration: 205.4,
          media_files: [
            mediaFile({
              id: 1,
              storage_type: "app_library",
              size_bytes: 4_194_304,
              path: "music/x.m4a",
              display_path: "/library/music/x.m4a",
            }),
            mediaFile({
              id: 2,
              storage_type: "plex_music",
              size_bytes: 8_388_608,
              path: "x.m4a",
              display_path: "/plex/music/x.m4a",
            }),
          ],
        }),
      ),
      { title: "Led Zeppelin IV" },
    );
    const user = await openLibraryPage();

    const selectButton = await screen.findByRole("button", {
      name: "Led Zeppelin IV",
    });
    await user.click(selectButton);

    expect(selectButton).toHaveAttribute("aria-pressed", "true");

    const detail = await screen.findByLabelText("Library item details");
    expect(detail).toHaveTextContent("TypeAudio");
    expect(detail).toHaveTextContent("Downloaded At27 Sep 2026, 14:31 UTC");
    expect(detail).toHaveTextContent("Formatm4a");
    expect(detail).toHaveTextContent("Codecalac");
    expect(detail).toHaveTextContent("Bitrate256 kbps");
    expect(detail).toHaveTextContent("Duration3:25");
    expect(detail).toHaveTextContent("ArtistLed Zeppelin");
    expect(detail).toHaveTextContent("AlbumLed Zeppelin IV");
    expect(detail).toHaveTextContent("PlatformYouTube");

    const sourceLink = screen.getByRole("link", {
      name: "https://www.youtube.com/watch?v=abc123",
    });
    expect(sourceLink).toHaveAttribute(
      "href",
      "https://www.youtube.com/watch?v=abc123",
    );

    // Both physical copies, stable API order, with location/path/size.
    const copies = within(detail).getAllByRole("listitem");
    expect(copies.map((copy) => copy.textContent)).toEqual([
      "App Library/library/music/x.m4a4.0 MiBDelete File",
      "Plex Music/plex/music/x.m4a8.0 MiBDelete File",
    ]);

    // Raw catalog ids stay out of the user-facing detail.
    expect(screen.queryByText(itemId)).not.toBeInTheDocument();
  });

  it("renders nullable fields only when they carry information", async () => {
    const itemId = "bare-item";
    stubListWithDetail(
      itemId,
      jsonResponse(
        libraryItem({
          id: itemId,
          title: "Bare Item",
          artist: null,
          album: null,
          codec: null,
          bitrate: null,
          resolution: null,
          duration: null,
          platform: null,
        }),
      ),
      { title: "Bare Item" },
    );
    const user = await openLibraryPage();

    await user.click(await screen.findByRole("button", { name: "Bare Item" }));

    const detail = await screen.findByLabelText("Library item details");
    for (const label of [
      "Codec",
      "Bitrate",
      "Resolution",
      "Duration",
      "Artist",
      "Album",
      "Platform",
    ]) {
      expect(within(detail).queryByText(label)).not.toBeInTheDocument();
    }
  });

  it("shows a detail loading state while the item is fetched", async () => {
    const itemId = "loading-item";
    const pending = deferred<Response>();
    stubListWithDetail(itemId, pending.promise, { title: "Sample Video" });
    const user = await openLibraryPage();

    await user.click(await screen.findByRole("button", { name: "Sample Video" }));

    expect(await screen.findByText("Loading details…")).toBeInTheDocument();

    await act(async () => {
      pending.resolve(jsonResponse(libraryItem({ id: itemId })));
    });

    expect(await screen.findByText("h264")).toBeInTheDocument();
  });

  it("surfaces detail errors with retry and recovers", async () => {
    const itemId = "error-item";
    let detailRequests = 0;
    stubListWithDetail(
      itemId,
      (requestNumber) => {
        detailRequests = requestNumber;
        return requestNumber === 1
          ? jsonResponse(
              {
                error: {
                  code: "internal_error",
                  message: "Details unavailable",
                  retryable: true,
                },
              },
              503,
            )
          : jsonResponse(libraryItem({ id: itemId }));
      },
      { title: "Sample Video" },
    );
    const user = await openLibraryPage();

    await user.click(await screen.findByRole("button", { name: "Sample Video" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Details unavailable");

    await user.click(within(alert).getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("1080p")).toBeInTheDocument();
    expect(detailRequests).toBe(2);
  });

  it("does not render a stale detail under a newer selection", async () => {
    const firstDetail = deferred<Response>();
    const secondDetail = deferred<Response>();
    stubRoutes((url) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        return jsonResponse(
          page([
            listItem({ id: "first-item", title: "First Item" }),
            listItem({ id: "second-item", title: "Second Item" }),
          ]),
        );
      }
      if (url === "/api/v1/library/first-item") return firstDetail.promise;
      if (url === "/api/v1/library/second-item") return secondDetail.promise;
      return undefined;
    });
    const user = await openLibraryPage();

    await user.click(await screen.findByRole("button", { name: "First Item" }));
    expect(await screen.findByText("Loading details…")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Second Item" }));
    expect(screen.getByText("Loading details…")).toBeInTheDocument();

    // The slower first response resolves while the second is still pending;
    // it must never appear under the newer selection.
    await act(async () => {
      firstDetail.resolve(
        jsonResponse(
          libraryItem({ id: "first-item", title: "First Item", artist: "Stale Artist" }),
        ),
      );
    });
    expect(screen.queryByText("Stale Artist")).not.toBeInTheDocument();
    expect(screen.getByText("Loading details…")).toBeInTheDocument();

    await act(async () => {
      secondDetail.resolve(
        jsonResponse(
          libraryItem({ id: "second-item", title: "Second Item", artist: "Fresh Artist" }),
        ),
      );
    });
    expect(await screen.findByText("Fresh Artist")).toBeInTheDocument();
    expect(screen.queryByText("Stale Artist")).not.toBeInTheDocument();
  });
});

/* ---- Remove from Library (record only, PRODUCT_SPEC §21) ---- */

describe("LibraryPage remove from library", () => {
  // Each test uses a distinct item id: the app-level QueryClient is shared
  // across tests, so a cached detail from one test must never satisfy (or
  // mask) another test's queries.
  function stubRemovableItem(options: {
    itemId: string;
    title: string;
    deleteResponse: () => Response | Promise<Response>;
    listAfterRemove?: LibraryListItem[];
  }) {
    let listRequests = 0;
    const fetchMock = stubRoutes((url, init) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        listRequests += 1;
        return jsonResponse(
          listRequests === 1
            ? page([listItem({ id: options.itemId, title: options.title })])
            : page(options.listAfterRemove ?? []),
        );
      }
      if (
        url === `/api/v1/library/${encodeURIComponent(options.itemId)}` &&
        init?.method !== "DELETE"
      ) {
        return jsonResponse(
          libraryItem({ id: options.itemId, title: options.title }),
        );
      }
      if (
        url === `/api/v1/library/${encodeURIComponent(options.itemId)}` &&
        init?.method === "DELETE"
      ) {
        return options.deleteResponse();
      }
      return undefined;
    });
    return { fetchMock, listRequests: () => listRequests };
  }

  async function selectItem(user: ReturnType<typeof userEvent.setup>, name: string) {
    await user.click(await screen.findByRole("button", { name }));
    // The loaded detail is where the remove action lives.
    await screen.findByLabelText("Library item details");
  }

  it("sends a body-less DELETE for the encoded item id", async () => {
    // Characters that must not leak into the URL unencoded.
    const itemId = "needs/encoding id";
    const { fetchMock } = stubRemovableItem({
      itemId,
      title: "Sample Video",
      deleteResponse: () => noContentResponse(),
    });
    const user = await openLibraryPage();

    await selectItem(user, "Sample Video");
    await user.click(screen.getByRole("button", { name: "Remove from Library" }));

    expect(
      await screen.findByText(/Removed “Sample Video” from the Library/),
    ).toBeInTheDocument();
    const deleteCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        init?.method === "DELETE" &&
        url === `/api/v1/library/${encodeURIComponent(itemId)}`,
    );
    // Exactly one DELETE, to the encoded id path, with no request body
    // (ARCHITECTURE §37: no filesystem paths travel with the request).
    expect(deleteCall).toBeDefined();
    expect(deleteCall?.[1]?.body).toBeUndefined();
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE"),
    ).toHaveLength(1);
  });

  it("distinguishes record-only removal from physical file deletion", async () => {
    stubRemovableItem({
      itemId: "copy-item",
      title: "Copy Check Item",
      deleteResponse: () => noContentResponse(),
    });
    const user = await openLibraryPage();

    await selectItem(user, "Copy Check Item");

    expect(
      screen.getByRole("button", { name: "Remove from Library" }),
    ).toBeInTheDocument();
    // Record removal stays separate from the copy-specific danger action.
    expect(
      screen.getByText(/the file on disk is not deleted/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /delete file/i }),
    ).toHaveClass("button-danger");
    expect(
      screen.queryByText(/confirm|are you sure/i),
    ).not.toBeInTheDocument();
  });

  it("shows a pending label, blocks re-triggering while in flight", async () => {
    const pendingDelete = deferred<Response>();
    const { fetchMock } = stubRemovableItem({
      itemId: "pending-item",
      title: "Pending Item",
      deleteResponse: () => pendingDelete.promise,
    });
    const user = await openLibraryPage();

    await selectItem(user, "Pending Item");
    await user.click(screen.getByRole("button", { name: "Remove from Library" }));

    // Pending state: an explicit label plus a disabled control, so a rapid
    // second click cannot fire a second DELETE.
    const pendingButton = screen.getByRole("button", { name: "Removing…" });
    expect(pendingButton).toBeDisabled();
    const deleteFile = screen.getByRole("button", { name: /Delete File:/ });
    expect(deleteFile).toBeDisabled();
    fireEvent.click(deleteFile);
    expect(screen.queryByRole("region", { name: "Confirm Delete File" })).not.toBeInTheDocument();
    await user.click(pendingButton);
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE"),
    ).toHaveLength(1);
    // The loaded detail (and the list row) stay visible while pending.
    expect(screen.getByLabelText("Library item details")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Pending Item" }),
    ).toBeInTheDocument();

    await act(async () => {
      pendingDelete.resolve(noContentResponse());
    });

    expect(
      await screen.findByText(/Removed “Pending Item” from the Library/),
    ).toBeInTheDocument();
  });

  it("clears the detail, refreshes the list, announces success, and moves focus", async () => {
    const { listRequests } = stubRemovableItem({
      itemId: "success-item",
      title: "Doomed Item",
      deleteResponse: () => noContentResponse(),
    });
    const user = await openLibraryPage();

    await selectItem(user, "Doomed Item");
    await user.click(screen.getByRole("button", { name: "Remove from Library" }));

    const notice = await screen.findByText(
      /Removed “Doomed Item” from the Library\. The file on disk was not deleted\./,
    );
    expect(notice).toHaveAttribute("role", "status");

    // The selection/detail is cleared and the active list refetches from
    // the server without the removed item.
    expect(
      screen.queryByLabelText("Library item details"),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByText("No downloads in the Library yet."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Doomed Item" }),
    ).not.toBeInTheDocument();
    expect(listRequests()).toBe(2);

    // The unmounted Remove button would drop focus to <body>; the page
    // status heading (right above the notice) receives it instead.
    expect(screen.getByRole("heading", { name: "Page 1" })).toHaveFocus();
  });

  it("surfaces removal errors accessibly, keeps the item, and retries", async () => {
    let deleteRequests = 0;
    stubRemovableItem({
      itemId: "error-item",
      title: "Failing Item",
      deleteResponse: () => {
        deleteRequests += 1;
        return deleteRequests === 1
          ? errorResponse("Removal failed")
          : noContentResponse();
      },
    });
    const user = await openLibraryPage();

    await selectItem(user, "Failing Item");
    await user.click(screen.getByRole("button", { name: "Remove from Library" }));

    // The failure is announced from inside the still-mounted detail; the
    // list row and the detail stay put so nothing the user was looking at
    // disappears, and the action is offered again.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Could not remove from the Library/);
    expect(alert).toHaveTextContent("Removal failed");
    expect(
      screen.getByRole("button", { name: "Failing Item" }),
    ).toBeInTheDocument();
    // A settled failure reopens the locked context: the list row stays a
    // selectable control again, ready for another attempt or another item.
    expect(
      screen.getByRole("button", { name: "Failing Item" }),
    ).toBeEnabled();
    expect(screen.getByLabelText("Library item details")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove from Library" }),
    ).toBeEnabled();

    await user.click(within(alert).getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByText(/Removed “Failing Item” from the Library/),
    ).toBeInTheDocument();
    expect(deleteRequests).toBe(2);
    expect(
      screen.queryByLabelText("Library item details"),
    ).not.toBeInTheDocument();
  });

  it("locks item selection and pagination while a removal is pending", async () => {
    const itemA = "lock-item-a";
    const itemB = "lock-item-b";
    const pendingDelete = deferred<Response>();
    let listRequests = 0;
    const fetchMock = stubRoutes((url, init) => {
      if (url === "/api/v1/library?limit=50&offset=0") {
        listRequests += 1;
        // First load carries both items (has_more keeps Next meaningful);
        // the post-removal refetch no longer contains the removed item.
        return jsonResponse(
          listRequests === 1
            ? page(
                [
                  listItem({ id: itemA, title: "Removing Item" }),
                  listItem({ id: itemB, title: "Other Item" }),
                ],
                { has_more: true },
              )
            : page([listItem({ id: itemB, title: "Other Item" })]),
        );
      }
      if (url === `/api/v1/library/${itemA}` && init?.method !== "DELETE") {
        return jsonResponse(libraryItem({ id: itemA, title: "Removing Item" }));
      }
      if (url === `/api/v1/library/${itemA}` && init?.method === "DELETE") {
        return pendingDelete.promise;
      }
      if (url === `/api/v1/library/${itemB}`) {
        return jsonResponse(libraryItem({ id: itemB, title: "Other Item" }));
      }
      return undefined;
    });
    const user = await openLibraryPage();

    // Start the removal for item A and hold it in flight.
    await user.click(await screen.findByRole("button", { name: "Removing Item" }));
    await screen.findByLabelText("Library item details");
    await user.click(screen.getByRole("button", { name: "Remove from Library" }));

    // While the removal is pending the selection context is locked: every
    // item selection control (including the selected row and other rows)
    // sits disabled, and the page transition controls are disabled too —
    // clicks on them are no-ops, so the request cannot settle against a
    // different item or page.
    expect(screen.getByRole("button", { name: "Removing…" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Removing Item" }),
    ).toBeDisabled();
    const otherButton = screen.getByRole("button", { name: "Other Item" });
    expect(otherButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await user.click(otherButton);
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByLabelText("Library item details")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([url]) => url === `/api/v1/library/${itemB}`),
    ).toHaveLength(0);
    expect(
      fetchMock.mock.calls.filter(
        ([url]) => url === "/api/v1/library?limit=50&offset=50",
      ),
    ).toHaveLength(0);
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE"),
    ).toHaveLength(1);

    // Settling the deferred DELETE produces the expected success cleanup.
    await act(async () => {
      pendingDelete.resolve(noContentResponse());
    });

    expect(
      await screen.findByText(/Removed “Removing Item” from the Library/),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Library item details"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Removing Item" }),
    ).not.toBeInTheDocument();

    // The lock is released after settling: the surviving item is a
    // selectable control again and selecting it loads its own detail.
    expect(otherButton).toBeEnabled();
    await user.click(otherButton);
    expect(
      await screen.findByLabelText("Library item details"),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Item Details" })).toBeInTheDocument();
  });
});
