import {
  AUDIO_OUTPUT_PROFILES,
  VIDEO_OUTPUT_PROFILES,
  type OutputProfile,
} from "../../api/jobs";
import { BEST_VIDEO_QUALITY_KEY, STANDARD_VIDEO_CEILINGS } from "./videoQuality";

/**
 * Browser-local "remember last selection" (PRODUCT_SPEC §6; ARCHITECTURE §47).
 *
 * Only three source-independent choice identifiers are kept: the single video
 * quality row key (`best` / `ceiling-<X>`), the collection video ceiling and
 * the output profile. Never a platform, URL, metadata, analysis/job/item id,
 * extractor variant id, collection mode or item selection. Every storage
 * access is swallowed: blocked, corrupt or unknown data means "no preference".
 */
export const LAST_SELECTION_STORAGE_KEY = "downloader.new-download.last-selection";

/** Fixed collection video short-edge ceilings (PRODUCT_SPEC §9). */
export const COLLECTION_VIDEO_CEILINGS = [2160, 1440, 1080, 720, 480] as const;
export type CollectionVideoCeiling = (typeof COLLECTION_VIDEO_CEILINGS)[number];

export interface LastSelection {
  /** Single video quality row key; null means no remembered choice. */
  videoQuality: string | null;
  /** Collection video ceiling; null means Best Available / none. */
  collectionCeiling: CollectionVideoCeiling | null;
  outputProfile: OutputProfile | null;
}

export const EMPTY_LAST_SELECTION: LastSelection = {
  videoQuality: null,
  collectionCeiling: null,
  outputProfile: null,
};

const SCHEMA_VERSION = 1;
const SCHEMA_KEYS = ["version", "videoQuality", "collectionCeiling", "outputProfile"];
const VALID_QUALITY_KEYS: readonly string[] = [
  BEST_VIDEO_QUALITY_KEY,
  ...STANDARD_VIDEO_CEILINGS.map((ceiling) => `ceiling-${ceiling}`),
];
const VALID_PROFILES: readonly string[] = [
  ...new Set([...AUDIO_OUTPUT_PROFILES, ...VIDEO_OUTPUT_PROFILES]),
];

export function isCollectionVideoCeiling(value: unknown): value is CollectionVideoCeiling {
  return COLLECTION_VIDEO_CEILINGS.some((ceiling) => ceiling === value);
}

/** Closed schema: any unknown key, wrong version or invalid value rejects it all. */
function parseLastSelection(raw: string | null): LastSelection {
  if (raw == null) return EMPTY_LAST_SELECTION;
  try {
    const data: unknown = JSON.parse(raw);
    if (data == null || typeof data !== "object" || Array.isArray(data)) {
      return EMPTY_LAST_SELECTION;
    }
    const record = data as Record<string, unknown>;
    const keys = Object.keys(record);
    if (
      keys.length !== SCHEMA_KEYS.length ||
      !SCHEMA_KEYS.every((key) => keys.includes(key)) ||
      record.version !== SCHEMA_VERSION
    ) {
      return EMPTY_LAST_SELECTION;
    }
    const { videoQuality, collectionCeiling, outputProfile } = record;
    if (
      (videoQuality !== null &&
        !(typeof videoQuality === "string" && VALID_QUALITY_KEYS.includes(videoQuality))) ||
      (collectionCeiling !== null && !isCollectionVideoCeiling(collectionCeiling)) ||
      (outputProfile !== null &&
        !(typeof outputProfile === "string" && VALID_PROFILES.includes(outputProfile)))
    ) {
      return EMPTY_LAST_SELECTION;
    }
    return {
      videoQuality: videoQuality as string | null,
      collectionCeiling: collectionCeiling as CollectionVideoCeiling | null,
      outputProfile: outputProfile as OutputProfile | null,
    };
  } catch {
    return EMPTY_LAST_SELECTION;
  }
}

export function readLastSelection(): LastSelection {
  try {
    return parseLastSelection(window.localStorage.getItem(LAST_SELECTION_STORAGE_KEY));
  } catch {
    return EMPTY_LAST_SELECTION;
  }
}

/** Merge one explicit choice into the stored entry; failures are ignored. */
export function writeLastSelection(patch: Partial<LastSelection>): void {
  try {
    const next = { ...readLastSelection(), ...patch };
    window.localStorage.setItem(
      LAST_SELECTION_STORAGE_KEY,
      JSON.stringify({
        version: SCHEMA_VERSION,
        videoQuality: next.videoQuality,
        collectionCeiling: next.collectionCeiling,
        outputProfile: next.outputProfile,
      }),
    );
  } catch {
    // Blocked or full storage: the UI keeps working with its in-memory choice.
  }
}
