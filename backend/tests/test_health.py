from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.main import create_app


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_creates_database_file(client: TestClient, settings: Settings) -> None:
    client.get("/api/v1/health")

    assert (settings.state_dir / "downloader.db").is_file()


def test_lifespan_creates_data_directories(client: TestClient, settings: Settings) -> None:
    assert settings.state_dir.is_dir()
    assert settings.staging_dir.is_dir()
    assert settings.library_dir.is_dir()


def test_health_returns_503_when_database_unreachable(
    settings: Settings, tmp_path: Path
) -> None:
    missing_dir = tmp_path / "missing"
    broken_engine = create_async_engine(
        f"sqlite+aiosqlite:///{missing_dir / 'downloader.db'}"
    )
    application = create_app(settings)
    application.state.sessionmaker = async_sessionmaker(broken_engine)

    with TestClient(application) as test_client:
        response = test_client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "unreachable"}
