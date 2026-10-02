import type { NormalizedAnalysis, NormalizedFormat } from "../../api/analysis";
import { formatFileSize, formatDuration } from "./format";
import { platformLabel } from "./platforms";

interface AnalysisResultProps {
  result: NormalizedAnalysis;
  selectedVariantId: string | null;
  isCreatingJob: boolean;
  onSelectVariant: (variantId: string) => void;
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
  const parts: string[] = [];
  const resolution = format.resolution_label ?? (format.width && format.height ? `${format.width}x${format.height}` : null);
  if (resolution) parts.push(resolution);
  if (format.ext) parts.push(format.ext.toUpperCase());
  const codecs = [format.video_codec, format.audio_codec].filter(Boolean).join("/");
  if (codecs) parts.push(codecs);
  if (format.fps) parts.push(`${Math.round(format.fps)} fps`);
  const size = sizeLabel(format);
  if (size) parts.push(size);
  return parts.join(" · ");
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

function kindBadge(kind: NormalizedFormat["kind"]): string {
  if (kind === "muxed") return "video + audio";
  if (kind === "video_only") return "video only";
  return "audio";
}

export function AnalysisResult({
  result,
  selectedVariantId,
  isCreatingJob,
  onSelectVariant,
  onDownload,
  onUseDetectedPlatform,
}: AnalysisResultProps) {
  const duration = formatDuration(result.duration_seconds);
  const videoFormats = result.formats.filter((format) => format.kind !== "audio_only");
  const audioFormats = result.formats.filter((format) => format.kind === "audio_only");

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

          {result.collection && (
            <section className="collection-preview">
              <h3>
                {result.collection.total_items} items
              </h3>
              <ul>
                {result.collection.preview_items.map((item) => (
                  <li key={item.source_id ?? item.position}>
                    <span className="item-position">
                      {String(item.position ?? 0).padStart(2, "0")}
                    </span>
                    <span className="item-title">{item.title ?? "Untitled"}</span>
                  </li>
                ))}
              </ul>
              {result.collection.truncated && (
                <p className="muted-note">Only a preview is shown.</p>
              )}
            </section>
          )}
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
                      <span className="badge badge-kind">{kindBadge(format.kind)}</span>
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
              disabled={selectedVariantId == null || isCreatingJob}
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
