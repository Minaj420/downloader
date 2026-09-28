import pytest

from app.domain.platforms import (
    Platform,
    detect_platform,
    has_platform_mismatch,
    platform_label,
)


def test_detects_youtube_from_extractor_key() -> None:
    detected = detect_platform({"extractor_key": "Youtube"}, "https://youtube.com/watch?v=1")
    assert detected is Platform.YOUTUBE


def test_detects_youtube_music_from_url_host() -> None:
    detected = detect_platform(
        {"extractor_key": "Youtube"}, "https://music.youtube.com/watch?v=1"
    )
    assert detected is Platform.YOUTUBE_MUSIC


@pytest.mark.parametrize(
    ("extractor_key", "expected"),
    [
        ("Soundcloud", Platform.SOUNDCLOUD),
        ("SoundcloudSet", Platform.SOUNDCLOUD),
        ("Bandcamp", Platform.BANDCAMP),
        ("Vimeo", Platform.VIMEO),
        ("Twitch", Platform.TWITCH),
        ("TikTok", Platform.TIKTOK),
        ("Instagram", Platform.INSTAGRAM),
        ("Twitter", Platform.TWITTER),
        ("Reddit", Platform.REDDIT),
    ],
)
def test_detects_known_platforms(extractor_key: str, expected: Platform) -> None:
    assert detect_platform({"extractor_key": extractor_key}, "https://example.com/x") is expected


def test_unknown_extractor_maps_to_other() -> None:
    assert detect_platform({"extractor_key": "Generic"}, "https://example.com/x") is Platform.OTHER


def test_missing_metadata_maps_to_other() -> None:
    assert detect_platform({}, "https://example.com/x") is Platform.OTHER


def test_platform_labels() -> None:
    assert platform_label("youtube") == "YouTube"
    assert platform_label("twitter") == "X / Twitter"
    assert platform_label("youtube_music") == "YouTube Music"
    assert platform_label(None) == "Unknown"


def test_mismatch_only_when_hint_conflicts_with_known_detection() -> None:
    assert has_platform_mismatch(None, Platform.YOUTUBE) is False
    assert has_platform_mismatch("youtube", Platform.YOUTUBE) is False
    assert has_platform_mismatch("soundcloud", Platform.YOUTUBE) is True
    assert has_platform_mismatch("soundcloud", Platform.OTHER) is False
    assert has_platform_mismatch("other", Platform.YOUTUBE) is False
