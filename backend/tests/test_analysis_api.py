import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.domain.analysis import AnalysisError, AnalysisErrorCode
from app.models.analysis import AnalysisSession
from tests.conftest import FakeAnalyzer
from tests.yt_dlp_samples import playlist_payload, single_video_payload

YOUTUBE_URL = "https://www.youtube.com/watch?v=abc123"


def _count_sessions(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return connection.execute("SELECT COUNT(*) FROM analysis_sessions").fetchone()[0]


def _fetch_session_row(db_path: Path, analysis_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT status, error_code FROM analysis_sessions WHERE id = ?",
            (analysis_id,),
        ).fetchone()


def _insert_session(
    settings: Settings,
    analysis_id: str,
    *,
    owner_id: str = "local",
    expires_at: datetime | None = None,
) -> None:
    async def _insert() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}")
        factory = async_sessionmaker(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            session.add(
                AnalysisSession(
                    id=analysis_id,
                    owner_id=owner_id,
                    source_url="https://example.com/old",
                    status="completed",
                    created_at=now,
                    expires_at=expires_at or now + timedelta(hours=1),
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_insert())


def _insert_expired_session(settings: Settings, analysis_id: str) -> None:
    _insert_session(
        settings, analysis_id, expires_at=datetime.now(UTC) - timedelta(hours=1)
    )


class TestAnalyzeEndpoint:
    def test_analyzes_single_item_and_persists_session(
        self, client: TestClient, analyzer: FakeAnalyzer, settings: Settings
    ) -> None:
        response = client.post("/api/v1/analysis", json={"url": YOUTUBE_URL})

        assert response.status_code == 200
        body = response.json()
        assert body["analysis_id"]
        assert body["original_url"] == YOUTUBE_URL
        assert body["title"] == "Test Video"
        assert body["detected_platform"] == "youtube"
        assert body["content_type"] == "single"
        assert body["uploader"] == "Test Channel"
        assert body["duration_seconds"] == 205.4
        assert body["platform_mismatch"] is False

        formats = {f["variant_id"]: f for f in body["formats"]}
        # Concise application-level choices: Best Available plus one option
        # per real source resolution, then source-derived audio options.
        assert [f["variant_id"] for f in body["formats"]] == [
            "video-best",
            "video-1080",
            "video-720",
            "video-360",
            "audio-opus-160",
            "audio-aac-128",
            "audio-opus-96",
            "audio-aac-m4a",
        ]
        assert formats["video-best"]["kind"] == "video_only"
        assert formats["video-best"]["label"] == "Best Available (1080p)"
        assert formats["video-best"]["format_id"] == "137"
        assert formats["video-1080"]["resolution_label"] == "1080p"
        assert formats["video-1080"]["label"] == "1080p"
        assert formats["audio-opus-160"]["bitrate_kbps"] == 160.0
        assert formats["audio-aac-m4a"]["bitrate_kbps"] is None
        assert "sb2" not in {f["format_id"] for f in body["formats"]}

        db_path = settings.state_dir / "downloader.db"
        assert _count_sessions(db_path) == 1
        status, error_code = _fetch_session_row(db_path, body["analysis_id"])
        assert status == "completed"
        assert error_code is None

    def test_get_returns_persisted_result(
        self, client: TestClient, analyzer: FakeAnalyzer
    ) -> None:
        created = client.post("/api/v1/analysis", json={"url": YOUTUBE_URL}).json()

        fetched = client.get(f"/api/v1/analysis/{created['analysis_id']}")

        assert fetched.status_code == 200
        body = fetched.json()
        assert body["status"] == "completed"
        assert body["result"]["analysis_id"] == created["analysis_id"]
        assert body["result"]["title"] == "Test Video"
        assert body["error"] is None

    def test_analyzes_playlist_collection(
        self, client: TestClient, analyzer: FakeAnalyzer
    ) -> None:
        analyzer.result = playlist_payload()

        response = client.post(
            "/api/v1/analysis",
            json={"url": "https://soundcloud.com/artist/sets/test-album"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["content_type"] == "collection"
        assert body["detected_platform"] == "soundcloud"
        assert body["formats"] == []
        assert body["collection"]["total_items"] == 8
        assert len(body["collection"]["preview_items"]) == 3
        assert body["collection"]["truncated"] is True

    def test_platform_hint_is_forwarded_and_mismatch_reported(
        self, client: TestClient, analyzer: FakeAnalyzer
    ) -> None:
        response = client.post(
            "/api/v1/analysis",
            json={"url": YOUTUBE_URL, "platform_hint": "soundcloud"},
        )

        assert response.status_code == 200
        body = response.json()
        assert analyzer.calls == [(YOUTUBE_URL, "soundcloud")]
        assert body["platform_hint"] == "soundcloud"
        assert body["platform_mismatch"] is True

    def test_matching_platform_hint_reports_no_mismatch(
        self, client: TestClient, analyzer: FakeAnalyzer
    ) -> None:
        response = client.post(
            "/api/v1/analysis", json={"url": YOUTUBE_URL, "platform_hint": "youtube"}
        )

        assert response.status_code == 200
        assert response.json()["platform_mismatch"] is False

    def test_auto_select_sends_no_hint(
        self, client: TestClient, analyzer: FakeAnalyzer
    ) -> None:
        response = client.post("/api/v1/analysis", json={"url": YOUTUBE_URL})

        assert response.status_code == 200
        assert analyzer.calls == [(YOUTUBE_URL, None)]


class TestAnalyzeErrors:
    def test_invalid_urls_are_rejected(self, client: TestClient) -> None:
        for url in ["file:///etc/passwd", "ftp://example.com/x", "/etc/passwd"]:
            response = client.post("/api/v1/analysis", json={"url": url})

            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_url"

    def test_whitespace_only_url_is_invalid(self, client: TestClient) -> None:
        response = client.post("/api/v1/analysis", json={"url": "   "})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_url"

    def test_empty_url_is_invalid_request(self, client: TestClient) -> None:
        response = client.post("/api/v1/analysis", json={"url": ""})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    def test_unknown_platform_hint_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/analysis", json={"url": YOUTUBE_URL, "platform_hint": "myspace"}
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    def test_missing_url_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/v1/analysis", json={})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    def test_unsupported_url_error_is_user_friendly_and_persisted(
        self, client: TestClient, analyzer: FakeAnalyzer, settings: Settings
    ) -> None:
        analyzer.error = AnalysisError(AnalysisErrorCode.UNSUPPORTED_URL, detail="boom")

        response = client.post("/api/v1/analysis", json={"url": YOUTUBE_URL})

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "unsupported_url"
        assert "traceback" not in error["message"]
        assert error["analysis_id"]

        status, error_code = _fetch_session_row(
            settings.state_dir / "downloader.db", error["analysis_id"]
        )
        assert status == "failed"
        assert error_code == "unsupported_url"

    def test_authentication_error_is_reported(
        self, client: TestClient, analyzer: FakeAnalyzer
    ) -> None:
        analyzer.error = AnalysisError(AnalysisErrorCode.AUTHENTICATION_REQUIRED)

        response = client.post("/api/v1/analysis", json={"url": YOUTUBE_URL})

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "authentication_required"
        assert error["retryable"] is False

    def test_network_error_is_retryable(
        self, client: TestClient, analyzer: FakeAnalyzer
    ) -> None:
        analyzer.error = AnalysisError(AnalysisErrorCode.NETWORK_ERROR)

        response = client.post("/api/v1/analysis", json={"url": YOUTUBE_URL})

        assert response.status_code == 422
        assert response.json()["error"]["retryable"] is True

    def test_malformed_extractor_output_is_internal(
        self, client: TestClient, analyzer: FakeAnalyzer
    ) -> None:
        analyzer.error = AnalysisError(AnalysisErrorCode.MALFORMED_EXTRACTOR_OUTPUT)

        response = client.post("/api/v1/analysis", json={"url": YOUTUBE_URL})

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "malformed_extractor_output"

    def test_get_unknown_analysis_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/analysis/deadbeef")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "analysis_not_found"

    def test_get_other_owners_session_returns_404(
        self, client: TestClient, settings: Settings
    ) -> None:
        _insert_session(settings, "otherowner", owner_id="someone-else")

        response = client.get("/api/v1/analysis/otherowner")

        assert response.status_code == 404

    def test_get_expired_analysis_returns_410(
        self, client: TestClient, settings: Settings
    ) -> None:
        _insert_expired_session(settings, "expired01")

        response = client.get("/api/v1/analysis/expired01")

        assert response.status_code == 410
        assert response.json()["error"]["code"] == "analysis_expired"

    @pytest.mark.parametrize(
        "code",
        [code for code in AnalysisErrorCode],
    )
    def test_get_reports_same_retryable_as_post(
        self,
        client: TestClient,
        analyzer: FakeAnalyzer,
        code: AnalysisErrorCode,
    ) -> None:
        analyzer.error = AnalysisError(code)

        created = client.post("/api/v1/analysis", json={"url": YOUTUBE_URL})
        posted_error = created.json()["error"]
        fetched = client.get(f"/api/v1/analysis/{posted_error['analysis_id']}")

        assert fetched.status_code == 200
        fetched_error = fetched.json()["error"]
        assert fetched_error["code"] == posted_error["code"]
        assert fetched_error["retryable"] == posted_error["retryable"]

    def test_single_item_without_usable_formats_fails(
        self, client: TestClient, analyzer: FakeAnalyzer
    ) -> None:
        analyzer.result = {**single_video_payload(), "formats": []}

        response = client.post("/api/v1/analysis", json={"url": YOUTUBE_URL})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "media_unavailable"
