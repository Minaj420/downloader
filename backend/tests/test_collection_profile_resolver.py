from typing import Any

import pytest

from app.domain.jobs import CollectionProfile, JobError, JobErrorCode
from app.services.analysis_normalizer import normalize_source_formats
from app.services.collection_profile_resolver import resolve_native_child
from tests.yt_dlp_samples import muxed_and_video_only_payload, single_video_payload


def stream(format_id: str, **values: Any) -> dict[str, Any]:
    return {"format_id": format_id, "ext": "mp4", "vcodec": "h264", "acodec": "aac",
            **values}


# Too large to convert to float; isfinite()/float() raise OverflowError.
_HUGE = 10**400


def test_preserves_all_source_streams_not_ui_representatives() -> None:
    source = single_video_payload()
    formats = normalize_source_formats(source["formats"])
    assert [fmt.format_id for fmt in formats] == [
        "18", "22", "137", "248", "251", "140", "600", "599"
    ]
    plan = resolve_native_child(source, mode="video")
    assert plan.selected.format_id == "248"
    assert plan.selected.ext == "webm"
    assert plan.selected.video_codec == "vp9"
    assert plan.companion is not None


@pytest.mark.parametrize("vcodec, idc", [
    ("avc1.6e0028", 110), ("avc3.f4001e", 244), ("avc1.640028", 100), ("avc1", None),
])
def test_selected_child_carries_h264_profile_idc(vcodec: str, idc: int | None) -> None:
    source = {"formats": [
        stream("small", height=360, vcodec="avc1.42e01e", acodec="none"),
        stream("big", height=1080, vcodec=vcodec, acodec="none"),
        stream("audio", vcodec="none", acodec="mp4a.40.2", ext="m4a", abr=128),
    ]}
    plan = resolve_native_child(source, mode="video")
    assert plan.selected.format_id == "big"
    assert (plan.selected.video_codec, plan.selected.h264_profile_idc) == ("h264", idc)
    assert plan.companion is not None
    assert plan.companion.h264_profile_idc is None
    small = resolve_native_child(source, mode="video", profile=CollectionProfile(max_height=720))
    assert small.selected.h264_profile_idc == 66


def test_audio_uses_extractor_preference_not_cross_codec_bitrate() -> None:
    source = {"formats": [
        stream("a", vcodec="none", acodec="aac", ext="m4a", abr=256),
        stream("b", vcodec="none", acodec="opus", ext="webm", abr=160),
    ]}
    plan = resolve_native_child(source, mode="audio")
    assert plan.selected.format_id == "b"
    assert plan.selected.ext == "webm"
    assert plan.selected.audio_codec == "opus"
    assert plan.companion is None


@pytest.mark.parametrize("ceiling, expected", [(2160, "248"), (1080, "248"), (720, "22")])
def test_ceiling_no_upscale(ceiling: int, expected: str) -> None:
    plan = resolve_native_child(single_video_payload(), mode="video",
                                profile=CollectionProfile(max_height=ceiling))
    assert plan.selected.format_id == expected
    assert plan.selected.height is not None and plan.selected.height <= ceiling


def test_dimensions_and_fps_precede_bitrate_and_source_order() -> None:
    source = {"formats": [
        stream("best", height=1080, width=1920, fps=60, tbr=100),
        stream("slow", height=1080, width=1920, fps=30, tbr=9000),
        stream("small", height=720, width=1280, fps=120, tbr=10000),
    ]}
    assert resolve_native_child(source, mode="video").selected.format_id == "best"


def test_muxed_fallback_without_audio_companion() -> None:
    plan = resolve_native_child(muxed_and_video_only_payload(), mode="video")
    assert plan.selected.format_id == "62"
    assert plan.companion is None


def test_unknown_height_excluded_only_when_ceiling_exists() -> None:
    source = {"formats": [stream("unknown", width=4096)]}
    assert resolve_native_child(source, mode="video").selected.height is None
    with pytest.raises(JobError) as exc:
        resolve_native_child(source, mode="video", profile=CollectionProfile(max_height=1080))
    assert exc.value.code == JobErrorCode.INVALID_VARIANT


def test_source_resolution_label_can_establish_ceiling() -> None:
    source = {"formats": [stream("known", resolution="1920x1080"),
                           stream("unknown", resolution="custom")]}
    plan = resolve_native_child(source, mode="video", profile=CollectionProfile(max_height=1080))
    assert plan.selected.format_id == "known"


@pytest.mark.parametrize("label, width", [
    ("0p", None),
    ("1920x0", 1920),
    ("0x0", None),
    ("9" * 5000 + "p", None),
    ("9" * 400 + "p", None),
    ("1920x" + "9" * 5000, 1920),
    ("1920x" + "9" * 400, 1920),
    ("1080.5p", None),
    ("1e309p", None),
    ("-1080p", None),
])
def test_invalid_label_height_is_unknown_and_cannot_prove_ceiling(
    label: str, width: int | None,
) -> None:
    source = {"formats": [stream("bad-label", resolution=label)]}
    formats = normalize_source_formats(source["formats"])
    assert len(formats) == 1
    assert formats[0].height is None
    assert formats[0].width == width
    assert normalize_source_formats(source["formats"]) == formats
    assert resolve_native_child(source, mode="video").selected.height is None
    profile = CollectionProfile(max_height=1080)
    with pytest.raises(JobError) as exc:
        resolve_native_child(source, mode="video", profile=profile)
    assert exc.value.code == JobErrorCode.INVALID_VARIANT
    source["formats"].insert(0, stream("clear", resolution="1280x720"))
    plan = resolve_native_child(source, mode="video", profile=profile)
    assert plan.selected.format_id == "clear"
    assert plan.selected.height == 720


@pytest.mark.parametrize("label, width", [
    ("1080p", None),
    ("1920x1080", 1920),
    ("0x1080", None),
    ("9" * 5000 + "x1080", None),
    ("9" * 400 + "x1080", None),
])
def test_valid_label_height_resolves_independently_of_invalid_width(
    label: str, width: int | None,
) -> None:
    source = {"formats": [stream("label", resolution=label)]}
    plan = resolve_native_child(source, mode="video", profile=CollectionProfile(max_height=1080))
    assert plan.selected.height == 1080
    assert plan.selected.width == width
    assert plan.selected.variant_id == "video-1080"
    assert plan.selected.label == "1080p"


@pytest.mark.parametrize("label", ["0p", "1920x0", "720p", "9" * 5000 + "p"])
def test_explicit_height_outranks_label_fallback(label: str) -> None:
    source = {"formats": [stream("explicit", height=1440, resolution=label)]}
    formats = normalize_source_formats(source["formats"])
    assert formats[0].height == 1440
    assert formats[0].label == "1440p"
    with pytest.raises(JobError) as exc:
        resolve_native_child(source, mode="video", profile=CollectionProfile(max_height=1080))
    assert exc.value.code == JobErrorCode.INVALID_VARIANT


@pytest.mark.parametrize("height", [True, 1080.5, float("inf"), float("nan"), -1])
def test_malformed_height_cannot_prove_ceiling(height: object) -> None:
    source = {"formats": [stream("bad", height=height)]}
    with pytest.raises(JobError) as exc:
        resolve_native_child(source, mode="video", profile=CollectionProfile(max_height=1080))
    assert exc.value.code == JobErrorCode.INVALID_VARIANT


@pytest.mark.parametrize("field, attribute", [
    ("height", "height"),
    ("width", "width"),
    ("fps", "fps"),
    ("filesize", "filesize_bytes"),
    ("filesize_approx", "filesize_approx_bytes"),
])
def test_unrepresentable_video_number_is_field_local_unknown(
    field: str, attribute: str,
) -> None:
    entry = stream("huge", height=1080, width=1920, fps=60,
                   filesize=1000, filesize_approx=900)
    entry[field] = _HUGE
    formats = normalize_source_formats([entry])
    assert len(formats) == 1
    parsed = formats[0]
    assert getattr(parsed, attribute) is None
    # Valid siblings of the same stream survive the field-local removal.
    assert parsed.format_id == "huge"
    if field != "height":
        assert parsed.height == 1080
    if field != "width":
        assert parsed.width == 1920
    if field != "fps":
        assert parsed.fps == 60
    if field != "filesize":
        assert parsed.filesize_bytes == 1000
    if field != "filesize_approx":
        assert parsed.filesize_approx_bytes == 900
    assert normalize_source_formats([entry]) == formats


@pytest.mark.parametrize("huge_field, kept_field, kept_bitrate", [
    ("abr", "tbr", 2000),
    ("tbr", "abr", 128),
])
def test_unrepresentable_audio_bitrate_falls_back_field_locally(
    huge_field: str, kept_field: str, kept_bitrate: float,
) -> None:
    entry = stream(
        "huge", vcodec="none", ext="m4a", filesize=1000, filesize_approx=900,
        **{huge_field: _HUGE, kept_field: kept_bitrate},
    )
    formats = normalize_source_formats([entry])
    assert len(formats) == 1
    parsed = formats[0]
    assert parsed.bitrate_kbps == kept_bitrate
    assert parsed.filesize_bytes == 1000
    assert parsed.filesize_approx_bytes == 900
    assert normalize_source_formats([entry]) == formats


def test_huge_source_numbers_never_poison_sibling_streams_or_ceiling() -> None:
    source = {"formats": [
        stream("huge-meta", height=_HUGE, width=1920, fps=120),
        stream("clear-720", height=720, width=1280, fps=30),
        stream("clear-audio", vcodec="none", acodec="aac", ext="m4a", abr=128),
    ]}
    formats = normalize_source_formats(source["formats"])
    assert [fmt.format_id for fmt in formats] == [
        "huge-meta", "clear-720", "clear-audio",
    ]
    assert formats[0].height is None  # field-local unknown, stream itself kept
    assert formats[0].width == 1920
    assert resolve_native_child(source, mode="video").selected.format_id == "clear-720"
    plan = resolve_native_child(
        source, mode="video", profile=CollectionProfile(max_height=720)
    )
    assert plan.selected.format_id == "clear-720"
    assert plan.selected.height == 720
    assert plan.companion is None


def test_huge_only_stream_with_ceiling_uses_existing_invalid_variant() -> None:
    source = {"formats": [stream("huge", height=_HUGE)]}
    formats = normalize_source_formats(source["formats"])
    assert len(formats) == 1
    assert formats[0].height is None
    with pytest.raises(JobError) as exc:
        resolve_native_child(
            source, mode="video", profile=CollectionProfile(max_height=1080)
        )
    assert exc.value.code == JobErrorCode.INVALID_VARIANT


@pytest.mark.parametrize("source, mode, code", [
    ({}, "video", JobErrorCode.MALFORMED_OUTPUT),
    ({"formats": "bad"}, "audio", JobErrorCode.MALFORMED_OUTPUT),
    ({"formats": [None, {}]}, "video", JobErrorCode.MALFORMED_OUTPUT),
    ({"_type": "playlist", "formats": [stream("v")]}, "video", JobErrorCode.MALFORMED_OUTPUT),
    ({"entries": [], "formats": [stream("v")]}, "audio", JobErrorCode.MALFORMED_OUTPUT),
    ({"formats": [stream("v")]}, "audio", JobErrorCode.INVALID_VARIANT),
    ({"formats": [stream("silent", acodec="none")]}, "video", JobErrorCode.INVALID_VARIANT),
    ({"formats": [stream("a", vcodec="none")]}, "video", JobErrorCode.INVALID_VARIANT),
    ({"formats": [stream("v")]}, "convert", JobErrorCode.INVALID_PROFILE),
])
def test_clean_rejections(source: dict[str, Any], mode: str, code: JobErrorCode) -> None:
    with pytest.raises(JobError) as exc:
        resolve_native_child(source, mode=mode)
    assert exc.value.code == code


@pytest.mark.parametrize("profile", [
    CollectionProfile(max_height=720),
    CollectionProfile.model_construct(kind="mp3"),
    CollectionProfile.model_construct(max_height=0),
])
def test_unsupported_or_malformed_profile_rejected(profile: CollectionProfile) -> None:
    with pytest.raises(JobError) as exc:
        resolve_native_child(single_video_payload(), mode="audio", profile=profile)
    assert exc.value.code == JobErrorCode.INVALID_PROFILE


def test_ceiling_below_all_real_formats_fails() -> None:
    with pytest.raises(JobError) as exc:
        resolve_native_child(single_video_payload(), mode="video",
                             profile=CollectionProfile(max_height=144))
    assert exc.value.code == JobErrorCode.INVALID_VARIANT


def test_false_and_missing_has_drm_preserved_in_source_order() -> None:
    source = {"formats": [
        stream("no-field", height=720, width=1280),
        stream("drm-flagged", height=1080, width=1920, has_drm=True),
        stream("explicit-false", height=360, width=640, has_drm=False),
        stream("clear-audio", vcodec="none", acodec="aac", ext="m4a", abr=128),
        stream("drm-audio", vcodec="none", acodec="opus", ext="webm", abr=256,
               has_drm=True),
    ]}
    assert [fmt.format_id for fmt in normalize_source_formats(source["formats"])] == [
        "no-field", "explicit-false", "clear-audio"
    ]


def test_audio_mode_skips_drm_streams() -> None:
    source = {"formats": [
        stream("clear-a", vcodec="none", acodec="aac", ext="m4a", abr=128),
        stream("drm-a", vcodec="none", acodec="opus", ext="webm", abr=256,
               has_drm=True),
    ]}
    plan = resolve_native_child(source, mode="audio")
    assert plan.selected.format_id == "clear-a"
    assert plan.companion is None


def test_drm_highest_video_falls_back_to_best_clear_format() -> None:
    source = {"formats": [
        stream("clear-720", height=720, width=1280),
        stream("drm-1080", height=1080, width=1920, has_drm=True),
        stream("clear-audio", vcodec="none", acodec="aac", ext="m4a", abr=96),
    ]}
    plan = resolve_native_child(source, mode="video")
    assert plan.selected.format_id == "clear-720"
    assert plan.selected.height == 720
    assert plan.companion is None


def test_drm_audio_never_becomes_companion() -> None:
    source = {"formats": [
        stream("clear-video-only", height=1080, width=1920, acodec="none"),
        stream("drm-audio", vcodec="none", acodec="opus", ext="webm", abr=256,
               has_drm=True),
        stream("clear-audio", vcodec="none", acodec="aac", ext="m4a", abr=128),
    ]}
    plan = resolve_native_child(source, mode="video")
    assert plan.selected.format_id == "clear-video-only"
    assert plan.companion is not None
    assert plan.companion.format_id == "clear-audio"


@pytest.mark.parametrize("mode", ["audio", "video"])
def test_all_drm_sources_use_existing_clean_error(mode: str) -> None:
    source = {"formats": [
        stream("drm-v", height=1080, width=1920, has_drm=True),
        stream("drm-a", vcodec="none", acodec="aac", ext="m4a", has_drm=True),
    ]}
    with pytest.raises(JobError) as exc:
        resolve_native_child(source, mode=mode)
    assert exc.value.code == JobErrorCode.MALFORMED_OUTPUT
