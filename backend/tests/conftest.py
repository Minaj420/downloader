from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        staging_dir=tmp_path / "staging",
        library_dir=tmp_path / "library",
        frontend_dist_dir=None,
    )


@pytest.fixture
def client(settings: Settings) -> Generator[TestClient, None, None]:
    application = create_app(settings)
    with TestClient(application) as test_client:
        yield test_client
