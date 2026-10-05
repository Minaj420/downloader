import { useMutation } from "@tanstack/react-query";
import { apiPost } from "./client";

export type FormatKind = "video_only" | "audio_only" | "muxed";
export type AnalysisContentType = "single" | "collection";

export interface NormalizedFormat {
  variant_id: string;
  format_id: string;
  /** Application-level display label for the primary choices list (video). */
  label: string | null;
  kind: FormatKind;
  ext: string | null;
  resolution_label: string | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  video_codec: string | null;
  audio_codec: string | null;
  bitrate_kbps: number | null;
  filesize_bytes: number | null;
  filesize_approx_bytes: number | null;
}

export interface CollectionItemPreview {
  source_id: string | null;
  url: string | null;
  title: string | null;
  duration_seconds: number | null;
  position: number | null;
}

/**
 * Snapshot-scoped selectable entry. `item_id` is the only stable identity:
 * source ids may be null or duplicated, so they are never used as keys or
 * selection references. `source_order` keeps the raw source slot order and
 * `position` stays plain source metadata.
 */
export interface CollectionSelectionItem extends CollectionItemPreview {
  item_id: string;
  source_order: number;
  selectable: boolean;
  unavailable_reason: "invalid_url" | "unavailable" | null;
}

export type CollectionSelectionScope = "analysis_window" | "legacy_preview";

export interface CollectionInfo {
  title: string | null;
  /** Lower bound unless `total_items_known` is true. */
  total_items: number;
  preview_items: CollectionItemPreview[];
  truncated: boolean;
  /** Selectable snapshot entries; legacy previews keep this list empty. */
  items: CollectionSelectionItem[];
  selection_scope: CollectionSelectionScope;
  /** Bounded first-N analysis window backing the selectable items. */
  analysis_window_limit: number;
  analyzed_items: number;
  selectable_items: number;
  /** v0.1 hard cap of selected items per job, enforced by backend and UI. */
  selected_item_limit: number;
  total_items_known: boolean;
  /** True when the selectable window does not cover the whole source. */
  selection_truncated: boolean;
  selection_limit_message: string;
}

export interface NormalizedAnalysis {
  analysis_id: string;
  original_url: string;
  /** Raw source media id (yt-dlp `id`) when the source provided one. */
  source_id: string | null;
  detected_platform: string;
  detected_platform_label: string;
  extractor: string | null;
  title: string | null;
  uploader: string | null;
  duration_seconds: number | null;
  thumbnail_url: string | null;
  content_type: AnalysisContentType;
  formats: NormalizedFormat[];
  collection: CollectionInfo | null;
  platform_hint: string | null;
  platform_mismatch: boolean;
}

export interface AnalyzeRequest {
  url: string;
  platform_hint: string | null;
}

export function useAnalyzeUrl() {
  return useMutation({
    mutationFn: (request: AnalyzeRequest) =>
      apiPost<NormalizedAnalysis, AnalyzeRequest>("/api/v1/analysis", request),
  });
}
