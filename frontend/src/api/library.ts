import { useQuery } from "@tanstack/react-query";
import { apiDelete, apiGet } from "./client";

/**
 * Frontend mirror of the M3-LIB-2 read-only Library contract
 * (backend/app/domain/library.py). The enums are the backend's closed
 * vocabularies; unexpected values are a contract violation, not a UI state.
 */
export type LibraryStorageType = "app_library" | "plex_music" | "plex_video";
export type LibraryMediaType = "audio" | "video";

/** One physical copy of a library item (backend LibraryMediaFileDto). */
export interface LibraryMediaFile {
  id: number;
  storage_type: LibraryStorageType;
  /** Storage-controlled relative path from the catalog (ARCHITECTURE §37). */
  path: string;
  display_path: string | null;
  size_bytes: number;
  created_at: string;
}

/** Compact Library row (backend LibraryListItemDto, PRODUCT_SPEC §20). */
export interface LibraryListItem {
  id: string;
  job_id: string;
  title: string;
  media_type: LibraryMediaType;
  format: string | null;
  created_at: string;
  media_files: LibraryMediaFile[];
}

/** Full Library item detail (backend LibraryItemDto). */
export interface LibraryItem extends LibraryListItem {
  source_url: string;
  source_id: string | null;
  platform: string | null;
  artist: string | null;
  album: string | null;
  codec: string | null;
  bitrate: number | null;
  resolution: string | null;
  duration: number | null;
}

/** One newest-first page of the owner's Library (backend LibraryListResponse). */
export interface LibraryListResult {
  items: LibraryListItem[];
  limit: number;
  offset: number;
  has_more: boolean;
}

export const LIBRARY_PAGE_SIZE = 50;

export async function getLibraryPage(
  limit: number,
  offset: number,
): Promise<LibraryListResult> {
  return apiGet<LibraryListResult>(
    `/api/v1/library?limit=${limit}&offset=${offset}`,
  );
}

export async function getLibraryItem(itemId: string): Promise<LibraryItem> {
  return apiGet<LibraryItem>(`/api/v1/library/${encodeURIComponent(itemId)}`);
}

/**
 * Remove from Library (PRODUCT_SPEC §21, ARCHITECTURE §37–38): deletes the
 * catalog record only; the physical media file stays untouched. Deletion is
 * by library id only — the request carries no body and no filesystem path.
 * Success is 204 No Content.
 */
export async function removeLibraryItem(itemId: string): Promise<void> {
  return apiDelete(`/api/v1/library/${encodeURIComponent(itemId)}`);
}

/** Delete exactly one catalog-selected physical copy; 204 has no body. */
export async function deleteLibraryFile(itemId: string, mediaFileId: number): Promise<void> {
  return apiDelete(`/api/v1/library/${encodeURIComponent(itemId)}/media-files/${mediaFileId}`);
}

/**
 * One page of the Library, newest first. The query key carries the
 * pagination parameters so every page is cached and invalidated separately.
 */
export function useLibraryList(offset: number) {
  return useQuery({
    queryKey: ["library", "list", { limit: LIBRARY_PAGE_SIZE, offset }],
    queryFn: () => getLibraryPage(LIBRARY_PAGE_SIZE, offset),
  });
}

/**
 * Detail for the selected item only; `enabled` keeps the query (and its
 * network request) idle while nothing is selected. The item id in the
 * query key guarantees a rapid re-selection can never render a previous
 * item's response under the new selection.
 */
export function useLibraryItem(itemId: string | null) {
  return useQuery({
    queryKey: ["library", "detail", itemId],
    queryFn: () => getLibraryItem(itemId ?? ""),
    enabled: itemId != null,
  });
}
