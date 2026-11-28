import type { ReactNode } from "react";
import type { NormalizedAnalysis, NormalizedFormat } from "../../api/analysis";
import type { CollectionMode, OutputMediaKind, OutputProfile } from "../../api/jobs";
import { formatDuration } from "./format";
import { platformIconUrl, platformLabel } from "./platforms";
import { CollectionSelection } from "./CollectionSelection";
import { OutputProfileSelect } from "./OutputProfileSelect";
import { fileSizeLabel, type VideoQualityOption } from "./videoQuality";

interface AnalysisResultProps {
  result: NormalizedAnalysis;
  selectedVariantId: string | null;
  /** Single-video rows: Best Available plus source-supported short-edge ceilings. */
  videoQualityOptions: readonly VideoQualityOption[];
  /** Selected quality row key; rows are separate identities even for one variant. */
  selectedVideoQualityKey: string | null;
  onSelectVideoQuality: (key: string) => void;
  /** Snapshot-scoped collection selection; empty for single media. */
  selectedCollectionItemIds: readonly string[];
  /** Explicit collection-level mode; null until the user chooses one. */
  collectionMode: CollectionMode | null;
  /** Media kind the output choice applies to; null hides the output group. */
  outputKind: OutputMediaKind | null;
  outputProfile: OutputProfile;
  /** Optional collection control placed after Audio / Video, before Output format. */
  collectionQualityControl?: ReactNode;
  isCreatingJob: boolean;
  /** A pending re-analysis must not post the stale snapshot. */
  isAnalyzing: boolean;
  onSelectVariant: (variantId: string) => void;
  onToggleCollectionItem: (itemId: string) => void;
  onSelectAllCollectionItems: () => void;
  onDeselectAllCollectionItems: () => void;
  onSelectCollectionMode: (mode: CollectionMode) => void;
  onSelectOutputProfile: (profile: OutputProfile) => void;
  onDownload: () => void;
  onUseDetectedPlatform: (platform: string) => void;
}

const sizeLabel = fileSizeLabel;

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
  videoQualityOptions,
  selectedVideoQualityKey,
  onSelectVideoQuality,
  selectedCollectionItemIds,
  collectionMode,
  outputKind,
  outputProfile,
  collectionQualityControl,
  isCreatingJob,
  isAnalyzing,
  onSelectVariant,
  onToggleCollectionItem,
  onSelectAllCollectionItems,
  onDeselectAllCollectionItems,
  onSelectCollectionMode,
  onSelectOutputProfile,
  onDownload,
  onUseDetectedPlatform,
}: AnalysisResultProps) {
  const duration = formatDuration(result.duration_seconds);
  const platformIcon = platformIconUrl(result.detected_platform);
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
                <span className="badge badge-platform">
                  {platformIcon != null && (
                    <img className="platform-icon" src={platformIcon} alt="" aria-hidden="true" />
                  )}
                  {result.detected_platform_label}
                </span>
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
              Source formats and sizes come from the analysis; the output
              format is chosen separately.
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
              {collectionQualityControl}
              {outputKind != null && (
                <OutputProfileSelect
                  kind={outputKind}
                  value={outputProfile}
                  disabled={selectionPending}
                  onChange={onSelectOutputProfile}
                />
              )}
              <p className="muted-note">
                One output format applies to the whole collection. Original
                keeps the best native result each item's own source offers, so
                containers and codecs may differ between items. Source video
                quality is never upgraded or upscaled.
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
              {videoQualityOptions.length > 0 && (
                <section
                  className="format-group"
                  role="group"
                  aria-labelledby="video-formats-heading"
                >
                  <h3 id="video-formats-heading">Video formats</h3>
                  <ul>
                    {videoQualityOptions.map((option) => {
                      const checked = selectedVideoQualityKey === option.key;
                      const sizeId = `quality-${option.key}-size`;
                      const sourceId = `quality-${option.key}-source`;
                      const showSource = checked && option.sourceSize != null;
                      const describedBy = [
                        option.fileSize != null ? sizeId : "",
                        showSource ? sourceId : "",
                      ].join(" ").trim();
                      return (
                        <li key={option.key} className="format-item">
                          <label className="format-option">
                            <input
                              type="radio"
                              name="variant"
                              value={option.key}
                              checked={checked}
                              disabled={selectionPending}
                              aria-describedby={describedBy.length > 0 ? describedBy : undefined}
                              onChange={() => onSelectVideoQuality(option.key)}
                            />
                            <span className="format-label">
                              {option.labelPrefix && (
                                <>
                                  <span className="quality-prefix">{option.labelPrefix}</span>{" "}
                                </>
                              )}
                              {option.labelMain}
                            </span>
                          </label>
                          {option.fileSize != null && (
                            <span className="quality-detail" id={sizeId}>{option.fileSize}</span>
                          )}
                          {showSource && (
                            <span className="quality-detail" id={sourceId}>
                              Source {option.sourceSize}
                            </span>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              )}

              {audioFormats.length > 0 && (
                <section
                  className="format-group"
                  role="group"
                  aria-labelledby="audio-formats-heading"
                >
                  <h3 id="audio-formats-heading">Audio formats</h3>
                  <ul>
                    {audioFormats.map((format) => (
                      <li key={format.variant_id} className="format-item">
                        <label className="format-option">
                          <input
                            type="radio"
                            name="variant"
                            value={format.variant_id}
                            checked={selectedVariantId === format.variant_id}
                            disabled={selectionPending}
                            onChange={() => onSelectVariant(format.variant_id)}
                          />
                          <span className="format-label">{audioLabel(format)}</span>
                        </label>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {outputKind != null && (
                <OutputProfileSelect
                  kind={outputKind}
                  value={outputProfile}
                  disabled={selectionPending}
                  onChange={onSelectOutputProfile}
                />
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
