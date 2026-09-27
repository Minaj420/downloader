from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

INDEX_HTML = "<!doctype html><html><body>Downloader SPA</body></html>\n"


@pytest.fixture
def spa_client(tmp_path: Path) -> Generator[TestClient, None, None]:
    dist_dir = tmp_path / "dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text(INDEX_HTML)
    (dist_dir / "assets" / "app.js").write_text("console.log('ok');")

    settings = Settings(
        state_dir=tmp_path / "state",
        staging_dir=tmp_path / "staging",
        library_dir=tmp_path / "library",
        frontend_dist_dir=dist_dir,
    )
    application = create_app(settings)
    with TestClient(application) as test_client:
        yield test_client


def test_root_serves_index(spa_client: TestClient) -> None:
    response = spa_client.get("/")

    assert response.status_code == 200
    assert "Downloader SPA" in response.text


def test_client_route_serves_index(spa_client: TestClient) -> None:
    response = spa_client.get("/queue")

    assert response.status_code == 200
    assert "Downloader SPA" in response.text


def test_assets_are_served(spa_client: TestClient) -> None:
    response = spa_client.get("/assets/app.js")

    assert response.status_code == 200


def test_unknown_api_route_returns_404(spa_client: TestClient) -> None:
    response = spa_client.get("/api/v1/does-not-exist")

    assert response.status_code == 404


def test_traversal_attempt_falls_back_to_index(spa_client: TestClient) -> None:
    response = spa_client.get("/%2e%2e/secret.txt")

    assert response.status_code == 200
    assert "Downloader SPA" in response.text
