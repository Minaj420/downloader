import { useState } from "react";
import type { ActivityEvent } from "../../api/activity";
import { formatActivityTime, type ActivityLoadStatus } from "./activity";

interface ActivityPanelProps {
  /** Merged (REST + live) activity events; ordered by id. */
  events: ActivityEvent[];
  loadStatus: ActivityLoadStatus;
  /** Re-fetches the full history from REST after a failed load. */
  onRefresh: () => void;
}

/**
 * Collapsible Activity panel (PRODUCT_SPEC §18): collapsed by default,
 * clean `HH:MM:SS message` rows. History is loaded after SSE opens; live
 * events continue to arrive while the panel is collapsed.
 */
export function ActivityPanel({ events, loadStatus, onRefresh }: ActivityPanelProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <section className="activity-panel" aria-label="Job activity">
      <button
        type="button"
        className="button-quiet activity-toggle"
        aria-expanded={expanded}
        aria-controls="activity-list"
        onClick={() => setExpanded((value) => !value)}
      >
        Activity {expanded ? "▾" : "▸"}
      </button>
      {expanded && (
        <ol className="activity-list" id="activity-list">
          {loadStatus === "loading" && events.length === 0 && (
            <li className="activity-empty">Loading activity…</li>
          )}
          {loadStatus === "error" && (
            <li className="activity-empty">
              Could not load activity.{" "}
              <button type="button" className="button-secondary" onClick={onRefresh}>
                Retry
              </button>
            </li>
          )}
          {loadStatus === "loaded" && events.length === 0 && (
            <li className="activity-empty">No activity yet.</li>
          )}
          {events.map((event) => (
            <li key={event.id} className="activity-item">
              <span className="activity-time">
                {formatActivityTime(event.created_at)}
              </span>
              <span className="activity-message">{event.message}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
