import { useHealth } from "../api/health";

export function BackendStatus() {
  const health = useHealth();
  const online = health.isSuccess && health.data?.status === "ok";
  const pending = health.isPending;
  const label = pending ? "Checking backend…" : online ? "Backend online" : "Backend offline";
  const dotClass = pending ? "" : online ? "online" : "offline";

  return (
    <div className="backend-status" title="GET /api/v1/health">
      <span className={`status-dot ${dotClass}`} aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}
