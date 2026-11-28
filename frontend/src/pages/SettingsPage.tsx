import type { AppSettings, DestinationSettings } from "../api/settings";
import { startupReasonLabel, startupTargetLabel, useAppSettings } from "../api/settings";

/** User-approved line (M7-S1); format/quality are browser-local, not API values. */
const FORMAT_QUALITY_NOTE =
  "Format and quality are remembered in your browser from your last choice; Original on first use.";

function destinationLabel(target: string): string {
  return target === "app_library" ? "App Library" : target;
}

function conflictPolicyLabel(policy: string): string {
  return policy === "skip" ? "Skip Existing" : policy;
}

function destinationState(destination: DestinationSettings): string {
  if (!destination.configured) return "Not configured";
  if (!destination.selectable) return "Configured — not selectable yet";
  return "Configured";
}

function SettingsDetails({ settings }: { settings: AppSettings }) {
  const check = settings.startup_storage_check;
  return (
    <>
      <section className="settings-section" aria-labelledby="settings-downloads">
        <h2 id="settings-downloads">Downloads</h2>
        <dl className="settings-grid">
          <div>
            <dt>Maximum concurrent jobs</dt>
            <dd>{settings.max_concurrent_jobs}</dd>
          </div>
          <div>
            <dt>Default destination</dt>
            <dd>{destinationLabel(settings.default_destination)}</dd>
          </div>
          <div>
            <dt>Existing files</dt>
            <dd>{conflictPolicyLabel(settings.conflict_policy)}</dd>
          </div>
        </dl>
        <p className="muted-note">{FORMAT_QUALITY_NOTE}</p>
      </section>

      <section className="settings-section" aria-labelledby="settings-destinations">
        <h2 id="settings-destinations">Destinations</h2>
        {/* Explicit role: Safari/VoiceOver drops list semantics with list-style: none. */}
        <ul className="settings-list" role="list">
          {settings.destinations.map((destination) => (
            <li key={destination.target}>
              <span className="settings-list-name">{startupTargetLabel(destination.target)}</span>
              <span>{destinationState(destination)}</span>
              {destination.display_path != null && (
                <span className="settings-path">{destination.display_path}</span>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section className="settings-section" aria-labelledby="settings-startup-check">
        <h2 id="settings-startup-check">Startup storage check</h2>
        {check.status === "unavailable" ? (
          <p>No startup check result is available.</p>
        ) : (
          <ul className="settings-list" role="list">
            {check.results.map((entry) => (
              <li key={entry.target}>
                <span className="settings-list-name">{startupTargetLabel(entry.target)}</span>
                <span className={entry.ok ? undefined : "settings-failed"}>
                  {entry.ok ? "Writable at startup" : `Failed — ${startupReasonLabel(entry.reason)}`}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="muted-note">
          The result describes permissions when the application started; it does not
          guarantee later writes. Opening this page does not run a new check.
        </p>
      </section>
    </>
  );
}

/**
 * Read-only Settings (M7-S1; PRODUCT_SPEC §23, ARCHITECTURE §47): effective
 * Compose/environment values, destination configuration and the stored
 * startup write check. Nothing here is editable.
 */
export function SettingsPage() {
  const settings = useAppSettings();

  return (
    <section>
      <h1>Settings</h1>
      <p className="placeholder">
        Read-only. Maximum concurrent jobs and the Plex destinations come from the
        Compose/environment configuration; change them there and restart the application.
        The default destination and existing-file handling are fixed in this version.
      </p>
      {settings.isPending ? (
        <p className="placeholder" role="status">
          Loading settings…
        </p>
      ) : settings.isError ? (
        <div className="error-banner" role="alert">
          <p>Could not load settings. {settings.error.message}</p>
          <button
            type="button"
            className="button-secondary"
            disabled={settings.isFetching}
            onClick={() => void settings.refetch()}
          >
            Retry
          </button>
        </div>
      ) : (
        <SettingsDetails settings={settings.data} />
      )}
    </section>
  );
}
