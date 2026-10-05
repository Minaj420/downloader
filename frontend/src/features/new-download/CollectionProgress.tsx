import type { Job } from "../../api/jobs";

export type ItemsLoadStatus = "loaded" | "loading" | "error";

export function CollectionProgress({ job, loadStatus }: {
  job: Job;
  loadStatus: ItemsLoadStatus;
}) {
  return (
    <section aria-label="Collection results">
      <p className="job-progress-meta">
        Completed: {job.completed_items ?? "Unknown"} / Failed: {job.failed_items ?? "Unknown"} / Total: {job.total_items ?? "Unknown"}
      </p>
      {loadStatus !== "loaded" && (
        <p role="status">{loadStatus === "loading"
          ? "Loading current item details…"
          : "Current item details unavailable."}{job.items != null && " Showing last-known details (not current)."}</p>
      )}
      <ol className="activity-list" aria-label={loadStatus === "loaded" ? "Current item details" : "Last-known item details"}>
        {[...(job.items ?? [])].sort((a, b) => a.source_order - b.source_order).map((item) => (
          <li className="activity-item" key={item.id}>
            <span>Source order: {item.source_order} — {item.title ?? "Untitled"}</span>{" "}
            <span>{item.status} — {item.progress.percent}%</span>
            {item.error && <p className="job-error">{item.error.code}: {item.error.message}</p>}
          </li>
        ))}
      </ol>
    </section>
  );
}
