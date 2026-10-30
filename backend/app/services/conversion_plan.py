"""Pure stream-copy vs. transcode decision for an explicit output profile.

Copy is chosen only on an exact normalized codec match; an unknown or missing
codec always transcodes; MP4 also transcodes denylisted H.264 profiles. WAV and
FLAC are always encoded with their fixed profile. Incompatible source/profile
combinations fail before any media I/O.
"""

from typing import NamedTuple, cast, get_args

from app.domain.analysis import MediaFormatKind, NormalizedFormat
from app.domain.jobs import ORIGINAL_OUTPUT_PROFILE, JobError, JobErrorCode
from app.integrations.ffmpeg import ConversionProfile

# Codec a profile's output carries; also the source codec that allows copy.
_OUTPUT_CODECS: dict[str, str] = {
    "mp3": "mp3",
    "wav": "pcm_s16le",
    "m4a": "aac",
    "opus": "opus",
    "flac": "flac",
    "mp4": "h264",
}
_COPYABLE_AUDIO: dict[str, str] = {"mp3": "mp3", "m4a": "aac", "opus": "opus", "mp4": "aac"}
_MP4_COPYABLE_VIDEO = "h264"
# Known poorly supported H.264 profiles (CAVLC 4:4:4 Intra, High 10, High 4:2:2,
# High 4:4:4 Predictive) are transcoded; missing or other profile_idc copies.
_MP4_TRANSCODE_H264_PROFILES = frozenset({44, 110, 122, 244})


class ConversionPlan(NamedTuple):
    copy_audio: bool
    copy_video: bool
    output_codec: str
    keep_source_bitrate: bool


def resolve_output_profile(stored: str | None) -> ConversionProfile | None:
    """Stored job profile to a conversion target; ``None`` means Original.

    NULL (legacy) and ``original`` keep the source file; a known conversion
    profile is returned as is; anything else fails closed before media I/O.
    """
    if stored is None or stored == ORIGINAL_OUTPUT_PROFILE:
        return None
    if stored in get_args(ConversionProfile):
        return cast(ConversionProfile, stored)
    raise JobError(
        JobErrorCode.INVALID_PROFILE, detail=f"unknown output profile {stored!r}"
    )


def plan_conversion(
    profile: ConversionProfile,
    selected: NormalizedFormat,
    companion: NormalizedFormat | None,
) -> ConversionPlan:
    if profile not in _OUTPUT_CODECS:
        raise ValueError(f"no conversion plan for profile {profile!r}")
    output_codec = _OUTPUT_CODECS[profile]

    if profile != "mp4":
        if selected.kind != MediaFormatKind.AUDIO_ONLY:
            raise JobError(
                JobErrorCode.INVALID_PROFILE,
                detail=f"audio profile {profile!r} needs an audio-only source",
            )
        copyable = _COPYABLE_AUDIO.get(profile)
        copy_audio = copyable is not None and selected.audio_codec == copyable
        return ConversionPlan(copy_audio, False, output_codec, copy_audio)

    if selected.kind == MediaFormatKind.AUDIO_ONLY:
        raise JobError(
            JobErrorCode.INVALID_PROFILE, detail="mp4 profile needs a video source"
        )
    if selected.kind == MediaFormatKind.VIDEO_ONLY:
        if companion is None:
            raise JobError(
                JobErrorCode.INVALID_VARIANT,
                detail="mp4 profile needs an audio companion for a video-only source",
            )
        audio_codec = companion.audio_codec
    else:
        audio_codec = selected.audio_codec
    copy_video = (
        selected.video_codec == _MP4_COPYABLE_VIDEO
        and selected.h264_profile_idc not in _MP4_TRANSCODE_H264_PROFILES
    )
    copy_audio = audio_codec == _COPYABLE_AUDIO["mp4"]
    return ConversionPlan(copy_audio, copy_video, output_codec, copy_audio and copy_video)
