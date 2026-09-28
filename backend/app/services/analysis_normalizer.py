"""Normalizes raw yt-dlp info dicts into application-level models.

Quality information is only ever taken from real source data: bitrates are
reported when the source provides them, sizes come from yt-dlp fields, and no
artificial quality upgrades are invented (PRODUCT_SPEC.md §2.1).
"""

from typing import Any

from app.domain.analysis import (
    CollectionInfo,
    CollectionItem,
    MediaFormatKind,
    NormalizedAnalysis,
    NormalizedFormat,
)
from app.domain.platforms import (
    detect_platform,
    has_platform_mismatch,
    platform_label,
)

PREVIEW_ITEM_LIMIT = 25

_CODEC_LABELS = {
    "avc1": "h264",
    "avc3": "h264",
    "mp4a": "aac",
    "vp09": "vp9",
    "vp9": "vp9",
    "vp8": "vp8",
    "av01": "av1",
    "hev1": "hevc",
    "hvc1": "hevc",
    "h263": "h263",
    "flac": "flac",
    "opus": "opus",
    "vorbis": "vorbis",
    "mp3": "mp3",
    "ac-3": "ac3",
    "ec-3": "eac3",
    "dtsc": "dts",
}


def _optional_number(value: object) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _optional_int(value: object) -> int | None:
    number = _optional_number(value)
    return int(number) if number is not None else None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _codec_label(value: str | None) -> str | None:
    if value is None:
        return None
    base = value.split(".")[0].strip().lower()
    return _CODEC_LABELS.get(base, base)


def _codec_value(value: object) -> str | None:
    if isinstance(value, str) and value and value != "none":
        return value
    return None


def _best_size(entry: dict[str, Any]) -> tuple[int | None, int | None]:
    exact = _optional_int(entry.get("filesize"))
    approximate = _optional_int(entry.get("filesize_approx"))
    return exact, approximate


def _variant_id(
    *,
    kind: MediaFormatKind,
    height: int | None,
    width: int | None,
    video_codec: str | None,
    audio_codec: str | None,
    bitrate: float | None,
    ext: str | None,
) -> str:
    """Deterministic application-level format identifier (ARCHITECTURE.md §10).

    Derived from the same properties used for deduplication, so variant ids
    stay unique within an analysis.
    """
    if kind is MediaFormatKind.AUDIO_ONLY:
        codec = audio_codec or ext or "unknown"
        if bitrate:
            return f"audio-{codec}-{round(bitrate)}"
        return f"audio-{codec}-{ext or 'unknown'}"
    resolution = height or width or 0
    return "-".join(
        [
            "video",
            str(resolution) if resolution else "unknown",
            kind.value,
            video_codec or "unknown",
            ext or "unknown",
        ]
    )


def _parse_format(entry: Any) -> NormalizedFormat | None:
    if not isinstance(entry, dict):
        return None
    if _optional_str(entry.get("ext")) == "mhtml":
        return None

    video_codec_raw = _codec_value(entry.get("vcodec"))
    audio_codec_raw = _codec_value(entry.get("acodec"))
    has_video = video_codec_raw is not None
    has_audio = audio_codec_raw is not None
    if not has_video and not has_audio:
        return None

    if has_video and has_audio:
        kind = MediaFormatKind.MUXED
    elif has_video:
        kind = MediaFormatKind.VIDEO_ONLY
    else:
        kind = MediaFormatKind.AUDIO_ONLY

    height = _optional_int(entry.get("height"))
    width = _optional_int(entry.get("width"))
    resolution = _optional_str(entry.get("resolution"))
    resolution_label = f"{height}p" if height is not None else resolution

    bitrate: float | None = None
    if kind is MediaFormatKind.AUDIO_ONLY:
        bitrate = _optional_number(entry.get("abr")) or _optional_number(
            entry.get("tbr")
        )

    exact_size, approximate_size = _best_size(entry)
    format_id = _optional_str(entry.get("format_id"))
    if format_id is None:
        return None

    variant_id = _variant_id(
        kind=kind,
        height=height,
        width=width,
        video_codec=_codec_label(video_codec_raw) if has_video else None,
        audio_codec=_codec_label(audio_codec_raw) if has_audio else None,
        bitrate=bitrate,
        ext=_optional_str(entry.get("ext")),
    )

    return NormalizedFormat(
        variant_id=variant_id,
        format_id=format_id,
        kind=kind,
        ext=_optional_str(entry.get("ext")),
        resolution_label=resolution_label,
        width=width,
        height=height,
        fps=_optional_number(entry.get("fps")),
        video_codec=_codec_label(video_codec_raw) if has_video else None,
        audio_codec=_codec_label(audio_codec_raw) if has_audio else None,
        bitrate_kbps=bitrate,
        filesize_bytes=exact_size,
        filesize_approx_bytes=approximate_size,
    )


def _entry_score(entry: dict[str, Any]) -> float:
    """Quality score from real source data: bitrate or size, never invented."""
    return (
        _optional_number(entry.get("tbr"))
        or _optional_number(entry.get("abr"))
        or _optional_number(entry.get("filesize"))
        or _optional_number(entry.get("filesize_approx"))
        or 0.0
    )


def _deduplicate(
    parsed: list[tuple[float, NormalizedFormat]],
) -> list[NormalizedFormat]:
    best_by_key: dict[tuple[Any, ...], tuple[float, NormalizedFormat]] = {}
    for score, entry_format in parsed:
        key: tuple[Any, ...]
        if entry_format.kind is MediaFormatKind.AUDIO_ONLY:
            key = (
                entry_format.ext,
                entry_format.audio_codec,
                round(entry_format.bitrate_kbps or 0),
            )
        else:
            key = (
                entry_format.height or 0,
                entry_format.kind,
                entry_format.video_codec,
                entry_format.ext,
            )
        current = best_by_key.get(key)
        if current is None or score >= current[0]:
            best_by_key[key] = (score, entry_format)

    ranked = list(best_by_key.values())
    videos = [
        entry_format
        for _, entry_format in ranked
        if entry_format.kind is not MediaFormatKind.AUDIO_ONLY
    ]
    audios = [
        entry_format
        for _, entry_format in ranked
        if entry_format.kind is MediaFormatKind.AUDIO_ONLY
    ]
    video_scores = {
        entry_format.format_id: score for score, entry_format in ranked
    }
    videos.sort(
        key=lambda entry_format: (
            -(entry_format.height or 0),
            -video_scores[entry_format.format_id],
        )
    )
    audios.sort(
        key=lambda entry_format: (
            -(entry_format.bitrate_kbps or 0),
            entry_format.ext or "",
        )
    )
    return videos + audios


def normalize_formats(raw_formats: object) -> list[NormalizedFormat]:
    if not isinstance(raw_formats, list):
        return []
    parsed: list[tuple[float, NormalizedFormat]] = []
    for entry in raw_formats:
        entry_format = _parse_format(entry)
        if entry_format is not None and isinstance(entry, dict):
            parsed.append((_entry_score(entry), entry_format))
    return _deduplicate(parsed)


def normalize_collection(raw_info: dict[str, Any]) -> CollectionInfo | None:
    entries = raw_info.get("entries")
    if not isinstance(entries, list):
        return None
    entries = [entry for entry in entries if isinstance(entry, dict)]
    if not entries and raw_info.get("playlist_count") is None:
        return None

    preview: list[CollectionItem] = []
    for index, entry in enumerate(entries[:PREVIEW_ITEM_LIMIT]):
        preview.append(
            CollectionItem(
                source_id=_optional_str(entry.get("id")),
                url=_optional_str(entry.get("webpage_url")) or _optional_str(
                    entry.get("url")
                ),
                title=_optional_str(entry.get("title")),
                duration_seconds=_optional_number(entry.get("duration")),
                position=_optional_int(entry.get("playlist_index")) or index + 1,
            )
        )

    raw_count = _optional_int(raw_info.get("playlist_count"))
    total_items = max(raw_count or 0, len(entries))
    return CollectionInfo(
        title=_optional_str(raw_info.get("title")),
        total_items=total_items,
        preview_items=preview,
        truncated=total_items > len(preview),
    )


def _first_thumbnail(raw_info: dict[str, Any]) -> str | None:
    thumbnail = _optional_str(raw_info.get("thumbnail"))
    if thumbnail:
        return thumbnail
    thumbnails = raw_info.get("thumbnails")
    if isinstance(thumbnails, list) and thumbnails:
        last = thumbnails[-1]
        if isinstance(last, dict):
            return _optional_str(last.get("url"))
    return None


def build_normalized_analysis(
    raw_info: dict[str, Any],
    *,
    analysis_id: str,
    original_url: str,
    platform_hint: str | None,
) -> NormalizedAnalysis:
    detected = detect_platform(raw_info, original_url)
    is_collection = raw_info.get("_type") == "playlist"
    title = _optional_str(raw_info.get("title"))

    return NormalizedAnalysis(
        analysis_id=analysis_id,
        original_url=original_url,
        detected_platform=detected.value,
        detected_platform_label=platform_label(detected.value),
        extractor=_optional_str(raw_info.get("extractor_key"))
        or _optional_str(raw_info.get("extractor")),
        title=title,
        uploader=_optional_str(raw_info.get("uploader"))
        or _optional_str(raw_info.get("channel"))
        or _optional_str(raw_info.get("uploader_id")),
        duration_seconds=_optional_number(raw_info.get("duration")),
        thumbnail_url=_first_thumbnail(raw_info),
        content_type="collection" if is_collection else "single",
        formats=[] if is_collection else normalize_formats(raw_info.get("formats")),
        collection=normalize_collection(raw_info) if is_collection else None,
        platform_hint=platform_hint,
        platform_mismatch=has_platform_mismatch(platform_hint, detected),
    )
