import { useQuery } from "@tanstack/react-query";
import { ApiError, apiGet } from "./client";

/**
 * Frontend mirror of the read-only M7-S1 Settings contract
 * (backend/app/api/v1/settings.py; PRODUCT_SPEC §23, ARCHITECTURE §47).
 * Values come from the deployment environment; nothing here is editable and
 * reading never triggers a new storage check.
 */
export type DestinationTarget = "app_library" | "plex_music" | "plex_video";
export type StartupProbeTarget = "staging" | DestinationTarget;

export interface DestinationSettings {
  target: DestinationTarget;
  configured: boolean;
  /** Plex stays not selectable in v0.1 even when configured. */
  selectable: boolean;
  /** Explicit Plex display path only; never a raw container root. */
  display_path: string | null;
}

export interface StartupProbeEntry {
  target: StartupProbeTarget;
  ok: boolean;
  reason: string | null;
}

export interface StartupStorageCheck {
  /** `unavailable`: no stored startup result (never success or failure). */
  status: "completed" | "unavailable";
  results: StartupProbeEntry[];
}

export interface AppSettings {
  max_concurrent_jobs: number;
  default_destination: string;
  conflict_policy: string;
  destinations: DestinationSettings[];
  startup_storage_check: StartupStorageCheck;
}

const DESTINATION_TARGETS: readonly string[] = ["app_library", "plex_music", "plex_video"];
const PROBE_TARGETS: readonly string[] = ["staging", ...DESTINATION_TARGETS];

function isRecord(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

function isDestination(value: unknown): value is DestinationSettings {
  return (
    isRecord(value) &&
    typeof value.target === "string" &&
    DESTINATION_TARGETS.includes(value.target) &&
    typeof value.configured === "boolean" &&
    typeof value.selectable === "boolean" &&
    (value.display_path === null || typeof value.display_path === "string")
  );
}

function isProbeEntry(value: unknown): value is StartupProbeEntry {
  return (
    isRecord(value) &&
    typeof value.target === "string" &&
    PROBE_TARGETS.includes(value.target) &&
    typeof value.ok === "boolean" &&
    (value.reason === null || typeof value.reason === "string")
  );
}

function isStartupCheck(value: unknown): value is StartupStorageCheck {
  return (
    isRecord(value) &&
    (value.status === "completed" || value.status === "unavailable") &&
    Array.isArray(value.results) &&
    value.results.every(isProbeEntry)
  );
}

/**
 * A malformed body is a contract violation and surfaces as a query error:
 * it must never read as "storage check succeeded" or as a disk failure.
 */
export function parseAppSettings(value: unknown): AppSettings {
  if (
    isRecord(value) &&
    typeof value.max_concurrent_jobs === "number" &&
    typeof value.default_destination === "string" &&
    typeof value.conflict_policy === "string" &&
    Array.isArray(value.destinations) &&
    value.destinations.every(isDestination) &&
    isStartupCheck(value.startup_storage_check)
  ) {
    return {
      max_concurrent_jobs: value.max_concurrent_jobs,
      default_destination: value.default_destination,
      conflict_policy: value.conflict_policy,
      destinations: value.destinations,
      startup_storage_check: value.startup_storage_check,
    };
  }
  throw new ApiError(200, "The settings response is not in the expected format.");
}

export async function getAppSettings(): Promise<AppSettings> {
  return parseAppSettings(await apiGet<unknown>("/api/v1/settings"));
}

/** Folder labels follow PRODUCT_SPEC §11 naming plus the staging folder. */
export function startupTargetLabel(target: StartupProbeTarget): string {
  switch (target) {
    case "staging":
      return "Staging";
    case "app_library":
      return "App Library";
    case "plex_music":
      return "Plex Music";
    case "plex_video":
      return "Plex Videos";
  }
}

const REASON_LABELS = new Map<string, string>([
  ["root_unavailable", "folder was unavailable"],
  ["root_changed", "folder changed during startup"],
  ["name_collision", "no free test file name"],
  ["create_failed", "could not create a test file"],
  ["write_failed", "could not write a test file"],
  ["cleanup_failed", "could not remove the test file"],
  ["cleanup_unverified", "test file removal could not be verified"],
  ["probe_failed", "check failed unexpectedly"],
]);

/** Fixed, path-free reason tokens; an unknown token stays a generic failure. */
export function startupReasonLabel(reason: string | null): string {
  return (reason != null ? REASON_LABELS.get(reason) : undefined) ?? "write check failed";
}

/** Failed entries of a completed startup check; none when unavailable. */
export function failedStartupTargets(settings: AppSettings | undefined): StartupProbeEntry[] {
  const check = settings?.startup_storage_check;
  if (check?.status !== "completed") return [];
  return check.results.filter((entry) => !entry.ok);
}

/**
 * Shared by the Settings page and the app-wide startup warning. No polling:
 * only React Query's normal reads (mount, window focus) re-read the stored
 * snapshot, e.g. after a backend restart.
 */
export function useAppSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: getAppSettings,
    retry: false,
  });
}
