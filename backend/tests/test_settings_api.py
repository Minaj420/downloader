"""M7-S1: read-only Settings API (PRODUCT_SPEC.md §23, ARCHITECTURE.md §47).

``GET /api/v1/settings`` reports effective values of the injected ``Settings``
object, configured destinations and the stored, path-free startup write-check
snapshot. Reading never probes, touches the roots or exposes raw root paths.
"""

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.services.storage_service import StartupProbeResult, StorageService
from tests.conftest import FakeAnalyzer, build_application

TOP_LEVEL_KEYS = {
    "max_concurrent_jobs",
    "default_destination",
    "conflict_policy",
    "destinations",
    "startup_storage_check",
}
DESTINATION_KEYS = {"target", "configured", "selectable", "display_path"}
PROBE_KEYS = {"target", "ok", "reason"}


def _get(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/v1/settings")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def _destinations(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["target"]: entry for entry in body["destinations"]}


def _entries(root: Path) -> list[str]:
    return sorted(os.listdir(root))


class TestEffectiveValues:
    def test_reports_injected_settings_and_fixed_defaults(
        self, settings: Settings, analyzer: FakeAnalyzer,
    ) -> None:
        application = build_application(settings, analyzer, {"download_concurrency": 3})
        with TestClient(application) as client:
            body = _get(client)
        assert set(body) == TOP_LEVEL_KEYS
        assert body["max_concurrent_jobs"] == 3
        assert body["default_destination"] == "app_library"
        assert body["conflict_policy"] == "skip"

    def test_concurrency_is_the_queue_effective_value(
        self, settings: Settings, analyzer: FakeAnalyzer,
    ) -> None:
        application = build_application(settings, analyzer, {"download_concurrency": 0})
        with TestClient(application) as client:
            body = _get(client)
        assert body["max_concurrent_jobs"] == 1
        assert body["max_concurrent_jobs"] == application.state.queue_manager._concurrency

    def test_unconfigured_plex_destinations(
        self, settings: Settings, analyzer: FakeAnalyzer,
    ) -> None:
        overrides: dict[str, Any] = {
            # A display path without a configured root is not shown.
            "plex_music_display_path": "/volume1/Music",
        }
        application = build_application(settings, analyzer, overrides)
        with TestClient(application) as client:
            body = _get(client)
        assert [entry["target"] for entry in body["destinations"]] == [
            "app_library", "plex_music", "plex_video",
        ]
        for entry in body["destinations"]:
            assert set(entry) == DESTINATION_KEYS
        assert _destinations(body) == {
            "app_library": {
                "target": "app_library", "configured": True,
                "selectable": True, "display_path": None,
            },
            "plex_music": {
                "target": "plex_music", "configured": False,
                "selectable": False, "display_path": None,
            },
            "plex_video": {
                "target": "plex_video", "configured": False,
                "selectable": False, "display_path": None,
            },
        }

    @pytest.mark.parametrize("video_display", [None, "", "   "])
    def test_configured_plex_is_not_selectable_and_shows_only_explicit_display_path(
        self, settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path,
        video_display: str | None,
    ) -> None:
        music, video = tmp_path / "plex-music", tmp_path / "plex-video"
        music.mkdir()
        video.mkdir()
        overrides: dict[str, Any] = {
            "plex_music_dir": music,
            "plex_video_dir": video,
            "plex_music_display_path": "/volume1/Music",
            "plex_video_display_path": video_display,
        }
        application = build_application(settings, analyzer, overrides)
        with TestClient(application) as client:
            body = _get(client)
        destinations = _destinations(body)
        assert destinations["plex_music"] == {
            "target": "plex_music", "configured": True,
            "selectable": False, "display_path": "/volume1/Music",
        }
        assert destinations["plex_video"] == {
            "target": "plex_video", "configured": True,
            "selectable": False, "display_path": None,
        }

    def test_no_raw_paths_or_other_configuration(
        self, settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path,
    ) -> None:
        music = tmp_path / "plex-music"
        music.mkdir()
        overrides: dict[str, Any] = {
            "plex_music_dir": music,
            "yt_dlp_binary": "custom-yt-dlp-marker",
            "ffmpeg_binary": "custom-ffmpeg-marker",
        }
        application = build_application(settings, analyzer, overrides)
        with TestClient(application) as client:
            response = client.get("/api/v1/settings")
        text = response.text
        body = response.json()
        for raw in {str(tmp_path), str(tmp_path.resolve()), tmp_path.name}:
            assert raw not in text
        for marker in ("custom-yt-dlp-marker", "custom-ffmpeg-marker"):
            assert marker not in text
        # Closed allowlist at every level: no paths, binaries, format/quality.
        assert set(body) == TOP_LEVEL_KEYS
        assert set(body["startup_storage_check"]) == {"status", "results"}
        for entry in body["destinations"]:
            assert set(entry) == DESTINATION_KEYS
        for entry in body["startup_storage_check"]["results"]:
            assert set(entry) == PROBE_KEYS

    def test_no_write_method(self, client: TestClient) -> None:
        assert client.patch("/api/v1/settings", json={}).status_code == 405
        assert client.put("/api/v1/settings", json={}).status_code == 405
        assert client.post("/api/v1/settings", json={}).status_code == 405


class TestStartupSnapshot:
    def test_unavailable_without_startup(
        self, settings: Settings, analyzer: FakeAnalyzer,
    ) -> None:
        application = build_application(settings, analyzer)
        # No lifespan: the startup check never ran.
        client = TestClient(application)
        body = _get(client)
        assert body["startup_storage_check"] == {"status": "unavailable", "results": []}

    def test_unavailable_when_snapshot_cleared(
        self, settings: Settings, analyzer: FakeAnalyzer,
    ) -> None:
        application = build_application(settings, analyzer)
        with TestClient(application) as client:
            application.state.startup_storage_probe = None
            body = _get(client)
        assert body["startup_storage_check"] == {"status": "unavailable", "results": []}

    def test_successful_probe(self, client: TestClient) -> None:
        body = _get(client)
        assert body["startup_storage_check"] == {
            "status": "completed",
            "results": [
                {"target": "staging", "ok": True, "reason": None},
                {"target": "app_library", "ok": True, "reason": None},
            ],
        }

    def test_failed_probe_is_reported_path_free(
        self, settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path,
    ) -> None:
        music = tmp_path / "plex-music"  # configured but missing
        application = build_application(settings, analyzer, {"plex_music_dir": music})
        with TestClient(application) as client:
            response = client.get("/api/v1/settings")
        body = response.json()
        check = body["startup_storage_check"]
        assert check["status"] == "completed"
        for entry in check["results"]:
            assert set(entry) == PROBE_KEYS
        assert check["results"] == [
            {"target": "staging", "ok": True, "reason": None},
            {"target": "app_library", "ok": True, "reason": None},
            {"target": "plex_music", "ok": False, "reason": "root_unavailable"},
        ]
        assert _destinations(body)["plex_music"]["configured"] is True
        assert str(tmp_path) not in response.text
        assert not music.exists()

    def test_reports_the_stored_snapshot(
        self, settings: Settings, analyzer: FakeAnalyzer,
    ) -> None:
        application = build_application(settings, analyzer)
        with TestClient(application) as client:
            application.state.startup_storage_probe = (
                StartupProbeResult("staging", False, "write_failed"),
                StartupProbeResult("app_library", True, None),
            )
            body = _get(client)
        assert body["startup_storage_check"]["results"] == [
            {"target": "staging", "ok": False, "reason": "write_failed"},
            {"target": "app_library", "ok": True, "reason": None},
        ]

    def test_repeated_get_never_probes_or_touches_storage(
        self, settings: Settings, analyzer: FakeAnalyzer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []
        real_probe = StorageService.probe_startup_writability

        def counting(self: StorageService) -> tuple[StartupProbeResult, ...]:
            calls.append("probe")
            return real_probe(self)

        monkeypatch.setattr(StorageService, "probe_startup_writability", counting)
        application = build_application(settings, analyzer)
        with TestClient(application) as client:
            assert calls == ["probe"]
            real_sessionmaker = application.state.sessionmaker

            def no_database() -> None:
                raise AssertionError("Settings read must not open a DB session")

            # Request-scoped DB access only; the queue keeps its own factory.
            application.state.sessionmaker = no_database
            before = (_entries(settings.staging_dir), _entries(settings.library_dir))
            first = _get(client)
            for _ in range(3):
                assert _get(client) == first
            after = (_entries(settings.staging_dir), _entries(settings.library_dir))
            application.state.sessionmaker = real_sessionmaker
        assert calls == ["probe"]
        assert after == before
        assert first["startup_storage_check"]["status"] == "completed"
