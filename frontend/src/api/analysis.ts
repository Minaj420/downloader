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

export interface CollectionInfo {
  title: string | null;
  total_items: number;
  preview_items: CollectionItemPreview[];
  truncated: boolean;
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
