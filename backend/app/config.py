from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from the environment (prefix ``DOWNLOADER_``)."""

    model_config = SettingsConfigDict(env_prefix="DOWNLOADER_", env_file=".env", extra="ignore")

    state_dir: Path = Path("data/state")
    staging_dir: Path = Path("data/staging")
    library_dir: Path = Path("data/library")

    plex_music_dir: Path | None = None
    plex_video_dir: Path | None = None
    plex_music_display_path: str | None = None
    plex_video_display_path: str | None = None

    frontend_dist_dir: Path | None = None

    yt_dlp_binary: str = "yt-dlp"
    analysis_timeout_seconds: float = 90.0

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.state_dir / 'downloader.db'}"

    @property
    def sync_database_url(self) -> str:
        return f"sqlite:///{self.state_dir / 'downloader.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
