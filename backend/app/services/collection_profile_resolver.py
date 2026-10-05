"""Pure native child selection; no downloading, conversion or public payload."""

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.domain.analysis import MediaFormatKind, NormalizedFormat
from app.domain.jobs import CollectionProfile, JobError, JobErrorCode
from app.services.analysis_normalizer import normalize_source_formats


@dataclass(frozen=True, slots=True)
class NativeChildPlan:
    selected: NormalizedFormat
    companion: NormalizedFormat | None = None


def resolve_native_child(
    source: dict[str, Any], *, mode: str, profile: CollectionProfile | None = None
) -> NativeChildPlan:
    """Dimensions/FPS first for video; extractor preference breaks remaining ties.

    yt-dlp orders source formats worst-to-best. Audio follows that source
    preference, not cross-codec bitrate arithmetic. A ceiling requires known
    height. Video-only streams require an actual audio-only companion.
    """
    try:
        preference = CollectionProfile.model_validate(
            profile.model_dump() if profile is not None else {}
        )
    except ValidationError as exc:
        raise JobError(JobErrorCode.INVALID_PROFILE) from exc
    if mode not in {"audio", "video"} or (
        mode == "audio" and preference.max_height is not None
    ):
        raise JobError(JobErrorCode.INVALID_PROFILE)
    if source.get("_type") in ("playlist", "multi_video") or "entries" in source:
        raise JobError(JobErrorCode.MALFORMED_OUTPUT)
    try:
        formats = normalize_source_formats(source.get("formats"))
    except (ValueError, OverflowError) as exc:
        raise JobError(JobErrorCode.MALFORMED_OUTPUT) from exc
    if not formats:
        raise JobError(JobErrorCode.MALFORMED_OUTPUT)
    audio = next(
        (fmt for fmt in reversed(formats) if fmt.kind == MediaFormatKind.AUDIO_ONLY), None
    )
    if mode == "audio":
        if audio is None:
            raise JobError(JobErrorCode.INVALID_VARIANT)
        return NativeChildPlan(audio)
    candidates = [
        (index, fmt) for index, fmt in enumerate(formats)
        if (fmt.kind == MediaFormatKind.MUXED or (
            fmt.kind == MediaFormatKind.VIDEO_ONLY and audio is not None
        )) and (preference.max_height is None or (
            fmt.height is not None and fmt.height <= preference.max_height
        ))
    ]
    if not candidates:
        raise JobError(JobErrorCode.INVALID_VARIANT)
    _, selected = max(candidates, key=lambda item: (
        item[1].height or 0, item[1].width or 0, item[1].fps or 0, item[0]
    ))
    return NativeChildPlan(
        selected, audio if selected.kind == MediaFormatKind.VIDEO_ONLY else None
    )
