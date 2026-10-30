import copy
import re
from typing import Any

import pytest

from app.domain.analysis import NormalizedAnalysis, NormalizedFormat, SourceMetadata
from app.services.analysis_normalizer import (
    SOURCE_METADATA_LIST_MAX_ITEMS,
    SOURCE_METADATA_TEXT_MAX_LENGTH,
    build_normalized_analysis,
    normalize_collection,
    normalize_formats,
    normalize_source_formats,
    normalize_source_metadata,
)
from tests.yt_dlp_samples import playlist_payload, single_video_payload

_VARIANT_ID_MAX_LENGTH = 128  # API/DB limit for variant ids


@pytest.mark.parametrize("raw_codec, label", [
    *[(f"mp4a.{detail}", "aac") for detail in
      ("40.1", "40.2", "40.4", "40.5", "40.29", "66", "67", "68")],
    *[(f"mp4a.{detail}", "mp3") for detail in ("40.34", "69", "6b")],
    ("mp4a", "mp4a"), ("mp4a.", "mp4a"), ("mp4a.40", "mp4a"), ("mp4a.40.99", "mp4a"),
    ("mp4a.6a", "mp4a"), ("mp4a.zz", "mp4a"), ("mp4a.40.2.1", "mp4a"),
    ("MP4A.40.2", "aac"), ("  Mp4a.40.5  ", "aac"), ("MP4A.6B", "mp3"),
    (" mp4a.69\t", "mp3"), (" MP4A ", "mp4a"), ("MP4A.40.99", "mp4a"),
    ("mp3", "mp3"), ("opus", "opus"), ("vorbis", "vorbis"), ("ac-3", "ac3"),
    ("flac", "flac"), ("unknowncodec.1", "unknowncodec"),
])
@pytest.mark.parametrize("path", ["formats", "source_formats"])
def test_audio_codec_labels_split_mp4a_subtypes(path: str, raw_codec: str, label: str) -> None:
    raw = [
        {"format_id": "a", "ext": "m4a", "vcodec": "none", "acodec": raw_codec, "abr": 128},
        {"format_id": "m", "ext": "mp4", "height": 360, "vcodec": "avc1", "acodec": raw_codec},
    ]
    parsed = normalize_formats(raw) if path == "formats" else normalize_source_formats(raw)
    codecs = {f.format_id: f.audio_codec for f in parsed}
    assert codecs == {"a": label, "m": label}


@pytest.mark.parametrize("raw_codec, idc", [
    ("avc1.6e001f", 110), ("avc1.7a0028", 122), ("avc1.f4001e", 244), ("avc1.2c001f", 44),
    ("avc1.42e01e", 66), ("avc1.4d401f", 77), ("avc1.58a01e", 88), ("avc1.640028", 100),
    ("avc3.640028", 100), ("AVC1.6E001F", 110), ("  Avc3.F4001E\t", 244),
    ("avc1", None), ("avc3", None), ("h264", None), ("avc1.", None), ("avc1.6e00", None),
    ("avc1.6e001", None), ("avc1.6e001f0", None), ("avc1.6e001f.1", None),
    ("avc1.100.40", None), ("avc1.66.30", None), ("avc1.6g001f", None),
    ("avc1.6e_01f", None), ("avc1.６e001f", None), ("avc1.6e001fé", None),
    ("avc2.6e001f", None), ("hvc1.6e001f", None), ("vp09.00.10.08", None),
    ("avc1 .6e001f", None), ("avc1.+6e001f", None),
])
@pytest.mark.parametrize("path", ["formats", "source_formats"])
def test_h264_profile_idc_parsed_only_from_exact_avc_codec_string(
    path: str, raw_codec: str, idc: int | None,
) -> None:
    raw = [
        {"format_id": "m", "ext": "mp4", "height": 720, "vcodec": raw_codec,
         "acodec": "mp4a.40.2"},
        {"format_id": "v", "ext": "mp4", "height": 360, "vcodec": raw_codec, "acodec": "none"},
        {"format_id": "a", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128},
    ]
    parsed = normalize_formats(raw) if path == "formats" else normalize_source_formats(raw)
    assert {f.format_id for f in parsed} == {"m", "v", "a"}
    for entry_format in parsed:
        expected = None if entry_format.kind == "audio_only" else idc
        assert entry_format.h264_profile_idc == expected, entry_format.format_id


def test_h264_profile_idc_survives_best_available_and_model_copies() -> None:
    raw: list[dict[str, Any]] = [
        {"format_id": "hi10", "ext": "mp4", "height": 1080, "vcodec": "avc1.6e0028",
         "acodec": "none", "tbr": 4000.0},
        {"format_id": "main", "ext": "mp4", "height": 720, "vcodec": "avc1.4d401f",
         "acodec": "none", "tbr": 2000.0},
        {"format_id": "bare", "ext": "mp4", "height": 480, "vcodec": "avc1",
         "acodec": "none", "tbr": 1000.0},
        {"format_id": "aud", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
         "abr": 128.0},
    ]
    formats = normalize_formats(raw)
    plain = normalize_formats([
        {**entry, "vcodec": "avc1"} if entry["vcodec"] != "none" else entry for entry in raw
    ])
    # Identity, dedup and ordering do not depend on the profile.
    assert [(f.variant_id, f.format_id, f.label) for f in formats] == [
        (f.variant_id, f.format_id, f.label) for f in plain
    ]
    by_id = {f.variant_id: f for f in formats}
    assert by_id["video-best"].format_id == "hi10"
    assert by_id["video-best"].h264_profile_idc == 110
    assert by_id["video-1080"].h264_profile_idc == 110
    assert by_id["video-720"].h264_profile_idc == 77
    assert by_id["video-480"].h264_profile_idc is None
    assert by_id["audio-aac-128"].h264_profile_idc is None
    assert all(f.video_codec == "h264" for f in formats if f.kind != "audio_only")


def test_h264_profile_idc_json_roundtrip_and_legacy_default() -> None:
    [parsed] = normalize_source_formats([
        {"format_id": "v", "ext": "mp4", "height": 360, "vcodec": "avc1.f4001e",
         "acodec": "mp4a.40.2"},
    ])
    assert parsed.h264_profile_idc == 244
    assert NormalizedFormat.model_validate_json(parsed.model_dump_json()) == parsed
    legacy = parsed.model_dump(exclude={"h264_profile_idc"})
    assert "h264_profile_idc" not in legacy
    assert NormalizedFormat.model_validate(legacy).h264_profile_idc is None


def test_collection_identity_uses_snapshot_and_uncompacted_source_order() -> None:
    raw = {
        "entries": [
            {"url": "https://example.com/a", "playlist_index": 7},
            None,
            {"id": "duplicate", "url": "https://example.com/b", "playlist_index": 7},
            {"id": "duplicate", "url": "https://example.com/b", "playlist_index": "bad"},
        ],
        "playlist_count": 4,
    }
    collection = normalize_collection(raw, analysis_id="snapshot-a")
    assert collection is not None
    assert [item.source_order for item in collection.items] == [1, 2, 3, 4]
    assert [item.position for item in collection.items] == [7, 2, 7, 4]
    assert [item.source_id for item in collection.items] == [None, None, "duplicate", "duplicate"]
    assert [item.selectable for item in collection.items] == [True, False, True, True]
    assert collection.items[1].unavailable_reason == "unavailable"
    ids = [item.item_id for item in collection.items]
    assert len(set(ids)) == 4
    assert all(re.fullmatch(r"item-[0-9a-f]{64}", item_id) for item_id in ids)
    assert normalize_collection(raw, analysis_id="snapshot-a") == collection
    other = normalize_collection(raw, analysis_id="snapshot-b")
    assert other is not None
    assert set(ids).isdisjoint(item.item_id for item in other.items)
    assert [item.position for item in collection.preview_items] == [7, 7, 3]


@pytest.mark.parametrize(
    "url",
    [None, "source-id", "/relative", "//example.com/a", "file:///etc/passwd",
     "ftp://example.com/a", "https://", "https://[invalid", "--exec=command"],
)
def test_collection_unsafe_urls_are_not_selectable(url: str | None) -> None:
    collection = normalize_collection({"entries": [{"url": url}]}, analysis_id="snapshot")
    assert collection is not None
    assert collection.selectable_items == 0
    assert collection.items[0].url is None
    assert collection.items[0].unavailable_reason == "invalid_url"


@pytest.mark.parametrize("availability", ["private", "needs_auth", "unavailable"])
def test_collection_unavailable_entries_with_urls_are_not_selectable(availability: str) -> None:
    collection = normalize_collection(
        {"entries": [{"url": "https://example.com/a", "availability": availability}]},
        analysis_id="snapshot",
    )
    assert collection is not None
    assert collection.items[0].selectable is False
    assert collection.items[0].unavailable_reason == "unavailable"


def test_collection_url_candidates_use_existing_boundary_without_synthesis() -> None:
    collection = normalize_collection(
        {"entries": [{"webpage_url": "/bad", "url": " https://example.com/a "}]},
        analysis_id="snapshot",
    )
    assert collection is not None
    assert collection.items[0].url == "https://example.com/a"
    assert collection.items[0].selectable is True
    assert collection.preview_items[0].url == "/bad"


@pytest.mark.parametrize("total", [25, 200, 201, 1000])
def test_collection_source_total_is_not_the_selected_cap_or_analysis_window(total: int) -> None:
    collection = normalize_collection(
        {"entries": [{"url": "https://example.com/a"}] * total, "playlist_count": total},
        analysis_id="snapshot",
    )
    assert collection is not None
    assert collection.total_items == total
    assert collection.total_items_known is True
    assert collection.analyzed_items == collection.selectable_items == 25
    assert collection.analysis_window_limit == 25
    assert collection.selected_item_limit == 200
    assert collection.selection_scope == "analysis_window"
    assert collection.selection_truncated is (total > 25)
    assert collection.truncated is (total > 25)
    assert "200 selected items per job" in collection.selection_limit_message
    assert "unseen items are not included" in collection.selection_limit_message
    assert len(collection.preview_items) == 25
    assert len(collection.items) == 25
    assert "selected" not in collection.items[0].model_dump()


def test_collection_bounded_unknown_source_total_is_a_lower_bound() -> None:
    collection = normalize_collection(
        {"entries": [{"url": "https://example.com/a"}] * 25}, analysis_id="snapshot"
    )
    assert collection is not None
    assert collection.total_items == 25
    assert collection.total_items_known is False
    assert collection.selection_truncated is True
    assert collection.truncated is True


def test_collection_without_entries_and_without_count_is_not_a_collection() -> None:
    assert normalize_collection({"entries": []}, analysis_id="snapshot") is None


def test_collection_known_empty_source_count_is_zero_and_known() -> None:
    collection = normalize_collection(
        {"entries": [], "playlist_count": 0}, analysis_id="snapshot"
    )
    assert collection is not None
    assert collection.total_items == 0
    assert collection.total_items_known is True
    assert collection.selection_truncated is False
    assert collection.truncated is False
    assert collection.items == []
    assert collection.analyzed_items == 0
    assert collection.selectable_items == 0


def test_collection_positive_count_without_entries_stays_known_total() -> None:
    collection = normalize_collection(
        {"entries": [], "playlist_count": 3}, analysis_id="snapshot"
    )
    assert collection is not None
    assert collection.total_items == 3
    assert collection.total_items_known is True
    assert collection.selection_truncated is True
    assert collection.truncated is True


@pytest.mark.parametrize(
    "count", [None, True, False, -1, "4", 4.0, 4.5, "bad"],
)
def test_collection_malformed_count_stays_unknown(count: object) -> None:
    collection = normalize_collection(
        {"entries": [{"url": "https://example.com/a"}], "playlist_count": count},
        analysis_id="snapshot",
    )
    assert collection is not None
    assert collection.total_items == 1
    assert collection.total_items_known is False


def assert_label_variant_id(variant_id: str) -> tuple[str, str]:
    """Validate the label-namespace id and return its (slug, digest) parts."""
    assert variant_id.startswith("video-label-")
    slug, digest = variant_id[len("video-label-") :].rsplit("-", 1)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?|res", slug)
    assert len(variant_id) <= _VARIANT_ID_MAX_LENGTH
    return slug, digest


def assert_collision_audio_id(variant_id: str) -> tuple[str, str]:
    """Validate a fixed-namespace audio collision id; return (ext, codec) slugs."""
    assert variant_id.startswith("audio-alt-")
    slugs, digest = variant_id[len("audio-alt-") :].rsplit("-", 1)
    ext_slug, codec_slug = slugs.split("-", 1)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?|res", ext_slug)
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?|res", codec_slug)
    assert len(variant_id) <= _VARIANT_ID_MAX_LENGTH
    return ext_slug, codec_slug


def audio_collision_raw() -> list[dict[str, Any]]:
    """Opus/160 in two containers plus an unambiguous aac control and an
    equal-score duplicate of the WebM identity."""
    return [
        {
            "format_id": "251",
            "ext": "webm",
            "vcodec": "none",
            "acodec": "opus",
            "abr": 160.0,
        },
        {
            "format_id": "251-dup",
            "ext": "webm",
            "vcodec": "none",
            "acodec": "opus",
            "abr": 160.0,
        },
        {
            "format_id": "620",
            "ext": "ogg",
            "vcodec": "none",
            "acodec": "opus",
            "abr": 160.0,
        },
        {
            "format_id": "140",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "abr": 128.0,
        },
    ]


def test_variant_ids_are_stable_and_unique() -> None:
    formats = normalize_formats(single_video_payload()["formats"])

    ids = [f.variant_id for f in formats]
    assert len(ids) == len(set(ids))
    # Best Available first, then one option per real source resolution,
    # highest to lowest, then the source-derived audio choices.
    assert ids == [
        "video-best",
        "video-1080",
        "video-720",
        "video-360",
        "audio-opus-160",
        "audio-aac-128",
        "audio-opus-96",
        "audio-aac-m4a",
    ]

    by_id = {f.variant_id: f for f in formats}
    assert by_id["video-best"].format_id == "137"
    assert by_id["video-1080"].format_id == "137"
    assert by_id["video-720"].format_id == "22"
    assert by_id["video-360"].format_id == "18"
    assert by_id["audio-opus-160"].format_id == "251"
    assert by_id["audio-aac-128"].format_id == "140"
    assert by_id["audio-opus-96"].bitrate_kbps == 96.0
    assert by_id["audio-aac-m4a"].bitrate_kbps is None


def test_video_choices_are_best_plus_one_per_source_resolution() -> None:
    formats = [
        f for f in normalize_formats(single_video_payload()["formats"]) if f.height
    ]

    assert [f.variant_id for f in formats] == [
        "video-best",
        "video-1080",
        "video-720",
        "video-360",
    ]
    heights = [f.height for f in formats[1:] if f.height is not None]
    assert heights == sorted(heights, reverse=True)
    # Every offered resolution is a real source resolution; nothing absent
    # from the source (e.g. 1440p/2160p) is invented.
    assert set(heights) == {1080, 720, 360}


def test_duplicate_codec_container_streams_collapse_per_resolution() -> None:
    # 1080p offers h264/mp4 (tbr 4000) and vp9/webm (tbr 2800); both are
    # video-only. They must collapse to a single deterministic choice that
    # keeps the higher-bitrate real stream.
    formats = normalize_formats(single_video_payload()["formats"])

    matching = [
        f for f in formats if f.kind != "audio_only" and f.height == 1080
    ]
    # Exactly Best Available plus the single 1080p choice — the h264 and
    # vp9 streams at 1080p collapsed into one option.
    assert [f.variant_id for f in matching] == ["video-best", "video-1080"]
    representative = matching[1]
    assert representative.format_id == "137"
    assert representative.kind == "video_only"
    assert representative.video_codec == "h264"
    assert representative.ext == "mp4"
    assert representative.fps == 30.0
    assert representative.filesize_bytes == 95_000_000


def test_best_available_maps_to_best_real_source_format() -> None:
    formats = normalize_formats(single_video_payload()["formats"])

    best = formats[0]
    assert best.variant_id == "video-best"
    assert best.label == "Best Available (1080p)"
    assert best.format_id == "137"
    assert best.kind == "video_only"
    assert best.video_codec == "h264"
    assert best.audio_codec is None


def test_video_variant_labels_are_concise_resolution_choices() -> None:
    by_id = {
        f.variant_id: f for f in normalize_formats(single_video_payload()["formats"])
    }

    assert by_id["video-1080"].label == "1080p"
    assert by_id["video-720"].label == "720p"
    assert by_id["video-360"].label == "360p"
    # Audio keeps its existing source-derived listing (no backend label).
    assert by_id["audio-opus-160"].label is None


def test_video_collapse_is_deterministic_across_source_order() -> None:
    raw = single_video_payload()["formats"]

    forward = {f.variant_id: f.format_id for f in normalize_formats(raw)}
    backward = {
        f.variant_id: f.format_id for f in normalize_formats(list(reversed(raw)))
    }

    assert forward == backward


def test_equal_score_streams_pick_stable_representative() -> None:
    raw: list[dict[str, Any]] = [
        {
            "format_id": "b-format",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "height": 720,
            "tbr": 2000.0,
        },
        {
            "format_id": "a-format",
            "ext": "mp4",
            "vcodec": "avc1.64001F",
            "acodec": "none",
            "height": 720,
            "tbr": 2000.0,
        },
    ]

    formats = normalize_formats(raw)

    assert [f.variant_id for f in formats] == ["video-best", "video-720"]
    assert formats[1].format_id == "a-format"


def test_resolution_string_only_sources_stay_distinct_and_ordered() -> None:
    """Distinct real resolutions given only as labels stay distinct choices."""
    raw: list[dict[str, Any]] = [
        {
            "format_id": "lbl-720",
            "ext": "mp4",
            "vcodec": "avc1.64001F",
            "acodec": "none",
            "resolution": "1280x720",
            "tbr": 1200.0,
        },
        {
            "format_id": "lbl-1080",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "resolution": "1920x1080",
            "tbr": 2800.0,
        },
        {
            "format_id": "lbl-480",
            "ext": "mp4",
            "vcodec": "avc1.4d401f",
            "acodec": "none",
            "resolution": "480p",
            "tbr": 800.0,
        },
    ]

    formats = normalize_formats(raw)

    assert [f.variant_id for f in formats] == [
        "video-best",
        "video-1080",
        "video-720",
        "video-480",
    ]
    by_id = {f.variant_id: f for f in formats}
    assert by_id["video-best"].label == "Best Available (1080p)"
    assert by_id["video-1080"].format_id == "lbl-1080"
    assert by_id["video-1080"].height == 1080
    assert by_id["video-1080"].width == 1920
    assert by_id["video-1080"].label == "1080p"
    assert by_id["video-720"].label == "720p"
    assert by_id["video-480"].label == "480p"


@pytest.mark.parametrize("label, height, width", [
    ("0p", None, None),
    ("1920x0", None, 1920),
    ("0x1080", 1080, None),
    ("0x0", None, None),
    ("9" * 5000 + "p", None, None),
    ("9" * 400 + "p", None, None),
    ("1920x" + "9" * 5000, None, 1920),
    ("1920x" + "9" * 400, None, 1920),
    ("9" * 5000 + "x1080", 1080, None),
    ("9" * 400 + "x1080", 1080, None),
    ("1080.5p", None, None),
    ("1e309p", None, None),
    ("-1080p", None, None),
])
def test_label_axes_are_safe_positive_and_independent(
    label: str, height: int | None, width: int | None,
) -> None:
    raw = [{"format_id": "label", "vcodec": "h264", "acodec": "aac",
            "resolution": label}]
    formats = normalize_formats(raw)
    assert len(formats) == 2
    assert all((fmt.height, fmt.width) == (height, width) for fmt in formats)
    choice = formats[1]
    if height is not None:
        assert choice.variant_id == f"video-{height}"
        assert choice.label == f"{height}p"
    elif width is not None:
        assert choice.variant_id == f"video-width-{width}"
        assert choice.label == f"{width}px wide"
    else:
        assert_label_variant_id(choice.variant_id)
        assert choice.label == label
    assert normalize_formats(raw) == formats


def test_label_width_only_identity_remains_distinct_from_positive_height() -> None:
    raw = [
        {"format_id": "width", "vcodec": "h264", "resolution": "1080x0"},
        {"format_id": "height", "vcodec": "h264", "resolution": "1080p"},
        {"format_id": "same-height", "vcodec": "h264", "resolution": "1920x1080"},
    ]
    formats = normalize_formats(raw)
    assert [fmt.variant_id for fmt in formats] == [
        "video-best", "video-1080", "video-width-1080",
    ]
    assert formats[1].height == 1080
    assert formats[1].label == "1080p"
    assert formats[2].height is None
    assert formats[2].width == 1080
    assert formats[2].label == "1080px wide"
    assert normalize_formats(list(reversed(raw))) == formats


def test_resolution_string_collapses_with_numeric_same_resolution() -> None:
    """Standard equivalence merge: explicit height 1080, ``1920x1080``, and
    ``1080p`` all describe the same real resolution."""
    raw: list[dict[str, Any]] = [
        {
            "format_id": "num-1080",
            "ext": "mp4",
            "vcodec": "avc1.640028",
            "acodec": "none",
            "height": 1080,
            "width": 1920,
            "tbr": 4000.0,
        },
        {
            "format_id": "wxh-1080",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "resolution": "1920x1080",
            "tbr": 2800.0,
        },
        {
            "format_id": "p-1080",
            "ext": "webm",
            "vcodec": "av01.0.08M.08",
            "acodec": "none",
            "resolution": "1080p",
            "tbr": 2600.0,
        },
    ]

    formats = normalize_formats(raw)

    assert [f.variant_id for f in formats] == ["video-best", "video-1080"]
    assert [f.format_id for f in formats] == ["num-1080", "num-1080"]


def test_unparseable_resolution_labels_stay_distinct_and_deterministic() -> None:
    raw: list[dict[str, Any]] = [
        {
            "format_id": "v-variable",
            "ext": "mp4",
            "vcodec": "avc1.64001F",
            "acodec": "none",
            "resolution": "Variable",
            "tbr": 1000.0,
        },
        {
            "format_id": "v-custom",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "resolution": "custom-x",
            "tbr": 900.0,
        },
        {
            "format_id": "v-360",
            "ext": "mp4",
            "vcodec": "avc1.4d401f",
            "acodec": "none",
            "height": 360,
            "tbr": 500.0,
        },
    ]

    formats = normalize_formats(raw)

    # Numeric resolutions first, then distinct exact-label buckets in
    # deterministic order; label ids live in their own digest namespace.
    assert [f.variant_id for f in formats[:2]] == ["video-best", "video-360"]
    # Exact ordering is code-point deterministic: "Variable" < "custom-x".
    variable_id = formats[2].variant_id
    custom_id = formats[3].variant_id
    assert assert_label_variant_id(variable_id)[0] == "variable"
    assert assert_label_variant_id(custom_id)[0] == "custom-x"
    assert custom_id != variable_id
    assert formats[2].label == "Variable"
    assert formats[3].label == "custom-x"

    reordered = normalize_formats(list(reversed(raw)))
    assert [f.variant_id for f in reordered] == [
        f.variant_id for f in formats
    ]
    assert {f.variant_id: f.format_id for f in reordered} == {
        f.variant_id: f.format_id for f in formats
    }


def test_source_label_best_never_collides_with_reserved_best_variant() -> None:
    raw: list[dict[str, Any]] = [
        {
            "format_id": "lbl-best",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "resolution": "best",
            "tbr": 900.0,
        },
        {
            "format_id": "num-720",
            "ext": "mp4",
            "vcodec": "avc1.64001F",
            "acodec": "none",
            "height": 720,
            "tbr": 1200.0,
        },
    ]

    formats = normalize_formats(raw)

    ids = [f.variant_id for f in formats]
    # Exactly one reserved synthetic video-best, mapped to the best real
    # numeric quality; the source-labelled stream keeps its own namespace.
    assert ids.count("video-best") == 1
    assert ids[:2] == ["video-best", "video-720"]
    label_id = ids[2]
    assert assert_label_variant_id(label_id)[0] == "best"
    assert label_id != "video-best"
    by_id = {f.variant_id: f for f in formats}
    assert by_id["video-best"].format_id == "num-720"
    assert by_id[label_id].format_id == "lbl-best"
    assert by_id[label_id].label == "best"


def test_cosmetic_slug_collision_keeps_distinct_identities() -> None:
    """``A/B`` and ``A-B`` share a cosmetic slug but never an id or mapping."""
    raw: list[dict[str, Any]] = [
        {
            "format_id": "slash-form",
            "ext": "mp4",
            "vcodec": "avc1.64001F",
            "acodec": "none",
            "resolution": "A/B",
            "tbr": 300.0,
        },
        {
            "format_id": "dash-form",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "resolution": "A-B",
            "tbr": 200.0,
        },
    ]

    formats = normalize_formats(raw)

    ids = [f.variant_id for f in formats]
    assert len(ids) == len(set(ids))
    label_formats = formats[1:]
    for entry_format in label_formats:
        slug, _ = assert_label_variant_id(entry_format.variant_id)
        assert slug == "a-b"
    assert label_formats[0].variant_id != label_formats[1].variant_id
    assert {f.format_id for f in label_formats} == {"slash-form", "dash-form"}
    # Exact source labels preserved, deterministic exact-order sorting.
    assert [f.label for f in label_formats] == ["A-B", "A/B"]


def test_width_only_resolution_stays_distinct_from_height() -> None:
    """Width-only evidence must never merge with, or imply, a height."""
    raw: list[dict[str, Any]] = [
        {
            "format_id": "w-1080",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "width": 1080,
            "tbr": 2500.0,
        },
        {
            "format_id": "h-1080",
            "ext": "mp4",
            "vcodec": "avc1.640028",
            "acodec": "none",
            "height": 1080,
            "tbr": 4000.0,
        },
        {
            "format_id": "aud-128",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "abr": 128.0,
        },
    ]

    formats = normalize_formats(raw)

    # Height buckets first (best quality), then the width-only bucket,
    # each with disjoint id namespaces and truthful labels.
    assert [f.variant_id for f in formats[:3]] == [
        "video-best",
        "video-1080",
        "video-width-1080",
    ]
    by_id = {f.variant_id: f for f in formats}
    assert by_id["video-1080"].format_id == "h-1080"
    assert by_id["video-1080"].label == "1080p"
    assert by_id["video-width-1080"].format_id == "w-1080"
    assert by_id["video-width-1080"].height is None
    assert by_id["video-width-1080"].label == "1080px wide"
    assert by_id["video-best"].format_id == "h-1080"
    assert by_id["video-best"].label == "Best Available (1080p)"
    assert by_id["audio-aac-128"].format_id == "aud-128"


def test_meaningless_resolution_labels_collapse_to_unknown() -> None:
    raw: list[dict[str, Any]] = [
        {
            "format_id": "u1",
            "ext": "mp4",
            "vcodec": "avc1.64001F",
            "acodec": "none",
            "resolution": "unknown",
            "tbr": 100.0,
        },
        {
            "format_id": "u2",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "tbr": 200.0,
        },
    ]

    formats = normalize_formats(raw)

    assert [f.variant_id for f in formats] == ["video-best", "video-unknown"]
    assert formats[1].format_id == "u2"
    assert formats[1].label == "Unknown"
    assert formats[0].label == "Best Available"


def test_unknown_and_exact_label_identities_stay_distinct() -> None:
    raw: list[dict[str, Any]] = [
        {
            "format_id": "no-evidence",
            "ext": "mp4",
            "vcodec": "avc1.64001F",
            "acodec": "none",
            "tbr": 100.0,
        },
        {
            "format_id": "exact",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "resolution": "Variable",
            "tbr": 200.0,
        },
    ]

    formats = normalize_formats(raw)

    # Exact labels sort before the unknown sentinel; ids stay disjoint.
    assert [f.variant_id for f in formats] == [
        "video-best",
        formats[1].variant_id,
        "video-unknown",
    ]
    assert assert_label_variant_id(formats[1].variant_id)[0] == "variable"
    assert formats[1].format_id == "exact"
    assert formats[1].label == "Variable"
    assert formats[2].format_id == "no-evidence"
    assert formats[2].label == "Unknown"


def test_full_variant_tuples_stable_across_input_order() -> None:
    """(variant_id, format_id, label) is fully stable across source order."""
    raw: list[dict[str, Any]] = [
        {
            "format_id": "h-1080",
            "ext": "mp4",
            "vcodec": "avc1.640028",
            "acodec": "none",
            "height": 1080,
            "tbr": 4000.0,
        },
        {
            "format_id": "m-360",
            "ext": "mp4",
            "vcodec": "avc1.4d401f",
            "acodec": "mp4a.40.2",
            "height": 360,
            "tbr": 500.0,
        },
        {
            "format_id": "w-640",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "width": 640,
            "tbr": 800.0,
        },
        {
            "format_id": "lbl-slash",
            "ext": "mp4",
            "vcodec": "avc1.64001F",
            "acodec": "none",
            "resolution": "A/B",
            "tbr": 300.0,
        },
        {
            "format_id": "lbl-var",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "resolution": "variable",
            "tbr": 250.0,
        },
        {
            "format_id": "no-ev",
            "ext": "mp4",
            "vcodec": "avc1.4d401f",
            "acodec": "none",
            "tbr": 200.0,
        },
        {
            "format_id": "aud-opus",
            "ext": "webm",
            "vcodec": "none",
            "acodec": "opus",
            "abr": 160.0,
        },
    ]

    def tuples(payload: list[dict[str, Any]]) -> list[tuple[str, str, str | None]]:
        return [
            (f.variant_id, f.format_id, f.label) for f in normalize_formats(payload)
        ]

    forward = tuples(raw)
    backward = tuples(list(reversed(raw)))

    assert forward == backward
    ids = [variant_id for variant_id, _, _ in forward]
    assert len(ids) == len(set(ids))
    assert all(len(variant_id) <= _VARIANT_ID_MAX_LENGTH for variant_id in ids)
    # Typed-identity ordering: heights high→low, width-only, exact labels
    # in exact order, unknown last, then the audio choices.
    assert ids == [
        "video-best",
        "video-1080",
        "video-360",
        "video-width-640",
        forward[4][0],
        forward[5][0],
        "video-unknown",
        "audio-opus-160",
    ]
    assert assert_label_variant_id(forward[4][0])[0] == "a-b"
    assert assert_label_variant_id(forward[5][0])[0] == "variable"


def test_audio_same_codec_bitrate_different_ext_stay_unique_and_stable() -> None:
    """WebM/Opus/160 and OGG/Opus/160 both remain choices with unique ids.

    The bitrate-based base id omits ext, so every member of the collision
    group moves to the fixed bounded ``audio-alt-`` namespace; unambiguous
    ids keep their exact form.
    """
    raw = audio_collision_raw()
    formats = normalize_formats(raw)

    # Analysis succeeds; both container alternatives remain as choices and
    # every id stays unique and bounded.
    ids = [f.variant_id for f in formats]
    assert len(ids) == len(set(ids))
    assert all(len(variant_id) <= _VARIANT_ID_MAX_LENGTH for variant_id in ids)

    # Unambiguous base ids keep their exact existing form.
    assert "audio-aac-128" in ids

    # Every member of the collision group gets a bounded collision id.
    collision_ids = [i for i in ids if i.startswith("audio-alt-")]
    assert len(collision_ids) == 2
    slugs = {assert_collision_audio_id(i) for i in collision_ids}
    assert slugs == {("webm", "opus"), ("ogg", "opus")}

    # Each variant id maps to its own container's raw stream; the equal
    # score duplicate collapses deterministically to the smaller raw id.
    by_id = {f.variant_id: f for f in formats}
    webm_id = next(i for i in collision_ids if "-webm-" in i)
    ogg_id = next(i for i in collision_ids if "-ogg-" in i)
    assert by_id[webm_id].format_id == "251"
    assert by_id[ogg_id].format_id == "620"

    # Ordering stays source-derived and deterministic; ids and mappings are
    # stable under reversed source order.
    assert [f.ext for f in formats] == ["ogg", "webm", "m4a"]

    def mapping(payload: list[dict[str, Any]]) -> list[tuple[str, str]]:
        return sorted(
            (f.variant_id, f.format_id) for f in normalize_formats(payload)
        )

    assert mapping(raw) == mapping(list(reversed(raw)))


def test_muxed_preferred_over_higher_score_video_only_without_audio() -> None:
    """No audio companion at all: the muxed stream must represent the
    resolution so selections never produce silent video-only output."""
    raw: list[dict[str, Any]] = [
        {
            "format_id": "62-muxed",
            "ext": "mp4",
            "vcodec": "avc1.64001F",
            "acodec": "mp4a.40.2",
            "height": 720,
            "width": 1280,
            "tbr": 1500.0,
        },
        {
            "format_id": "666-vo",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "height": 720,
            "width": 1280,
            "tbr": 3000.0,
        },
    ]

    formats = normalize_formats(raw)

    assert [f.variant_id for f in formats] == ["video-best", "video-720"]
    assert [f.format_id for f in formats] == ["62-muxed", "62-muxed"]
    assert all(f.kind == "muxed" for f in formats)


def test_video_only_wins_at_resolution_with_audio_companion() -> None:
    """With a companion available, quality-based selection is preserved."""
    raw: list[dict[str, Any]] = [
        {
            "format_id": "62-muxed",
            "ext": "mp4",
            "vcodec": "avc1.64001F",
            "acodec": "mp4a.40.2",
            "height": 720,
            "width": 1280,
            "tbr": 1500.0,
        },
        {
            "format_id": "666-vo",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "height": 720,
            "width": 1280,
            "tbr": 3000.0,
        },
        {
            "format_id": "aud-128",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "abr": 128.0,
        },
    ]

    formats = normalize_formats(raw)

    by_id = {f.variant_id: f for f in formats}
    assert by_id["video-720"].format_id == "666-vo"
    assert by_id["video-720"].kind == "video_only"
    assert by_id["video-best"].format_id == "666-vo"
    assert by_id["audio-aac-128"].format_id == "aud-128"


def test_video_only_only_source_keeps_source_derived_choice() -> None:
    """A source with only video-only streams keeps its real choice; no audio
    or conversion is invented."""
    raw: list[dict[str, Any]] = [
        {
            "format_id": "vo-480",
            "ext": "mp4",
            "vcodec": "avc1.4d401f",
            "acodec": "none",
            "height": 480,
            "tbr": 900.0,
        },
    ]

    formats = normalize_formats(raw)

    assert [f.variant_id for f in formats] == ["video-best", "video-480"]
    assert all(f.format_id == "vo-480" for f in formats)
    assert all(f.kind == "video_only" for f in formats)


def test_formats_classify_muxed_video_only_and_audio_only() -> None:
    formats = {f.format_id: f for f in normalize_formats(single_video_payload()["formats"])}

    assert formats["18"].kind == "muxed"
    assert formats["137"].kind == "video_only"
    assert formats["251"].kind == "audio_only"


def test_formats_map_codecs_to_readable_labels() -> None:
    formats = {f.format_id: f for f in normalize_formats(single_video_payload()["formats"])}

    assert formats["137"].video_codec == "h264"
    assert formats["140"].audio_codec == "aac"
    assert formats["251"].audio_codec == "opus"
    assert formats["18"].audio_codec == "aac"


def test_representative_preserves_codec_container_and_fps_details() -> None:
    raw: list[dict[str, Any]] = [
        {
            "format_id": "999",
            "ext": "webm",
            "vcodec": "vp09.00.10.08",
            "acodec": "none",
            "height": 1440,
            "width": 2560,
            "fps": 30,
            "tbr": 3000.0,
            "filesize_approx": 80_000_000,
        },
    ]

    formats = normalize_formats(raw)

    assert [f.variant_id for f in formats] == ["video-best", "video-1440"]
    representative = formats[1]
    assert representative.video_codec == "vp9"
    assert representative.ext == "webm"
    assert representative.kind == "video_only"
    assert representative.fps == 30.0
    assert representative.filesize_approx_bytes == 80_000_000
    assert formats[0].label == "Best Available (1440p)"


def test_formats_only_report_real_bitrates() -> None:
    formats = {f.format_id: f for f in normalize_formats(single_video_payload()["formats"])}

    assert formats["251"].bitrate_kbps == 160.0
    assert formats["600"].bitrate_kbps == 96.0
    assert formats["599"].bitrate_kbps is None


def test_formats_preserve_size_and_approximate_size() -> None:
    formats = {f.format_id: f for f in normalize_formats(single_video_payload()["formats"])}

    assert formats["137"].filesize_bytes == 95_000_000
    assert formats["137"].filesize_approx_bytes is None
    assert formats["22"].filesize_bytes == 30_000_000


def test_storyboard_and_empty_formats_are_dropped() -> None:
    formats = normalize_formats(single_video_payload()["formats"])

    ids = [f.format_id for f in formats]
    assert "sb2" not in ids


def test_duplicate_video_itags_collapse_to_best_per_variant() -> None:
    raw = single_video_payload()["formats"] + [
        {
            "format_id": "137-dup",
            "ext": "mp4",
            "vcodec": "avc1.640028",
            "acodec": "none",
            "height": 1080,
            "tbr": 100.0,
        }
    ]
    formats = normalize_formats(raw)

    matching = [
        f
        for f in formats
        if f.kind == "video_only" and f.video_codec == "h264" and f.height == 1080
    ]
    # The weaker duplicate adds no extra choice and never wins the mapping.
    assert [f.variant_id for f in matching] == ["video-best", "video-1080"]
    assert [f.format_id for f in matching] == ["137", "137"]


def test_formats_sorted_video_by_height_then_audio_by_bitrate() -> None:
    formats = normalize_formats(single_video_payload()["formats"])

    video_heights = [f.height or 0 for f in formats if f.kind != "audio_only"]
    audio_bitrates = [f.bitrate_kbps for f in formats if f.kind == "audio_only"]

    assert video_heights == sorted(video_heights, reverse=True)
    non_null = [b for b in audio_bitrates if b is not None]
    assert non_null == sorted(non_null, reverse=True)


def test_build_normalized_analysis_single() -> None:
    analysis = build_normalized_analysis(
        single_video_payload(),
        analysis_id="a" * 32,
        original_url="https://www.youtube.com/watch?v=abc123",
        platform_hint=None,
    )

    assert analysis.content_type == "single"
    assert analysis.detected_platform == "youtube"
    assert analysis.detected_platform_label == "YouTube"
    assert analysis.title == "Test Video"
    assert analysis.uploader == "Test Channel"
    assert analysis.duration_seconds == 205.4
    assert analysis.thumbnail_url == "https://example.com/thumb.jpg"
    assert analysis.collection is None
    assert len(analysis.formats) > 0
    assert analysis.platform_mismatch is False


def test_build_normalized_analysis_reports_hint_mismatch() -> None:
    analysis = build_normalized_analysis(
        single_video_payload(),
        analysis_id="a" * 32,
        original_url="https://www.youtube.com/watch?v=abc123",
        platform_hint="soundcloud",
    )

    assert analysis.platform_hint == "soundcloud"
    assert analysis.platform_mismatch is True


def test_build_normalized_analysis_collection() -> None:
    analysis = build_normalized_analysis(
        playlist_payload(),
        analysis_id="b" * 32,
        original_url="https://soundcloud.com/artist/sets/test-album",
        platform_hint=None,
    )

    assert analysis.content_type == "collection"
    assert analysis.detected_platform == "soundcloud"
    assert analysis.formats == []
    assert analysis.collection is not None
    assert analysis.collection.total_items == 8
    assert [item.title for item in analysis.collection.preview_items] == [
        "Track One",
        "Track Two",
        "Track Three",
    ]
    assert analysis.collection.truncated is True
    assert analysis.collection.preview_items[0].position == 1


def test_build_normalized_analysis_carries_raw_source_id() -> None:
    analysis = build_normalized_analysis(
        single_video_payload(),
        analysis_id="a" * 32,
        original_url="https://www.youtube.com/watch?v=abc123",
        platform_hint=None,
    )

    assert analysis.source_id == "abc123"


def test_build_normalized_analysis_without_source_id_stays_none() -> None:
    payload = single_video_payload()
    del payload["id"]

    analysis = build_normalized_analysis(
        payload,
        analysis_id="a" * 32,
        original_url="https://www.youtube.com/watch?v=abc123",
        platform_hint=None,
    )

    assert analysis.source_id is None


def test_normalized_analysis_without_source_id_is_backward_compatible() -> None:
    """Sessions persisted before ``source_id`` existed must still validate."""
    legacy_json = {
        "analysis_id": "c" * 32,
        "original_url": "https://www.youtube.com/watch?v=abc123",
        "detected_platform": "youtube",
        "detected_platform_label": "YouTube",
        "content_type": "single",
    }

    parsed = NormalizedAnalysis.model_validate(legacy_json)

    assert parsed.source_id is None
    assert parsed.source_metadata is None


def _music_fields() -> dict[str, Any]:
    return {
        "track": "Black Dog (Remaster)",
        "artists": ["Led Zeppelin"],
        "album": "Led Zeppelin IV",
        "album_artists": ["Led Zeppelin"],
        "track_number": 1,
        "disc_number": 1,
        "release_year": 1971,
        "release_date": "19711108",
    }


def _build_single(payload: dict[str, Any]) -> NormalizedAnalysis:
    return build_normalized_analysis(
        payload,
        analysis_id="a" * 32,
        original_url="https://www.youtube.com/watch?v=abc123",
        platform_hint=None,
    )


def test_source_metadata_happy_path_carries_all_explicit_fields() -> None:
    analysis = _build_single({**single_video_payload(), **_music_fields()})

    assert analysis.source_metadata == SourceMetadata(**_music_fields())


def test_source_metadata_absent_is_none() -> None:
    assert _build_single(single_video_payload()).source_metadata is None
    assert normalize_source_metadata({}) is None


@pytest.mark.parametrize("raw", [None, [], "track", 3, ("track", "x")])
def test_source_metadata_non_dict_is_none(raw: object) -> None:
    assert normalize_source_metadata(raw) is None


def test_source_metadata_only_unusable_fields_is_none() -> None:
    raw = {
        "track": "   ",
        "artists": [None, ""],
        "artist": 5,
        "album": ["x"],
        "track_number": 0,
        "disc_number": True,
        "release_year": 999,
        "release_date": "20230230",
    }
    assert normalize_source_metadata(raw) is None


def test_source_metadata_never_infers_from_title_channel_or_playlist() -> None:
    raw = {
        **single_video_payload(),
        "title": "Some Artist - Some Song",
        "uploader": "Some Artist",
        "channel": "Some Artist - Topic",
        "uploader_id": "@someartist",
        "playlist_title": "Some Album",
        "playlist": "Some Album",
        "playlist_index": 3,
        "playlist_count": 12,
        "n_entries": 12,
        "track_count": 12,
        "disc_count": 2,
        "upload_date": "20200101",
        "timestamp": 1577836800,
    }
    assert normalize_source_metadata(raw) is None
    assert _build_single(raw).source_metadata is None


def test_source_metadata_does_not_carry_non_whitelisted_raw_fields() -> None:
    metadata = normalize_source_metadata(
        {"track": "Song", "genre": "Rock", "composer": "X", "track_count": 9}
    )
    assert metadata is not None
    assert set(metadata.model_dump()) == {
        "track", "artists", "album", "album_artists",
        "track_number", "disc_number", "release_year", "release_date",
    }
    assert metadata.model_dump()["track"] == "Song"


def test_source_metadata_list_precedes_singular_string() -> None:
    metadata = normalize_source_metadata(
        {
            "artists": ["A", "B"], "artist": "Ignored",
            "album_artists": ["C"], "album_artist": "Ignored too",
        }
    )
    assert metadata is not None
    assert metadata.artists == ["A", "B"]
    assert metadata.album_artists == ["C"]


@pytest.mark.parametrize("bad_list", [None, [], ["", "  ", None, 3], "A, B", {"A": 1}])
def test_source_metadata_invalid_list_falls_back_to_unsplit_singular(
    bad_list: object,
) -> None:
    metadata = normalize_source_metadata(
        {
            "artists": bad_list, "artist": "Simon & Garfunkel, Live",
            "album_artists": bad_list, "album_artist": "Crosby, Stills & Nash",
        }
    )
    assert metadata is not None
    assert metadata.artists == ["Simon & Garfunkel, Live"]
    assert metadata.album_artists == ["Crosby, Stills & Nash"]


def test_source_metadata_list_cleanup_strips_dedupes_and_keeps_order() -> None:
    metadata = normalize_source_metadata(
        {"artists": [" B ", "A", None, 7, "", "B", "a", "A", "x" * 513, "  Ç  é  "]}
    )
    assert metadata is not None
    # Exact duplicates go after stripping; case and inner spacing are kept.
    assert metadata.artists == ["B", "A", "a", "Ç  é"]


def test_source_metadata_list_keeps_first_distinct_names_up_to_bound() -> None:
    """Bound: the first 32 distinct valid names are kept, later ones ignored."""
    names = [f"Artist {index}" for index in range(SOURCE_METADATA_LIST_MAX_ITEMS + 8)]
    raw_names = [None, names[0], *names]
    metadata = normalize_source_metadata({"artists": raw_names})
    assert SOURCE_METADATA_LIST_MAX_ITEMS == 32
    assert metadata is not None
    assert metadata.artists == names[:SOURCE_METADATA_LIST_MAX_ITEMS]


def test_source_metadata_does_not_mutate_raw_input() -> None:
    raw = {
        **single_video_payload(),
        **_music_fields(),
        "artists": [" B ", "B", None, "A"],
        "artist": "Fallback",
        "track": "  Song  ",
    }
    snapshot = copy.deepcopy(raw)
    normalize_source_metadata(raw)
    _build_single(raw)
    assert raw == snapshot


def test_source_metadata_strings_strip_keep_inner_text_and_drop_overlong() -> None:
    limit = SOURCE_METADATA_TEXT_MAX_LENGTH
    metadata = normalize_source_metadata(
        {
            "track": "  Song  (Official Video)  [Live]\t",
            "album": "x" * limit,
            "artist": "y" * (limit + 1),
        }
    )
    assert limit == 512
    assert metadata is not None
    assert metadata.track == "Song  (Official Video)  [Live]"
    assert metadata.album == "x" * limit
    assert metadata.artists == []


@pytest.mark.parametrize("value", [None, 5, 1.0, b"Song", ["Song"], "", " \n "])
def test_source_metadata_non_string_or_blank_text_is_dropped(value: object) -> None:
    metadata = normalize_source_metadata({"track": value, "album": "Album"})
    assert metadata is not None
    assert metadata.track is None
    assert metadata.album == "Album"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1), (9999, 9999), ("7", 7), ("0007", 7), ("9999", 9999)],
)
def test_source_metadata_track_and_disc_numbers_accept_strict_values(
    value: object, expected: int
) -> None:
    metadata = normalize_source_metadata({"track_number": value, "disc_number": value})
    assert metadata is not None
    assert metadata.track_number == expected
    assert metadata.disc_number == expected


@pytest.mark.parametrize(
    "value",
    [
        0, -1, 10000, 10**400, True, False, 3.0, 3.5,
        float("nan"), float("inf"), float("-inf"),
        "0", "-3", "+3", " 3", "3 ", "3/12", "1.0", "abc", "", "١٢", "３",
        "00003", "9" * 5000, None, [3], {"n": 3},
    ],
)
def test_source_metadata_track_and_disc_numbers_reject_malformed(value: object) -> None:
    metadata = normalize_source_metadata(
        {"track_number": value, "disc_number": value, "track": "Song"}
    )
    assert metadata is not None
    assert metadata.track_number is None
    assert metadata.disc_number is None
    assert metadata.track == "Song"


@pytest.mark.parametrize(("value", "expected"), [(1000, 1000), (9999, 9999), ("1971", 1971)])
def test_source_metadata_release_year_accepts_four_digit_years(
    value: object, expected: int
) -> None:
    metadata = normalize_source_metadata({"release_year": value})
    assert metadata is not None
    assert metadata.release_year == expected


@pytest.mark.parametrize(
    "value",
    [999, 10000, 0, -1971, True, 1971.0, float("nan"), float("inf"), 10**400,
     "999", "19710", "1971-11", "9" * 5000, "١٩٧١", None],
)
def test_source_metadata_release_year_rejects_malformed(value: object) -> None:
    assert normalize_source_metadata({"release_year": value}) is None


@pytest.mark.parametrize("value", ["20240229", "20000229", "19711108"])
def test_source_metadata_release_date_accepts_real_dates(value: str) -> None:
    metadata = normalize_source_metadata({"release_date": value})
    assert metadata is not None
    assert metadata.release_date == value


@pytest.mark.parametrize(
    "value",
    ["20230229", "19000229", "20241301", "20240001", "20240100", "20240431",
     "2024-02-29", "2024022", "202402290", " 20240229", "２０２４０２２９",
     20240229, None, True],
)
def test_source_metadata_release_date_rejects_invalid(value: object) -> None:
    assert normalize_source_metadata({"release_date": value}) is None


def test_source_metadata_year_and_date_are_independent() -> None:
    date_only = normalize_source_metadata(
        {"release_date": "19711108", "upload_date": "20200101"}
    )
    assert date_only is not None
    assert date_only.release_year is None
    assert date_only.release_date == "19711108"

    year_only = normalize_source_metadata(
        {"release_year": 1971, "upload_date": "20200101"}
    )
    assert year_only is not None
    assert year_only.release_year == 1971
    assert year_only.release_date is None

    mismatched = normalize_source_metadata(
        {"release_year": 1990, "release_date": "19711108"}
    )
    assert mismatched is not None
    assert (mismatched.release_year, mismatched.release_date) == (1990, "19711108")


def test_source_metadata_malformed_field_keeps_analysis_and_formats() -> None:
    plain = _build_single(single_video_payload())
    noisy = _build_single(
        {
            **single_video_payload(),
            **_music_fields(),
            "track_number": "3/12",
            "release_date": "20230230",
        }
    )
    assert noisy.formats == plain.formats
    assert noisy.model_dump(exclude={"source_metadata"}) == plain.model_dump(
        exclude={"source_metadata"}
    )
    assert noisy.source_metadata is not None
    assert noisy.source_metadata.track_number is None
    assert noisy.source_metadata.release_date is None
    assert noisy.source_metadata.album == "Led Zeppelin IV"


def test_collection_parent_never_gets_source_metadata() -> None:
    plain = build_normalized_analysis(
        playlist_payload(),
        analysis_id="b" * 32,
        original_url="https://soundcloud.com/artist/sets/test-album",
        platform_hint=None,
    )
    with_music = build_normalized_analysis(
        {**playlist_payload(), **_music_fields(), "artist": "Test Artist"},
        analysis_id="b" * 32,
        original_url="https://soundcloud.com/artist/sets/test-album",
        platform_hint=None,
    )
    assert plain.source_metadata is None
    assert with_music.source_metadata is None
    assert with_music.collection is not None and plain.collection is not None
    assert [item.item_id for item in with_music.collection.items] == [
        item.item_id for item in plain.collection.items
    ]
    assert with_music.collection == plain.collection
    assert "source_metadata" not in with_music.collection.items[0].model_dump()
