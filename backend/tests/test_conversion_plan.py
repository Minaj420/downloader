from typing import Any, cast

import pytest

from app.domain.analysis import MediaFormatKind, NormalizedFormat
from app.domain.jobs import JobError, JobErrorCode
from app.integrations.ffmpeg import ConversionProfile
from app.services.analysis_normalizer import normalize_source_formats
from app.services.conversion_plan import (
    ConversionPlan,
    plan_conversion,
    resolve_output_profile,
)


def fmt(
    kind: MediaFormatKind, video: str | None = None, audio: str | None = None,
) -> NormalizedFormat:
    return NormalizedFormat(
        variant_id="v", format_id="f", kind=kind, video_codec=video, audio_codec=audio,
    )


def audio(codec: str | None) -> NormalizedFormat:
    return fmt(MediaFormatKind.AUDIO_ONLY, audio=codec)


def muxed(video: str | None, audio_codec: str | None) -> NormalizedFormat:
    return fmt(MediaFormatKind.MUXED, video, audio_codec)


def video_only(codec: str | None) -> NormalizedFormat:
    return fmt(MediaFormatKind.VIDEO_ONLY, video=codec)


@pytest.mark.parametrize("profile, source_codec, copy", [
    ("mp3", "mp3", True), ("mp3", "aac", False), ("mp3", None, False),
    ("m4a", "aac", True), ("m4a", "opus", False), ("m4a", None, False),
    ("opus", "opus", True), ("opus", "vorbis", False), ("opus", None, False),
    ("wav", "pcm_s16le", False), ("wav", "aac", False), ("wav", None, False),
    ("flac", "flac", False), ("flac", "opus", False), ("flac", None, False),
])
def test_audio_profiles_copy_only_on_exact_match(
    profile: ConversionProfile, source_codec: str | None, copy: bool,
) -> None:
    plan = plan_conversion(profile, audio(source_codec), None)
    assert plan.copy_audio is copy
    assert plan.copy_video is False
    assert plan.keep_source_bitrate is copy


def test_audio_profile_ignores_companion() -> None:
    plan = plan_conversion("mp3", audio("mp3"), audio("aac"))
    assert plan == ConversionPlan(True, False, "mp3", True)


@pytest.mark.parametrize("profile, codec", [
    ("mp3", "mp3"), ("wav", "pcm_s16le"), ("m4a", "aac"),
    ("opus", "opus"), ("flac", "flac"), ("mp4", "h264"),
])
def test_output_codec_per_profile(profile: ConversionProfile, codec: str) -> None:
    source = muxed("vp9", "opus") if profile == "mp4" else audio("vorbis")
    assert plan_conversion(profile, source, None).output_codec == codec


@pytest.mark.parametrize("video, audio_codec, copy_video, copy_audio", [
    ("h264", "aac", True, True),
    ("hevc", "aac", False, True),
    ("h264", "opus", True, False),
    ("vp9", "opus", False, False),
    ("av1", "aac", False, True),
    ("vp9", "aac", False, True),
    (None, "aac", False, True),
    ("h264", None, True, False),
    (None, None, False, False),
])
def test_mp4_muxed_source_uses_selected_codecs(
    video: str | None, audio_codec: str | None, copy_video: bool, copy_audio: bool,
) -> None:
    plan = plan_conversion("mp4", muxed(video, audio_codec), None)
    assert plan == ConversionPlan(
        copy_audio, copy_video, "h264", copy_audio and copy_video,
    )


@pytest.mark.parametrize("video, companion_codec, copy_video, copy_audio", [
    ("h264", "aac", True, True),
    ("hevc", "aac", False, True),
    ("h264", "opus", True, False),
    ("av1", None, False, False),
])
def test_mp4_video_only_uses_companion_audio(
    video: str, companion_codec: str | None, copy_video: bool, copy_audio: bool,
) -> None:
    # The video-only selection's own audio codec must not be consulted.
    selected = fmt(MediaFormatKind.VIDEO_ONLY, video, "aac")
    plan = plan_conversion("mp4", selected, audio(companion_codec))
    assert plan == ConversionPlan(
        copy_audio, copy_video, "h264", copy_audio and copy_video,
    )


@pytest.mark.parametrize("profile", ["mp3", "wav", "m4a", "opus", "flac"])
@pytest.mark.parametrize("source", [muxed("h264", "aac"), video_only("h264")])
def test_audio_profile_rejects_non_audio_source(
    profile: ConversionProfile, source: NormalizedFormat,
) -> None:
    with pytest.raises(JobError) as caught:
        plan_conversion(profile, source, audio("aac"))
    assert caught.value.code == JobErrorCode.INVALID_PROFILE


def test_mp4_rejects_audio_only_source() -> None:
    with pytest.raises(JobError) as caught:
        plan_conversion("mp4", audio("aac"), None)
    assert caught.value.code == JobErrorCode.INVALID_PROFILE


def test_mp4_video_only_without_companion_is_invalid_variant() -> None:
    with pytest.raises(JobError) as caught:
        plan_conversion("mp4", video_only("h264"), None)
    assert caught.value.code == JobErrorCode.INVALID_VARIANT


@pytest.mark.parametrize("stored", [None, "original"])
def test_resolve_output_profile_original_is_no_conversion(stored: str | None) -> None:
    assert resolve_output_profile(stored) is None


@pytest.mark.parametrize("stored", ["mp3", "wav", "m4a", "opus", "flac", "mp4"])
def test_resolve_output_profile_known_returns_itself(stored: str) -> None:
    assert resolve_output_profile(stored) == stored


@pytest.mark.parametrize("stored", ["", "bogus", "MP3", "Original", "mkv", " mp3"])
def test_resolve_output_profile_unknown_fails_closed(stored: str) -> None:
    with pytest.raises(JobError) as caught:
        resolve_output_profile(stored)
    assert caught.value.code == JobErrorCode.INVALID_PROFILE
    assert caught.value.retryable is False


_MP3_SUBTYPES = ["mp4a.69", "mp4a.6b", "mp4a.40.34", " MP4A.69 "]
_AMBIGUOUS_MP4A = ["mp4a", "mp4a.40.99", "mp4a.6a", "mp4a.zz"]


def source_stream(format_id: str, vcodec: str, acodec: str) -> NormalizedFormat:
    [parsed] = normalize_source_formats([
        {"format_id": format_id, "ext": "mp4", "height": 360, "vcodec": vcodec, "acodec": acodec},
    ])
    return parsed


@pytest.mark.parametrize("raw_codec, copy_mp4, copy_m4a, copy_mp3", [
    *[(raw, False, False, True) for raw in _MP3_SUBTYPES],
    *[(raw, False, False, False) for raw in _AMBIGUOUS_MP4A],
    ("mp4a.40.2", True, True, False),
])
def test_normalized_mp4a_subtypes_drive_copy_decisions(
    raw_codec: str, copy_mp4: bool, copy_m4a: bool, copy_mp3: bool,
) -> None:
    muxed_source = source_stream("m", "avc1.64001f", raw_codec)
    audio_source = source_stream("a", "none", raw_codec)
    video_only_source = source_stream("v", "avc1.64001f", "none")

    assert plan_conversion("mp4", muxed_source, None).copy_audio is copy_mp4
    assert plan_conversion("mp4", muxed_source, None).copy_video is True
    companion_plan = plan_conversion("mp4", video_only_source, audio_source)
    assert companion_plan.copy_audio is copy_mp4 and companion_plan.copy_video is True
    assert plan_conversion("m4a", audio_source, None).copy_audio is copy_m4a
    assert plan_conversion("mp3", audio_source, None).copy_audio is copy_mp3
    for profile in ("opus", "wav", "flac"):
        assert plan_conversion(profile, audio_source, None).copy_audio is False


@pytest.mark.parametrize("profile", ["original", "mkv"])
def test_non_conversion_profile_is_value_error(profile: Any) -> None:
    with pytest.raises(ValueError):
        plan_conversion(cast(ConversionProfile, profile), audio("aac"), None)
