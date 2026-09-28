import type { ActivityEvent } from "../../api/activity";

export type ActivityLoadStatus = "idle" | "loading" | "loaded" | "error";

/**
 * Merges two activity sources (REST history and live SSE events) into one
 * id-ascending list, deduplicated by id — a persisted id that arrived live
 * before a REST refresh must never render twice.
 */
export function mergeActivityEvents(
  existing: ActivityEvent[],
  incoming: ActivityEvent[],
): ActivityEvent[] {
  const byId = new Map<number, ActivityEvent>();
  for (const event of existing) {
    if (!byId.has(event.id)) {
      byId.set(event.id, event);
    }
  }
  for (const event of incoming) {
    if (!byId.has(event.id)) {
      byId.set(event.id, event);
    }
  }
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

/** Local `HH:MM:SS` label for one activity row ("" for invalid dates). */
export function formatActivityTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
