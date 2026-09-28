from app.services.analysis_normalizer import build_normalized_analysis, normalize_formats
from tests.yt_dlp_samples import playlist_payload, single_video_payload


def test_variant_ids_are_stable_and_unique() -> None:
    formats = normalize_formats(single_video_payload()["formats"])

    ids = [f.variant_id for f in formats]
    assert len(ids) == len(set(ids))
    by_id = {f.variant_id: f for f in formats}

    assert by_id["video-1080-video_only-h264-mp4"].format_id == "137"
    assert by_id["video-1080-video_only-vp9-webm"].format_id == "248"
    assert by_id["video-720-muxed-h264-mp4"].format_id == "22"
    assert by_id["audio-opus-160"].format_id == "251"
    assert by_id["audio-aac-128"].format_id == "140"
    assert by_id["audio-opus-96"].bitrate_kbps == 96.0
    assert by_id["audio-aac-m4a"].bitrate_kbps is None


def test_formats_classify_muxed_video_only_and_audio_only() -> None:
    formats = {f.format_id: f for f in normalize_formats(single_video_payload()["formats"])}

    assert formats["18"].kind == "muxed"
    assert formats["137"].kind == "video_only"
    assert formats["251"].kind == "audio_only"


def test_formats_map_codecs_to_readable_labels() -> None:
    formats = {f.format_id: f for f in normalize_formats(single_video_payload()["formats"])}

    assert formats["137"].video_codec == "h264"
    assert formats["248"].video_codec == "vp9"
    assert formats["140"].audio_codec == "aac"
    assert formats["251"].audio_codec == "opus"
    assert formats["18"].audio_codec == "aac"


def test_formats_only_report_real_bitrates() -> None:
    formats = {f.format_id: f for f in normalize_formats(single_video_payload()["formats"])}

    assert formats["251"].bitrate_kbps == 160.0
    assert formats["600"].bitrate_kbps == 96.0
    assert formats["599"].bitrate_kbps is None


def test_formats_preserve_size_and_approximate_size() -> None:
    formats = {f.format_id: f for f in normalize_formats(single_video_payload()["formats"])}

    assert formats["137"].filesize_bytes == 95_000_000
    assert formats["137"].filesize_approx_bytes is None
    assert formats["248"].filesize_bytes is None
    assert formats["248"].filesize_approx_bytes == 70_000_000


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
    assert [f.format_id for f in matching] == ["137"]


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
