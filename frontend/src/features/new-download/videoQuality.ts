import type { NormalizedFormat } from "../../api/analysis";
import { formatFileSize } from "./format";

/** Standard short-edge ceilings a single video may offer (PRODUCT_SPEC §9). */
export const STANDARD_VIDEO_CEILINGS = [144, 240, 360, 480, 720, 1080, 1440, 2160] as const;

/** Backend's synthetic Best Available clone; never part of the ceiling pool. */
export const BEST_VIDEO_VARIANT_ID = "video-best";

export const BEST_VIDEO_QUALITY_KEY = "best";

export interface VideoQualityOption {
  /** Radio identity: `best` or `ceiling-<X>`; distinct even for one variant. */
  key: string;
  /** Standard short-edge ceiling, or null for Best Available. */
  ceiling: number | null;
  label: string;
  /** Presentation split of `label` (`Up to` + `1080p`); prefix is de-emphasised. */
  labelPrefix: string;
  labelMain: string;
  /** Concrete analysis variant posted when this row is selected. */
  variantId: string;
  /** `W×H` of the representative, only when both dimensions are reliable. */
  sourceSize: string | null;
  /** Exact or `~` approximate file size of the representative, if known. */
  fileSize: string | null;
}

/** Exact size, else `~` approximate size, else null (never a guess). */
export function fileSizeLabel(format: NormalizedFormat): string | null {
  if (format.filesize_bytes != null) return formatFileSize(format.filesize_bytes);
  const approx = formatFileSize(format.filesize_approx_bytes);
  return approx ? `~${approx}` : null;
}

function positive(value: number | null): value is number {
  return value != null && Number.isFinite(value) && value > 0;
}

/**
 * Safe upper bound of a format's short edge from its numeric dimensions only
 * (never parsed from label text): the smaller known positive dimension. With
 * one known dimension the short edge cannot exceed it; with none, null.
 */
export function shortEdgeBound(format: NormalizedFormat): number | null {
  const known = [format.width, format.height].filter(positive);
  return known.length === 0 ? null : Math.min(...known);
}

/** Exact source size, only when both dimensions are known; never a guess. */
export function sourceSizeLabel(format: NormalizedFormat): string | null {
  return positive(format.width) && positive(format.height)
    ? `${format.width}×${format.height}`
    : null;
}

/**
 * Best Available plus the standard short-edge ceilings the analysis can
 * support. A ceiling X is offered only when some concrete (non-clone) format
 * has a short-edge bound <= X and X does not exceed the smallest standard
 * ceiling covering the highest known bound (capped at 2160). Each ceiling
 * picks the fitting format with the largest bound; ties keep backend order.
 * Rows are ordered Best Available, then ceilings high to low.
 */
export function buildVideoQualityOptions(
  formats: readonly NormalizedFormat[],
): VideoQualityOption[] {
  const videos = formats.filter((format) => format.kind !== "audio_only");
  const best = videos.find((format) => format.variant_id === BEST_VIDEO_VARIANT_ID) ?? videos[0];
  if (best == null) return [];

  const options: VideoQualityOption[] = [
    {
      key: BEST_VIDEO_QUALITY_KEY,
      ceiling: null,
      label: "Best Available",
      labelPrefix: "",
      labelMain: "Best Available",
      variantId: best.variant_id,
      sourceSize: sourceSizeLabel(best),
      fileSize: fileSizeLabel(best),
    },
  ];

  const pool = videos
    .filter((format) => format.variant_id !== BEST_VIDEO_VARIANT_ID)
    .map((format) => ({ format, bound: shortEdgeBound(format) }))
    .filter((entry): entry is { format: NormalizedFormat; bound: number } => entry.bound != null);
  if (pool.length === 0) return options;

  const highest = Math.max(...pool.map((entry) => entry.bound));
  const top = STANDARD_VIDEO_CEILINGS.find((ceiling) => ceiling >= highest) ?? 2160;
  for (const ceiling of [...STANDARD_VIDEO_CEILINGS].reverse()) {
    if (ceiling > top) continue;
    let representative: (typeof pool)[number] | null = null;
    for (const entry of pool) {
      if (entry.bound <= ceiling && (representative == null || entry.bound > representative.bound)) {
        representative = entry;
      }
    }
    if (representative == null) continue;
    options.push({
      key: `ceiling-${ceiling}`,
      ceiling,
      label: `Up to ${ceiling}p`,
      labelPrefix: "Up to",
      labelMain: `${ceiling}p`,
      variantId: representative.format.variant_id,
      sourceSize: sourceSizeLabel(representative.format),
      fileSize: fileSizeLabel(representative.format),
    });
  }
  return options;
}
