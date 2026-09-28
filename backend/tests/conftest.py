from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.analysis import AnalysisError
from app.main import create_app
from tests.yt_dlp_samples import playlist_payload, single_video_payload


class FakeAnalyzer:
    """Test double replacing the yt-dlp subprocess boundary."""

    def __init__(self) -> None:
        self.result: dict = single_video_payload()
        self.error: AnalysisError | None = None
        self.calls: list[tuple[str, str | None]] = []

    async def analyze(self, url: str, platform_hint: str | None = None) -> dict:
        self.calls.append((url, platform_hint))
        if self.error is not None:
            raise self.error
        return self.result


def build_application(
    settings: Settings,
    analyzer: FakeAnalyzer,
    overrides: dict[str, Any] | None = None,
) -> FastAPI:
    resolved = settings.model_copy(update=overrides) if overrides else settings
    return create_app(resolved, analyzer=analyzer)


@pytest.fixture
def analyzer() -> FakeAnalyzer:
    return FakeAnalyzer()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        staging_dir=tmp_path / "staging",
        library_dir=tmp_path / "library",
        frontend_dist_dir=None,
    )


@pytest.fixture
def client(settings: Settings, analyzer: FakeAnalyzer) -> Generator[TestClient, None, None]:
    application = create_app(settings, analyzer=analyzer)
    with TestClient(application) as test_client:
        yield test_client


__all__ = [
    "FakeAnalyzer",
    "build_application",
    "playlist_payload",
    "single_video_payload",
]
