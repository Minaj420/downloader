import type { LibraryMediaType, LibraryStorageType } from "../../api/library";

const MONTH_LABELS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

/**
 * Deterministic, human-readable date label (e.g. "27 Sep 2026, 14:31 UTC").
 *
 * Built from explicit UTC parts instead of `Intl.DateTimeFormat` so the
 * output cannot drift with the host locale, time zone or ICU data; invalid
 * or missing input falls back to a safe placeholder.
 */
export function formatLibraryDate(value: string | null | undefined): string {
  if (value == null) {
    return "—";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "—";
  }
  const hours = String(parsed.getUTCHours()).padStart(2, "0");
  const minutes = String(parsed.getUTCMinutes()).padStart(2, "0");
  return `${parsed.getUTCDate()} ${MONTH_LABELS[parsed.getUTCMonth()]} ${parsed.getUTCFullYear()}, ${hours}:${minutes} UTC`;
}

/** Destination labels follow PRODUCT_SPEC §11 naming. */
export function storageTypeLabel(storage: LibraryStorageType): string {
  switch (storage) {
    case "app_library":
      return "App Library";
    case "plex_music":
      return "Plex Music";
    case "plex_video":
      return "Plex Videos";
  }
}

export function mediaTypeLabel(mediaType: LibraryMediaType): string {
  return mediaType === "audio" ? "Audio" : "Video";
}

/** Catalog bitrate is stored in kbps (same unit as the analysis formats). */
export function formatBitrate(bitrate: number | null | undefined): string | null {
  if (bitrate == null || !Number.isFinite(bitrate) || bitrate <= 0) {
    return null;
  }
  return `${Math.round(bitrate)} kbps`;
}

/**
 * Returns the URL unchanged when it is a navigable http(s) address,
 * otherwise null (rendered as plain text — never a clickable link).
 */
export function httpUrlOrNull(url: string | null | undefined): string | null {
  if (url == null) {
    return null;
  }
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? parsed.toString()
      : null;
  } catch {
    return null;
  }
}
