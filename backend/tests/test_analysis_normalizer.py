import re
from typing import Any

import pytest

from app.domain.analysis import NormalizedAnalysis
from app.services.analysis_normalizer import (
    build_normalized_analysis,
    normalize_collection,
    normalize_formats,
)
from tests.yt_dlp_samples import playlist_payload, single_video_payload

_VARIANT_ID_MAX_LENGTH = 128  # API/DB limit for variant ids


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
