"""Platform catalogue and detection rules."""

from enum import StrEnum
from urllib.parse import urlparse


class Platform(StrEnum):
    YOUTUBE = "youtube"
    YOUTUBE_MUSIC = "youtube_music"
    SOUNDCLOUD = "soundcloud"
    BANDCAMP = "bandcamp"
    VIMEO = "vimeo"
    TWITCH = "twitch"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    TWITTER = "twitter"
    REDDIT = "reddit"
    OTHER = "other"


PLATFORM_LABELS: dict[str, str] = {
    Platform.YOUTUBE: "YouTube",
    Platform.YOUTUBE_MUSIC: "YouTube Music",
    Platform.SOUNDCLOUD: "SoundCloud",
    Platform.BANDCAMP: "Bandcamp",
    Platform.VIMEO: "Vimeo",
    Platform.TWITCH: "Twitch",
    Platform.TIKTOK: "TikTok",
    Platform.INSTAGRAM: "Instagram",
    Platform.TWITTER: "X / Twitter",
    Platform.REDDIT: "Reddit",
    Platform.OTHER: "Other",
}

_EXTRACTOR_SIGNATURES: tuple[tuple[str, Platform], ...] = (
    ("soundcloud", Platform.SOUNDCLOUD),
    ("bandcamp", Platform.BANDCAMP),
    ("vimeo", Platform.VIMEO),
    ("twitch", Platform.TWITCH),
    ("tiktok", Platform.TIKTOK),
    ("instagram", Platform.INSTAGRAM),
    ("twitter", Platform.TWITTER),
    ("reddit", Platform.REDDIT),
)

_YOUTUBE_MUSIC_HOSTS = frozenset({"music.youtube.com"})


def platform_label(platform: str | None) -> str:
    if platform is None:
        return "Unknown"
    try:
        return PLATFORM_LABELS[Platform(platform)]
    except ValueError:
        return platform


def detect_platform(raw_info: dict[str, object], original_url: str) -> Platform:
    """Detect the platform from yt-dlp metadata, falling back to the URL."""
    hostname = (urlparse(original_url).hostname or "").lower()
    if hostname in _YOUTUBE_MUSIC_HOSTS:
        return Platform.YOUTUBE_MUSIC

    extractor_key = str(
        raw_info.get("extractor_key") or raw_info.get("extractor") or ""
    ).lower()
    if "youtube" in extractor_key:
        return Platform.YOUTUBE
    for signature, platform in _EXTRACTOR_SIGNATURES:
        if signature in extractor_key:
            return platform
    return Platform.OTHER


def has_platform_mismatch(platform_hint: str | None, detected: Platform) -> bool:
    """A hint is advisory: only a clear conflict with a known platform counts."""
    if not platform_hint or platform_hint == Platform.OTHER.value:
        return False
    if detected is Platform.OTHER:
        return False
    return platform_hint != detected.value
