"""Normalizes raw yt-dlp info dicts into application-level models.

Quality information is only ever taken from real source data: bitrates are
reported when the source provides them, sizes come from yt-dlp fields, and no
artificial quality upgrades are invented (PRODUCT_SPEC.md §2.1).

Video choices are concise application-level variants (PRODUCT_SPEC.md §7/§9,
ARCHITECTURE.md §10): one ``Best Available`` option plus exactly one option
per real source resolution. Multiple codec/container/kind streams at the same
resolution collapse to one deterministic source-derived representative that
still carries a real raw yt-dlp ``format_id`` for job execution.
"""

import hashlib
import re
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

BEST_VIDEO_VARIANT_ID = "video-best"
BEST_VIDEO_LABEL = "Best Available"

_RESOLUTION_WIDTH_X_HEIGHT = re.compile(r"^(\d+)\s*x\s*(\d+)$")
_RESOLUTION_HEIGHT_P = re.compile(r"^(\d+)\s*p$")
# Labels that carry no real resolution information.
_MEANINGLESS_RESOLUTION_LABELS = {"unknown", "none"}

# Cosmetic bound for the readable fragment of label-derived variant ids;
# uniqueness never depends on it (the full digest carries the identity).
_LABEL_SLUG_MAX_LENGTH = 24
# Domain separation so label digests can never be confused with other uses.
_LABEL_DIGEST_DOMAIN = b"downloader/video-resolution-label/v1"

# Audio collision groups (same base id, distinct lossless identities) get a
# fixed-namespace id whose uniqueness comes from the full digest over the
# typed identity; codec/ext fragments are cosmetic and individually bounded.
_AUDIO_ID_DISAMBIGUATION_DOMAIN = b"downloader/audio-variant-id/v1"
_AUDIO_COLLISION_PREFIX = "audio-alt"
_AUDIO_COLLISION_SLUG_MAX_LENGTH = 8
# audio-alt- (10) + 8 + "-" + 8 + "-" + 64 hex digest = 92 chars, <= 128 by
# construction regardless of source codec/ext length.
_AUDIO_COLLISION_MAX_ID_LENGTH = 92
# Public variant ids must fit the API request schema (api/v1/jobs.py
# ``Field(max_length=128)``) and the ``String(128)`` DB column; keep aligned.
_VARIANT_ID_MAX_LENGTH = 128

# Axis-aware identity ordering: heights, then width-only, then exact
# labels, then the unknown sentinel.
_AXIS_ORDER = {"height": 0, "width": 1, "label": 2, "unknown": 3}

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


def _source_resolution(value: object) -> str | None:
    """Non-empty resolution label that actually carries information."""
    label = _optional_str(value)
    if label is None or label.strip().lower() in _MEANINGLESS_RESOLUTION_LABELS:
        return None
    return label


def _parse_resolution_label(
    label: str | None,
) -> tuple[int | None, int | None]:
    """Extract ``(height, width)`` from common real source labels.

    Understands ``WIDTHxHEIGHT`` (``1920x1080``) and ``HEIGHTp``
    (``1080p``); anything else is not a quality we may invent dimensions
    for and keeps its source label as the resolution identity.
    """
    if label is None:
        return None, None
    text = label.strip().lower()
    match = _RESOLUTION_WIDTH_X_HEIGHT.fullmatch(text)
    if match:
        return int(match.group(2)), int(match.group(1))
    match = _RESOLUTION_HEIGHT_P.fullmatch(text)
    if match:
        return int(match.group(1)), None
    return None, None


# Lossless, axis-aware resolution identity used as the deduplication key.
# The second element is the height/width number or the exact label string.
ResolutionIdentity = tuple[str, int | str]


def _video_identity(
    *,
    height: int | None,
    width: int | None,
    source_label: str | None,
) -> ResolutionIdentity:
    """Typed resolution identity: axis first, then the exact value.

    ``height`` wins whenever it can be established (explicit or safely
    parsed from ``WIDTHxHEIGHT`` / ``HEIGHTp``), which preserves the proven
    equivalence of those three spellings. ``width`` is its own axis and is
    never treated as equivalent to a height bucket. Unparseable meaningful
    labels keep their exact string; everything else is the unknown
    sentinel. No lossy normalization participates in identity.
    """
    if height is not None:
        return "height", height
    if width is not None:
        return "width", width
    if source_label:
        return "label", source_label
    return "unknown", ""


def _identity_of(entry_format: NormalizedFormat) -> ResolutionIdentity:
    """Identity of an already-parsed video format.

    Height/width are checked first, so the (possibly normalized) display
    ``resolution_label`` is only consulted for the exact-label axis, where
    it stores the untouched source label verbatim.
    """
    return _video_identity(
        height=entry_format.height,
        width=entry_format.width,
        source_label=entry_format.resolution_label,
    )


def _cosmetic_slug(
    label: str, max_length: int = _LABEL_SLUG_MAX_LENGTH
) -> str:
    """Bounded, ASCII-safe readable fragment; purely cosmetic."""
    slug = (
        re.sub(r"[^a-z0-9]+", "-", label.strip().lower())
        .strip("-")[:max_length]
        .rstrip("-")
    )
    return slug or "res"


def _label_variant_id(exact_label: str) -> str:
    """Public id for an exact-label identity.

    The full stdlib sha256 digest over a domain-separated, unambiguous
    encoding of the exact label carries identity and uniqueness; the slug
    is only human-readable. ``A/B`` and ``A-B`` share a cosmetic slug but
    never an id, and no source label can produce the reserved synthetic
    ``video-best`` id.
    """
    digest = hashlib.sha256(
        _LABEL_DIGEST_DOMAIN + b"\x00" + exact_label.encode("utf-8")
    ).hexdigest()
    return f"video-label-{_cosmetic_slug(exact_label)}-{digest}"


def _video_identity_descriptor(
    identity: ResolutionIdentity,
) -> tuple[str, str | None, str]:
    """(variant_id, resolution_label, label) for a video identity.

    Public id namespaces are disjoint: ``video-1080`` (height),
    ``video-width-1080`` (width-only), ``video-label-<slug>-<digest>``
    (exact label), ``video-unknown``. Labels stay truthful: a lone width
    never implies a vertical resolution.
    """
    axis, value = identity
    if axis == "height":
        height = int(value)
        return f"video-{height}", f"{height}p", f"{height}p"
    if axis == "width":
        width = int(value)
        return f"video-width-{width}", f"{width}px wide", f"{width}px wide"
    if axis == "label":
        exact_label = str(value)
        return _label_variant_id(exact_label), exact_label, exact_label
    return "video-unknown", None, "Unknown"


def _identity_sort_key(identity: ResolutionIdentity) -> tuple[int, int, str]:
    """Typed-identity ordering: heights high→low, width-only high→low,
    exact labels in exact deterministic order, unknown last."""
    axis, value = identity
    order = _AXIS_ORDER[axis]
    if axis in ("height", "width"):
        return order, -int(value), ""
    if axis == "label":
        return order, 0, str(value)
    return order, 0, ""


def _audio_variant_id(
    *,
    audio_codec: str | None,
    bitrate: float | None,
    ext: str | None,
) -> str:
    codec = audio_codec or ext or "unknown"
    if bitrate:
        return f"audio-{codec}-{round(bitrate)}"
    return f"audio-{codec}-{ext or 'unknown'}"


def _audio_base_variant_id(entry_format: NormalizedFormat) -> str:
    return _audio_variant_id(
        audio_codec=entry_format.audio_codec,
        bitrate=entry_format.bitrate_kbps,
        ext=entry_format.ext,
    )


def _audio_collision_variant_id(
    *, ext: str | None, codec: str | None, rounded_bitrate: int
) -> str:
    """Fixed-namespace public id for a member of an audio collision group.

    The base id is deliberately excluded: it embeds the raw source codec,
    which is unbounded. Instead the id is ``audio-alt-`` plus two bounded
    cosmetic slugs plus the full sha256 digest over a domain-separated,
    unambiguous canonical encoding of the complete lossless identity
    ``(ext, codec, rounded bitrate)`` — never input order, Python
    ``hash()``, or a truncated digest. Its length therefore never depends
    on source codec/ext length.
    """
    canonical = b"\x00".join(
        (
            _AUDIO_ID_DISAMBIGUATION_DOMAIN,
            (ext or "").encode("utf-8"),
            (codec or "").encode("utf-8"),
            str(rounded_bitrate).encode("ascii"),
        )
    )
    digest = hashlib.sha256(canonical).hexdigest()
    ext_slug = _cosmetic_slug(
        ext or "", max_length=_AUDIO_COLLISION_SLUG_MAX_LENGTH
    )
    codec_slug = _cosmetic_slug(
        codec or "", max_length=_AUDIO_COLLISION_SLUG_MAX_LENGTH
    )
    collision_id = f"{_AUDIO_COLLISION_PREFIX}-{ext_slug}-{codec_slug}-{digest}"
    assert len(collision_id) <= _AUDIO_COLLISION_MAX_ID_LENGTH
    return collision_id


def _assign_audio_variant_ids(
    representatives: list[NormalizedFormat],
) -> list[NormalizedFormat]:
    """Keep short unambiguous base audio ids; route the rest to bounded ids.

    A base id is kept verbatim only when it is unambiguous AND fits the
    128-char API/DB limit. Collision-group members — and any single-member
    base that embeds a source codec long enough to exceed the limit — get
    the fixed bounded digest id instead, so every offered audio choice
    stays selectable.
    """
    base_ids = [_audio_base_variant_id(f) for f in representatives]
    counts: dict[str, int] = {}
    for base_id in base_ids:
        counts[base_id] = counts.get(base_id, 0) + 1
    assigned: list[NormalizedFormat] = []
    for entry_format, base_id in zip(representatives, base_ids, strict=True):
        if (
            counts[base_id] == 1
            and len(base_id) <= _VARIANT_ID_MAX_LENGTH
        ):
            assigned.append(entry_format)
            continue
        assigned.append(
            entry_format.model_copy(
                update={
                    "variant_id": _audio_collision_variant_id(
                        ext=entry_format.ext,
                        codec=entry_format.audio_codec,
                        rounded_bitrate=round(entry_format.bitrate_kbps or 0),
                    )
                }
            )
        )
    return assigned


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
    resolution = _source_resolution(entry.get("resolution"))
    if height is None or width is None:
        # Fall back to dimensions parsed from the real source label so
        # resolution-only metadata still yields the correct concise choice.
        parsed_height, parsed_width = _parse_resolution_label(resolution)
        height = height if height is not None else parsed_height
        width = width if width is not None else parsed_width

    bitrate: float | None = None
    if kind is MediaFormatKind.AUDIO_ONLY:
        bitrate = _optional_number(entry.get("abr")) or _optional_number(
            entry.get("tbr")
        )

    exact_size, approximate_size = _best_size(entry)
    format_id = _optional_str(entry.get("format_id"))
    if format_id is None:
        return None

    if kind is MediaFormatKind.AUDIO_ONLY:
        variant_id = _audio_variant_id(
            audio_codec=_codec_label(audio_codec_raw) if has_audio else None,
            bitrate=bitrate,
            ext=_optional_str(entry.get("ext")),
        )
        resolution_label = None
        # Audio keeps its existing source-derived frontend listing.
        label = None
    else:
        identity = _video_identity(
            height=height, width=width, source_label=resolution
        )
        variant_id, resolution_label, label = _video_identity_descriptor(identity)

    return NormalizedFormat(
        variant_id=variant_id,
        format_id=format_id,
        label=label,
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


def _representative_rank(
    item: tuple[float, NormalizedFormat],
) -> tuple[float, int, str]:
    """Deterministic per-resolution winner: real score, then exact size, then id.

    The tie-breaks keep the representative stable even if yt-dlp reorders
    equally scored streams; nothing is invented from outside the source.
    """
    score, entry_format = item
    return (-score, -(entry_format.filesize_bytes or 0), entry_format.format_id)


def _best_video_label(entry_format: NormalizedFormat) -> str:
    if entry_format.resolution_label:
        return f"{BEST_VIDEO_LABEL} ({entry_format.resolution_label})"
    return BEST_VIDEO_LABEL


def _ensure_unique_variant_ids(formats: list[NormalizedFormat]) -> None:
    """Guarantee the public-id scheme never emits a duplicate variant id."""
    seen: set[str] = set()
    for entry_format in formats:
        if entry_format.variant_id in seen:
            raise RuntimeError(
                f"duplicate variant id: {entry_format.variant_id}"
            )
        seen.add(entry_format.variant_id)


def _deduplicate(
    parsed: list[tuple[float, NormalizedFormat]],
) -> list[NormalizedFormat]:
    audio_by_identity: dict[
        tuple[str | None, str | None, int], list[tuple[float, NormalizedFormat]]
    ] = {}
    video_by_identity: dict[ResolutionIdentity, list[tuple[float, NormalizedFormat]]] = {}
    for score, entry_format in parsed:
        if entry_format.kind is MediaFormatKind.AUDIO_ONLY:
            audio_identity = (
                entry_format.ext,
                entry_format.audio_codec,
                round(entry_format.bitrate_kbps or 0),
            )
            audio_by_identity.setdefault(audio_identity, []).append(
                (score, entry_format)
            )
        else:
            identity = _identity_of(entry_format)
            video_by_identity.setdefault(identity, []).append((score, entry_format))

    has_audio_companion = bool(audio_by_identity)

    def _representative(
        candidates: list[tuple[float, NormalizedFormat]],
    ) -> tuple[float, NormalizedFormat]:
        # Executability guard: without any source audio companion, a
        # video-only representative would download silent output, so a real
        # muxed candidate wins at that resolution regardless of score. With
        # a companion available, quality-based selection keeps the merge
        # path; sources with only video-only streams keep their choice.
        if not has_audio_companion:
            muxed = [
                item
                for item in candidates
                if item[1].kind is MediaFormatKind.MUXED
            ]
            if muxed:
                candidates = muxed
        return min(candidates, key=_representative_rank)

    representatives = sorted(
        (_representative(candidates) for candidates in video_by_identity.values()),
        key=lambda item: (
            *_identity_sort_key(_identity_of(item[1])),
            -item[0],
            item[1].format_id,
        ),
    )

    # Assign public ids from the lossless identities after bucket selection;
    # ``video-best`` is reserved for the Best Available clone.
    videos: list[NormalizedFormat] = []
    if representatives:
        _, best = representatives[0]
        videos.append(
            best.model_copy(
                update={
                    "variant_id": BEST_VIDEO_VARIANT_ID,
                    "label": _best_video_label(best),
                }
            )
        )
    for _, representative in representatives:
        descriptor_id, _, display_label = _video_identity_descriptor(
            _identity_of(representative)
        )
        videos.append(
            representative.model_copy(
                update={"variant_id": descriptor_id, "label": display_label}
            )
        )

    # One deterministic representative per exact audio identity (rank:
    # score, exact size, then raw id — order-stable), ordered by the
    # existing source-derived key with a codec tie-break so listing order
    # is fully deterministic too.
    audio_representatives = [
        min(candidates, key=_representative_rank)
        for candidates in audio_by_identity.values()
    ]
    audio_representatives.sort(
        key=lambda item: (
            -(item[1].bitrate_kbps or 0),
            item[1].ext or "",
            item[1].audio_codec or "",
        )
    )
    audios = _assign_audio_variant_ids(
        [entry_format for _, entry_format in audio_representatives]
    )
    result = videos + audios
    _ensure_unique_variant_ids(result)
    return result


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
        source_id=_optional_str(raw_info.get("id")),
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
