import type { NormalizedAnalysis, NormalizedFormat } from "../../api/analysis";
import type { CollectionMode } from "../../api/jobs";
import { formatFileSize, formatDuration } from "./format";
import { platformLabel } from "./platforms";
import { CollectionSelection } from "./CollectionSelection";

interface AnalysisResultProps {
  result: NormalizedAnalysis;
  selectedVariantId: string | null;
  /** Snapshot-scoped collection selection; empty for single media. */
  selectedCollectionItemIds: readonly string[];
  /** Explicit collection-level mode; null until the user chooses one. */
  collectionMode: CollectionMode | null;
  isCreatingJob: boolean;
  /** A pending re-analysis must not post the stale snapshot. */
  isAnalyzing: boolean;
  onSelectVariant: (variantId: string) => void;
  onToggleCollectionItem: (itemId: string) => void;
  onSelectAllCollectionItems: () => void;
  onDeselectAllCollectionItems: () => void;
  onSelectCollectionMode: (mode: CollectionMode) => void;
  onDownload: () => void;
  onUseDetectedPlatform: (platform: string) => void;
}

function sizeLabel(format: NormalizedFormat): string | null {
  if (format.filesize_bytes != null) {
    return formatFileSize(format.filesize_bytes);
  }
  const approx = formatFileSize(format.filesize_approx_bytes);
  return approx ? `~${approx}` : null;
}

function videoLabel(format: NormalizedFormat): string {
  // Application-level choice label from the backend analysis (e.g.
  // "Best Available (1080p)", "720p"); low-level codec/container/fps and
  // kind details stay out of the primary video list (PRODUCT_SPEC §9).
  const name = format.label ?? format.resolution_label ?? "Video";
  const size = sizeLabel(format);
  return size ? `${name} · ${size}` : name;
}

function audioLabel(format: NormalizedFormat): string {
  const parts: string[] = [];
  if (format.ext) parts.push(format.ext.toUpperCase());
  if (format.audio_codec) parts.push(format.audio_codec);
  if (format.bitrate_kbps != null) parts.push(`~${Math.round(format.bitrate_kbps)} kbps`);
  const size = sizeLabel(format);
  if (size) parts.push(size);
  return parts.join(" · ");
}

export function AnalysisResult({
  result,
  selectedVariantId,
  selectedCollectionItemIds,
  collectionMode,
  isCreatingJob,
  isAnalyzing,
  onSelectVariant,
  onToggleCollectionItem,
  onSelectAllCollectionItems,
  onDeselectAllCollectionItems,
  onSelectCollectionMode,
  onDownload,
  onUseDetectedPlatform,
}: AnalysisResultProps) {
  const duration = formatDuration(result.duration_seconds);
  const videoFormats = result.formats.filter((format) => format.kind !== "audio_only");
  const audioFormats = result.formats.filter((format) => format.kind === "audio_only");
  const isCollection = result.content_type === "collection";
  const collection = result.collection ?? null;
  // Only the analysis-window snapshot offers real selectable items; legacy
  // stored previews stay read-only and require re-analysis.
  const hasSelectableWindow =
    isCollection && collection != null && collection.selection_scope === "analysis_window";
  const selectionPending = isCreatingJob || isAnalyzing;
  const selectionReady = isCollection
    ? collectionMode != null && selectedCollectionItemIds.length > 0
    : selectedVariantId != null;

  return (
    <article className="analysis-result" aria-label="Analysis result">
      {result.platform_mismatch && result.platform_hint && (
        <div className="platform-mismatch" role="status">
          <p>
            Selected platform is <strong>{platformLabel(result.platform_hint)}</strong>, but this
            URL was detected as <strong>{result.detected_platform_label}</strong>.
          </p>
          <button
            type="button"
            className="button-secondary"
            onClick={() => onUseDetectedPlatform(result.detected_platform)}
          >
            Use {result.detected_platform_label}
          </button>
        </div>
      )}

      {/* Panel grammar (UI_DIRECTION §4): the New Download flow is genuinely
          sequential, so the analysis result continues the numbered plates
          from the Add Media form (01 → 02 → 03). Everything shown is taken
          from the backend analysis result only. */}
      <section className="result-panel" aria-labelledby="media-preview-heading">
        <header className="result-panel-header">
          <span className="result-panel-step" aria-hidden="true">
            02
          </span>
          <h2 id="media-preview-heading" className="result-panel-title">
            Media Preview
          </h2>
        </header>
        <div className="result-panel-body">
          <header className="result-header">
            {result.thumbnail_url && (
              <img
                className="result-thumb"
                src={result.thumbnail_url}
                alt=""
                onError={(event) => {
                  event.currentTarget.style.display = "none";
                }}
              />
            )}
            <div className="result-heading">
              <h3>{result.title ?? "Unknown title"}</h3>
              <div className="result-meta">
                <span className="badge badge-platform">{result.detected_platform_label}</span>
                <span className="badge">
                  {result.content_type === "collection" ? "Playlist" : "Single item"}
                </span>
                {result.uploader && <span className="meta-item">{result.uploader}</span>}
                {duration && <span className="meta-item">{duration}</span>}
              </div>
            </div>
          </header>

          {collection != null &&
            (hasSelectableWindow ? (
              <CollectionSelection
                collection={collection}
                selectedItemIds={selectedCollectionItemIds}
                disabled={selectionPending}
                onToggleItem={onToggleCollectionItem}
                onSelectAll={onSelectAllCollectionItems}
                onDeselectAll={onDeselectAllCollectionItems}
              />
            ) : (
              <section className="collection-preview">
                <h3>
                  {collection.total_items} items
                </h3>
                <ul>
                  {collection.preview_items.map((item) => (
                    <li key={item.source_id ?? item.position}>
                      <span className="item-position">
                        {String(item.position ?? 0).padStart(2, "0")}
                      </span>
                      <span className="item-title">{item.title ?? "Untitled"}</span>
                    </li>
                  ))}
                </ul>
                {collection.truncated && (
                  <p className="muted-note">Only a preview is shown.</p>
                )}
                <p className="muted-note">
                  This analysis predates selectable items. Re-analyze the source
                  to choose items for a collection download.
                </p>
              </section>
            ))}
        </div>
      </section>

      <section className="result-panel" aria-labelledby="format-selection-heading">
        <header className="result-panel-header">
          <span className="result-panel-step" aria-hidden="true">
            03
          </span>
          <div className="result-panel-heading">
            <h2 id="format-selection-heading" className="result-panel-title">
              Format &amp; Destination
            </h2>
            <p className="result-panel-subtitle">
              Format options and sizes come directly from the analyzed source.
            </p>
          </div>
        </header>
        <div className="result-panel-body">
          {hasSelectableWindow && collection != null ? (
            <section className="format-group">
              <h3>Collection output</h3>
              <fieldset className="destination-group">
                <legend>Audio / Video</legend>
                <label className="destination-option">
                  <input
                    type="radio"
                    name="collection-mode"
                    value="audio"
                    checked={collectionMode === "audio"}
                    disabled={selectionPending}
                    onChange={() => onSelectCollectionMode("audio")}
                  />
                  Audio
                </label>
                <label className="destination-option">
                  <input
                    type="radio"
                    name="collection-mode"
                    value="video"
                    checked={collectionMode === "video"}
                    disabled={selectionPending}
                    onChange={() => onSelectCollectionMode("video")}
                  />
                  Video
                </label>
              </fieldset>
              <p className="muted-note">
                One profile applies to the whole collection: Original / Best
                Available. Each item keeps the best native result its own
                source offers, so containers and codecs may differ between
                items. Video quality is never upgraded or upscaled.
              </p>
            </section>
          ) : isCollection ? (
            <section className="format-group">
              <h3>Collection output</h3>
              <p className="muted-note">
                Re-analyze this source to choose items and start a collection
                download.
              </p>
            </section>
          ) : (
            <>
              {videoFormats.length > 0 && (
                <section className="format-group">
                  <h3>Video formats</h3>
                  <ul>
                    {videoFormats.map((format) => (
                      <li key={format.variant_id} className="format-item">
                        <label className="format-option">
                          <input
                            type="radio"
                            name="variant"
                            value={format.variant_id}
                            checked={selectedVariantId === format.variant_id}
                            onChange={() => onSelectVariant(format.variant_id)}
                          />
                          <span className="format-label">{videoLabel(format)}</span>
                        </label>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {audioFormats.length > 0 && (
                <section className="format-group">
                  <h3>Audio formats</h3>
                  <ul>
                    {audioFormats.map((format) => (
                      <li key={format.variant_id} className="format-item">
                        <label className="format-option">
                          <input
                            type="radio"
                            name="variant"
                            value={format.variant_id}
                            checked={selectedVariantId === format.variant_id}
                            onChange={() => onSelectVariant(format.variant_id)}
                          />
                          <span className="format-label">{audioLabel(format)}</span>
                        </label>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}

          <fieldset className="destination-group">
            <legend>Save to</legend>
            <label className="destination-option">
              <input type="radio" name="destination" value="app_library" defaultChecked />
              App Library
            </label>
            <label className="destination-option destination-disabled">
              <input type="radio" name="destination" disabled />
              Plex Music (coming later)
            </label>
            <label className="destination-option destination-disabled">
              <input type="radio" name="destination" disabled />
              Plex Videos (coming later)
            </label>
            <label className="destination-option destination-disabled">
              <input type="radio" name="destination" disabled />
              This device (coming later)
            </label>
          </fieldset>

          <footer className="result-actions">
            <button
              type="button"
              className="button-primary"
              disabled={!selectionReady || selectionPending}
              onClick={onDownload}
            >
              {isCreatingJob ? "Starting…" : "Download"}
            </button>
          </footer>
        </div>
      </section>
    </article>
  );
}
