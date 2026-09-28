"""Shared canned yt-dlp payloads used by analysis tests."""

from typing import Any


def single_video_payload() -> dict[str, Any]:
    return {
        "id": "abc123",
        "_type": "video",
        "extractor": "youtube",
        "extractor_key": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=abc123",
        "title": "Test Video",
        "uploader": "Test Channel",
        "duration": 205.4,
        "thumbnail": "https://example.com/thumb.jpg",
        "formats": [
            {
                "format_id": "sb2",
                "ext": "mhtml",
                "vcodec": "none",
                "acodec": "none",
                "format_note": "storyboard",
            },
            {
                "format_id": "18",
                "ext": "mp4",
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
                "width": 640,
                "height": 360,
                "fps": 25,
                "tbr": 500.0,
                "filesize": 12_800_000,
            },
            {
                "format_id": "22",
                "ext": "mp4",
                "vcodec": "avc1.64001F",
                "acodec": "mp4a.40.2",
                "width": 1280,
                "height": 720,
                "fps": 25,
                "tbr": 1200.0,
                "filesize": 30_000_000,
            },
            {
                "format_id": "137",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "none",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "tbr": 4000.0,
                "filesize": 95_000_000,
            },
            {
                "format_id": "248",
                "ext": "webm",
                "vcodec": "vp09.00.10.08",
                "acodec": "none",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "tbr": 2800.0,
                "filesize_approx": 70_000_000,
            },
            {
                "format_id": "251",
                "ext": "webm",
                "vcodec": "none",
                "acodec": "opus",
                "abr": 160.0,
                "tbr": 160.0,
                "filesize": 4_100_000,
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128.0,
                "filesize": 3_300_000,
            },
            {
                "format_id": "600",
                "ext": "webm",
                "vcodec": "none",
                "acodec": "opus",
                "tbr": 96.0,
            },
            {
                "format_id": "599",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
            },
        ],
    }


def playlist_payload() -> dict[str, Any]:
    return {
        "id": "set1",
        "_type": "playlist",
        "extractor": "soundcloud:set",
        "extractor_key": "SoundcloudSet",
        "webpage_url": "https://soundcloud.com/artist/sets/test-album",
        "title": "Test Album",
        "uploader": "Test Artist",
        "playlist_count": 8,
        "entries": [
            {
                "id": "track1",
                "title": "Track One",
                "webpage_url": "https://soundcloud.com/artist/track-one",
                "duration": 120.0,
                "playlist_index": 1,
            },
            {
                "id": "track2",
                "title": "Track Two",
                "webpage_url": "https://soundcloud.com/artist/track-two",
                "duration": 140.0,
                "playlist_index": 2,
            },
            {
                "id": "track3",
                "title": "Track Three",
                "webpage_url": "https://soundcloud.com/artist/track-three",
                "duration": 160.0,
                "playlist_index": 3,
            },
        ],
    }
