import { failedStartupTargets, startupReasonLabel, startupTargetLabel, useAppSettings } from "../api/settings";

/**
 * App-wide, persistent startup write-check warning (PRODUCT_SPEC §23).
 * Derived from the last valid settings snapshot only (`data` holds parsed
 * successful reads and survives a failed refetch). A missing snapshot or a
 * first-load network/HTTP/parse error shows nothing — a read error is never
 * a disk failure — and a later read error does not hide an already seen
 * warning. Only a newer successful snapshot replaces it. There is no
 * dismiss and no re-check.
 */
export function StartupStorageWarning() {
  const settings = useAppSettings();
  const failed = failedStartupTargets(settings.data);
  if (failed.length === 0) return null;

  return (
    <div className="error-banner startup-storage-warning" role="alert">
      <p>
        <strong>Storage write check failed at startup.</strong> Downloads that use these
        folders may fail:
      </p>
      <ul>
        {failed.map((entry) => (
          <li key={entry.target}>
            {startupTargetLabel(entry.target)} — {startupReasonLabel(entry.reason)}
          </li>
        ))}
      </ul>
      <p>Fix the folder permissions or mounts, then restart the application.</p>
    </div>
  );
}
