import asyncio
import errno
import json
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.events import _replay_backlog
from app.config import Settings
from app.domain.jobs import job_error_code_retryable
from app.integrations.ytdlp import YtDlpDownloader, YtDlpError, YtDlpFailureKind
from app.models.jobs import ActivityEvent, DownloadJob
from app.repositories import activity as activity_repo
from app.repositories import jobs as job_repo
from app.services import queue_manager as queue_runtime
from app.services.analysis_normalizer import normalize_formats
from app.services.job_runner import _JobExecution
from tests.conftest import FakeAnalyzer, build_application
from tests.fake_binaries import (
    fake_ffmpeg_fail,
    fake_ffmpeg_ok,
    fake_ffmpeg_slow,
    fake_ytdlp_fail,
    fake_ytdlp_ok,
    fake_ytdlp_slow,
    fake_ytdlp_steps,
)
from tests.yt_dlp_samples import muxed_and_video_only_payload, single_video_payload

YOUTUBE_URL = "https://www.youtube.com/watch?v=abc123"
MUXED_VARIANT = "video-720"
VIDEO_ONLY_VARIANT = "video-1080"
BEST_VARIANT = "video-best"
AUDIO_VARIANT = "audio-opus-160"


@pytest.fixture
def app_factory(
    settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path
) -> Any:
    binaries = tmp_path / "bin"
    binaries.mkdir()

    def _make(
        *,
        yt_dlp: str | None = None,
        ffmpeg: str | None = None,
        concurrency: int = 2,
        analyzer_result: dict | None = None,
    ) -> TestClient:
        if analyzer_result is not None:
            analyzer.result = analyzer_result
        overrides: dict[str, Any] = {
            "download_concurrency": concurrency,
            "queue_poll_interval_seconds": 0.05,
            "progress_min_interval_seconds": 0.0,
        }
        if yt_dlp is not None:
            overrides["yt_dlp_binary"] = yt_dlp
        if ffmpeg is not None:
            overrides["ffmpeg_binary"] = ffmpeg
        application = build_application(settings, analyzer, overrides)
        return TestClient(application)

    _make.binaries = binaries  # type: ignore[attr-defined]
    return _make


def wait_for_job(
    client: TestClient,
    job_id: str,
    *,
    statuses: set[str],
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    pytest.fail(f"job did not reach {statuses}; last status: {job.get('status')}")


def create_analysis(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/v1/analysis", json={"url": YOUTUBE_URL})
    assert response.status_code == 200
    return response.json()


def create_job(
    client: TestClient,
    analysis: dict[str, Any],
    variant_id: str = MUXED_VARIANT,
) -> Any:
    return client.post(
        "/api/v1/jobs",
        json={
            "analysis_id": analysis["analysis_id"],
            "variant_id": variant_id,
            "destination": "app_library",
        },
    )


def _job_rows(settings: Settings) -> list[tuple]:
    with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
        return connection.execute(
            "SELECT id, status, error_code, final_path FROM download_jobs"
        ).fetchall()


def _row_count(settings: Settings, table: str) -> int:
    with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _activity_messages(settings: Settings, job_id: str) -> list[str]:
    with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
        rows = connection.execute(
            "SELECT message FROM activity_events WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
    return [row[0] for row in rows]


def _expire_analysis(settings: Settings, analysis_id: str) -> None:
    with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
        connection.execute(
            "UPDATE analysis_sessions SET expires_at = ? WHERE id = ?",
            (datetime.now(UTC) - timedelta(hours=1), analysis_id),
        )
        connection.commit()


class TestJobCreation:
    def test_creates_job_for_valid_analysis_and_variant(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            analysis = create_analysis(client)
            response = create_job(client, analysis)

            assert response.status_code == 200
            job = response.json()
            assert job["status"] == "queued"
            assert job["variant_id"] == MUXED_VARIANT
            assert job["destination"] == "app_library"
            assert job["title"] == "Test Video"
            assert job["source_url"] == YOUTUBE_URL

    def test_unknown_analysis_returns_404(self, app_factory: Any) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            response = client.post(
                "/api/v1/jobs",
                json={
                    "analysis_id": "f" * 32,
                    "variant_id": MUXED_VARIANT,
                    "destination": "app_library",
                },
            )

            assert response.status_code == 404
            assert response.json()["error"]["code"] == "analysis_not_found"

    def test_expired_analysis_returns_422(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            analysis = create_analysis(client)
            _expire_analysis(settings, analysis["analysis_id"])

            response = create_job(client, analysis)

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "analysis_expired"

    def test_invalid_variant_returns_422(self, app_factory: Any) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            analysis = create_analysis(client)

            response = create_job(client, analysis, variant_id="nope")

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_variant"

    def test_raw_format_id_cannot_be_used_as_variant(self, app_factory: Any) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            analysis = create_analysis(client)

            # Even a raw yt-dlp id that belongs to a real source stream
            # (137 backs video-best/video-1080) must be rejected.
            response = create_job(client, analysis, variant_id="137")

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_variant"

    def test_unsupported_destination_returns_422(self, app_factory: Any) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            analysis = create_analysis(client)

            response = client.post(
                "/api/v1/jobs",
                json={
                    "analysis_id": analysis["analysis_id"],
                    "variant_id": MUXED_VARIANT,
                    "destination": "plex_music",
                },
            )

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_destination"

    def test_wrong_owner_analysis_returns_404(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            analysis = create_analysis(client)
            with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
                connection.execute(
                    "UPDATE analysis_sessions SET owner_id = 'someone-else' WHERE id = ?",
                    (analysis["analysis_id"],),
                )
                connection.commit()

            response = create_job(client, analysis)

            assert response.status_code == 404

    def test_activity_failure_rolls_back_job_row(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Job row and its initial "Queued" event commit atomically.

        If writing the initial activity event fails, the staged job row
        must be rolled back instead of surviving as an eventless job.
        """

        async def failing_add_activity_event(
            session: Any, **kwargs: Any
        ) -> ActivityEvent:
            raise RuntimeError("activity insert failed")

        monkeypatch.setattr(
            activity_repo, "add_activity_event", failing_add_activity_event
        )
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            analysis = create_analysis(client)

            with pytest.raises(RuntimeError, match="activity insert failed"):
                create_job(client, analysis)

            assert _row_count(settings, "download_jobs") == 0
            assert _row_count(settings, "activity_events") == 0

    def test_create_job_publishes_initial_queued_activity_to_live_bus(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """An open subscriber receives the initial "Queued" activity live.

        The event must be published exactly once, carry the real database
        id of the persisted activity row, and use the same payload schema
        as job_runner's ``activity.created`` events.
        """
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            bus = client.app.state.event_bus
            subscriber = bus.subscribe()
            try:
                response = create_job(client, analysis)
                assert response.status_code == 200
                job = response.json()
                wait_for_job(client, job["id"], statuses={"completed", "failed"})

                received: list[dict[str, Any]] = []
                while not subscriber.empty():
                    received.append(subscriber.get_nowait())
            finally:
                bus.unsubscribe(subscriber)

        queued_events = [
            event
            for event in received
            if event["event"] == "activity.created"
            and event["data"]["message"] == "Queued"
        ]
        assert len(queued_events) == 1
        published = queued_events[0]

        with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
            row = connection.execute(
                "SELECT id, job_id, level, event_type, message "
                "FROM activity_events WHERE job_id = ? AND message = 'Queued'",
                (job["id"],),
            ).fetchone()
        assert row is not None
        # The published id is the real database row id, and the payload
        # carries the same fields as job_runner activity events.
        assert set(published["data"]) == {
            "id",
            "job_id",
            "level",
            "event_type",
            "message",
            "created_at",
        }
        assert published["id"] == row[0]
        assert published["data"]["id"] == row[0]
        assert published["data"]["job_id"] == row[1] == job["id"]
        assert published["data"]["level"] == row[2] == "info"
        assert published["data"]["event_type"] == row[3] == "job"
        assert published["data"]["message"] == row[4] == "Queued"
        datetime.fromisoformat(published["data"]["created_at"])

    def test_activity_failure_publishes_nothing_to_the_bus(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rolled-back creation must not emit any live activity event.

        Publishing happens only after a successful commit, so a failing
        activity insert must leave connected subscribers untouched.
        """

        async def failing_add_activity_event(
            session: Any, **kwargs: Any
        ) -> ActivityEvent:
            raise RuntimeError("activity insert failed")

        monkeypatch.setattr(
            activity_repo, "add_activity_event", failing_add_activity_event
        )
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            bus = client.app.state.event_bus
            subscriber = bus.subscribe()
            try:
                with pytest.raises(RuntimeError, match="activity insert failed"):
                    create_job(client, analysis)

                assert subscriber.empty()
            finally:
                bus.unsubscribe(subscriber)

    def test_get_unknown_job_returns_404(self, app_factory: Any) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            response = client.get("/api/v1/jobs/deadbeef")

            assert response.status_code == 404
            assert response.json()["error"]["code"] == "job_not_found"


class TestJobActivityEndpoint:
    def test_returns_seeded_events_in_id_order_for_owner(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()
            wait_for_job(client, job["id"], statuses={"completed"})
            # SQLite returns naïve datetimes; they are defined as UTC by the
            # API contract and must be serialized with an explicit offset.
            with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
                connection.execute(
                    "UPDATE activity_events SET created_at = ? WHERE job_id = ?",
                    ("2026-09-28 12:00:00", job["id"]),
                )
                connection.commit()

            response = client.get(f"/api/v1/jobs/{job['id']}/activity")

        assert response.status_code == 200
        events = response.json()["events"]
        assert len(events) >= 3
        ids = [event["id"] for event in events]
        assert ids == sorted(ids)
        assert events[0]["message"] == "Queued"
        for event in events:
            # Same field set as the SSE activity payload; details stay
            # backend-side in the basic M2 panel.
            assert set(event) == {
                "id",
                "job_id",
                "level",
                "event_type",
                "message",
                "created_at",
            }
            assert event["job_id"] == job["id"]
            parsed = datetime.fromisoformat(event["created_at"])
            assert parsed.utcoffset() == timedelta(0)

    def test_unknown_job_returns_404(self, app_factory: Any) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            response = client.get("/api/v1/jobs/deadbeef/activity")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "job_not_found"

    def test_wrong_owner_job_returns_404(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()
            with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
                connection.execute(
                    "UPDATE download_jobs SET owner_id = 'someone-else' WHERE id = ?",
                    (job["id"],),
                )
                connection.commit()

            response = client.get(f"/api/v1/jobs/{job['id']}/activity")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "job_not_found"


class TestJobExecution:
    def test_muxed_download_completes_into_app_library(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
            assert final["status"] == "completed", final
            assert final["progress"]["percent"] == 100.0
            assert final["final_path"] == "video/Test Video.mp4"

            final_file = settings.library_dir / "video" / "Test Video.mp4"
            assert final_file.read_text() == "fake media"
            assert not (settings.staging_dir / job["id"]).exists()

            messages = _activity_messages(settings, job["id"])
            assert messages[0] == "Queued"
            assert "Download started" in messages
            assert "Saved to App Library" in messages

            rows = _job_rows(settings)
            assert len(rows) == 1
            assert rows[0][1] == "completed"

    def test_audio_variant_places_into_audio_directory(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis, variant_id=AUDIO_VARIANT).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
            assert final["status"] == "completed", final
            assert final["final_path"] == "audio/Test Video.mp4"
            assert (settings.library_dir / "audio" / "Test Video.mp4").is_file()

    def test_audio_collision_variant_ids_resolve_to_their_raw_formats(
        self, app_factory: Any
    ) -> None:
        """Same codec+bitrate in two containers: both disambiguated variant
        ids are accepted and download their own raw stream directly."""
        binaries = app_factory.binaries
        two_containers = {
            **single_video_payload(),
            "formats": [
                {
                    "format_id": "251",
                    "ext": "webm",
                    "vcodec": "none",
                    "acodec": "opus",
                    "abr": 160.0,
                },
                {
                    "format_id": "620",
                    "ext": "ogg",
                    "vcodec": "none",
                    "acodec": "opus",
                    "abr": 160.0,
                },
            ],
        }
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
            analyzer_result=two_containers,
        ) as client:
            analysis = create_analysis(client)
            variants = analysis["formats"]
            assert len(variants) == 2
            ids = [variant["variant_id"] for variant in variants]
            assert len(set(ids)) == 2

            by_raw = {variant["format_id"]: variant["variant_id"] for variant in variants}
            assert by_raw["251"].startswith("audio-alt-webm-opus-")
            assert by_raw["620"].startswith("audio-alt-ogg-opus-")

            for variant in variants:
                job = create_job(
                    client, analysis, variant_id=variant["variant_id"]
                ).json()
                final = wait_for_job(
                    client, job["id"], statuses={"completed", "failed"}
                )
                assert final["status"] == "completed", final
                assert final["final_path"] == "audio/Test Video.mp4"

    def test_long_codec_collision_ids_stay_bounded_and_selectable(
        self, app_factory: Any
    ) -> None:
        """A pathological source codec must not push collision ids past the
        API/DB 128-char limit: both bounded ids stay offered, unique,
        deterministic, and selectable, each mapping to its own raw stream."""
        binaries = app_factory.binaries
        long_codec = "x" * 200
        two_containers = {
            **single_video_payload(),
            "formats": [
                {
                    "format_id": "web-1",
                    "ext": "webm",
                    "vcodec": "none",
                    "acodec": long_codec,
                    "abr": 160.0,
                },
                {
                    "format_id": "ogg-1",
                    "ext": "ogg",
                    "vcodec": "none",
                    "acodec": long_codec,
                    "abr": 160.0,
                },
            ],
        }
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
            analyzer_result=two_containers,
        ) as client:
            analysis = create_analysis(client)
            variants = analysis["formats"]
            ids = [variant["variant_id"] for variant in variants]
            assert len(ids) == len(set(ids)) == 2
            assert all(len(variant_id) <= 128 for variant_id in ids)

            # Deterministic: re-analyzing the same source yields identical ids.
            repeat = create_analysis(client)
            assert [v["variant_id"] for v in repeat["formats"]] == ids

            by_raw = {v["format_id"]: v["variant_id"] for v in variants}
            assert by_raw["web-1"].startswith("audio-alt-webm-")
            assert by_raw["ogg-1"].startswith("audio-alt-ogg-")

            for variant in variants:
                job = create_job(
                    client, analysis, variant_id=variant["variant_id"]
                ).json()
                final = wait_for_job(
                    client, job["id"], statuses={"completed", "failed"}
                )
                assert final["status"] == "completed", final
                assert final["variant_id"] == variant["variant_id"]
                assert final["final_path"] == "audio/Test Video.mp4"

    def test_oversized_single_audio_codec_routes_to_bounded_id(
        self, app_factory: Any
    ) -> None:
        """A lone long-codec audio stream must stay selectable.

        Its base id would embed the ~200-char codec and exceed the
        128-char API/DB limit, so even as a single-member group it routes
        through the bounded ``audio-alt-`` digest namespace; a normal
        short id in the same source keeps its exact existing form.
        """
        binaries = app_factory.binaries
        long_codec = "y" * 200
        raw_formats = [
            {
                "format_id": "900",
                "ext": "webm",
                "vcodec": "none",
                "acodec": long_codec,
                "abr": 160.0,
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128.0,
            },
        ]
        single_long_codec = {
            **single_video_payload(),
            "formats": raw_formats,
        }
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
            analyzer_result=single_long_codec,
        ) as client:
            analysis = create_analysis(client)
            by_raw = {f["format_id"]: f for f in analysis["formats"]}
            assert set(by_raw) == {"900", "140"}

            bounded_id = by_raw["900"]["variant_id"]
            assert bounded_id.startswith("audio-alt-webm-")
            assert len(bounded_id) <= 92  # bounded namespace bound (< 128)
            # Short unambiguous ids keep their exact existing form.
            assert by_raw["140"]["variant_id"] == "audio-aac-128"

            # Deterministic and stable across reversed source order.
            forward = [
                (f.variant_id, f.format_id)
                for f in normalize_formats(raw_formats)
            ]
            backward = [
                (f.variant_id, f.format_id)
                for f in normalize_formats(list(reversed(raw_formats)))
            ]
            assert forward == backward
            assert ("audio-aac-128", "140") in forward
            assert (bounded_id, "900") in forward

            job = create_job(client, analysis, variant_id=bounded_id).json()
            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
            assert final["status"] == "completed", final
            assert final["variant_id"] == bounded_id
            assert final["final_path"] == "audio/Test Video.mp4"

    def test_video_only_variant_merges_with_ffmpeg(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=fake_ffmpeg_ok(binaries),
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis, variant_id=VIDEO_ONLY_VARIANT).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
            assert final["status"] == "completed", final
            # h264 video + opus audio => mkv container
            assert final["final_path"] == "video/Test Video.mkv"
            merged = settings.library_dir / "video" / "Test Video.mkv"
            assert merged.read_text() == "merged-media"

            messages = _activity_messages(settings, job["id"])
            assert any("merging" in message for message in messages)

    def test_best_available_variant_resolves_to_best_source_format(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """video-best maps to the stored best real stream and merges normally."""
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=fake_ffmpeg_ok(binaries),
        ) as client:
            analysis = create_analysis(client)
            assert analysis["formats"][0]["variant_id"] == BEST_VARIANT
            job = create_job(client, analysis, variant_id=BEST_VARIANT).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
            assert final["status"] == "completed", final
            # Best source video (137, h264 video-only) + best audio (251,
            # opus) companion => FFmpeg merge into mkv, no conversion.
            assert final["final_path"] == "video/Test Video.mkv"
            assert (settings.library_dir / "video" / "Test Video.mkv").read_text() == (
                "merged-media"
            )
            messages = _activity_messages(settings, job["id"])
            assert any("merging" in message for message in messages)

    def test_best_available_without_audio_companion_downloads_muxed_directly(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """No audio-only companion: Best Available maps to the muxed stream.

        The same resolution also offers a higher-bitrate video-only stream;
        selecting it would download silent output with no companion to
        merge. Both video variants must map to the muxed raw id, and the
        job must complete by direct download even with FFmpeg unavailable.
        """
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
            analyzer_result=muxed_and_video_only_payload(),
        ) as client:
            analysis = create_analysis(client)
            variants = {f["variant_id"]: f for f in analysis["formats"]}
            assert set(variants) == {"video-best", "video-720"}
            assert variants["video-best"]["format_id"] == "62"
            assert variants["video-720"]["format_id"] == "62"
            job = create_job(client, analysis, variant_id=BEST_VARIANT).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
            assert final["status"] == "completed", final
            assert final["final_path"] == "video/Muxed Source Video.mp4"
            assert (
                settings.library_dir / "video" / "Muxed Source Video.mp4"
            ).read_text() == "fake media"

            messages = _activity_messages(settings, job["id"])
            assert not any("merging" in message for message in messages)

    def test_label_identity_variant_maps_to_real_format_for_jobs(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """Exotic label-namespace variant ids resolve to their real stream.

        A source whose only resolution evidence is an unparseable label
        gets a digest-namespaced variant id; job creation and the runner
        must look it up exactly like any other representative id.
        """
        binaries = app_factory.binaries
        labeled = {
            **single_video_payload(),
            "formats": [
                {
                    "format_id": "77",
                    "ext": "mp4",
                    "vcodec": "avc1.640028",
                    "acodec": "mp4a.40.2",
                    "resolution": "variable",
                    "tbr": 1800.0,
                },
            ],
        }
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
            analyzer_result=labeled,
        ) as client:
            analysis = create_analysis(client)
            best = analysis["formats"][0]
            labelled = analysis["formats"][1]
            assert best["variant_id"] == "video-best"
            assert labelled["variant_id"].startswith("video-label-variable-")
            # Both representative ids map to the same real raw stream.
            assert best["format_id"] == labelled["format_id"] == "77"

            job = create_job(
                client, analysis, variant_id=labelled["variant_id"]
            ).json()
            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
            assert final["status"] == "completed", final
            assert final["final_path"] == "video/Test Video.mp4"

    def test_progress_fields_update_during_download(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            deadline = time.monotonic() + 5.0
            seen_progress = False
            while time.monotonic() < deadline:
                current = client.get(f"/api/v1/jobs/{job['id']}").json()
                progress = current["progress"]
                if progress["total_bytes"]:
                    assert progress["downloaded_bytes"] == 3
                    assert progress["total_bytes"] == 10
                    assert progress["speed_bytes_per_second"] == 1_000_000
                    assert progress["eta_seconds"] == 7
                    seen_progress = True
                    break
                time.sleep(0.02)
            assert seen_progress

            wait_for_job(client, job["id"], statuses={"completed", "failed"})

    def test_download_failure_records_user_friendly_error(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        failing = fake_ytdlp_fail(binaries, "Unsupported URL: https://example.com/x")
        with app_factory(yt_dlp=failing, ffmpeg=str(binaries / "ffmpeg-missing")) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(client, job["id"], statuses={"failed", "cancelled"})
            assert final["status"] == "failed"
            assert final["error"]["code"] == "unsupported_url"
            assert "no longer supported" in final["error"]["message"].lower()

            messages = _activity_messages(settings, job["id"])
            assert any(message.startswith("Failed:") for message in messages)

    def test_missing_yt_dlp_binary_fails_job(self, app_factory: Any) -> None:
        with app_factory(
            yt_dlp="/nonexistent/yt-dlp-binary", ffmpeg="/nonexistent/ffmpeg"
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(client, job["id"], statuses={"failed"})
            assert final["error"]["code"] == "yt_dlp_missing"

    def test_missing_ffmpeg_binary_fails_merge_job(self, app_factory: Any) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg="/nonexistent/ffmpeg"
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis, variant_id=VIDEO_ONLY_VARIANT).json()

            final = wait_for_job(client, job["id"], statuses={"failed"})
            assert final["error"]["code"] == "ffmpeg_missing"

    def test_ffmpeg_failure_fails_job(self, app_factory: Any) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=fake_ffmpeg_fail(binaries)
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis, variant_id=VIDEO_ONLY_VARIANT).json()

            final = wait_for_job(client, job["id"], statuses={"failed"})
            assert final["error"]["code"] == "post_processing_failed"

    def test_malformed_download_output_fails_job(self, app_factory: Any) -> None:
        binaries = app_factory.binaries
        body = "#!/bin/sh\nprintf 'nothing useful\\n'\n"
        path = binaries / "yt-dlp-empty"
        path.write_text(body)
        path.chmod(0o755)
        with app_factory(
            yt_dlp=str(path), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(client, job["id"], statuses={"failed"})
            assert final["error"]["code"] == "malformed_output"

    def test_dangerous_title_is_sanitized_in_final_path(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        malicious = single_video_payload()
        malicious["title"] = "../../etc/passwd:\u0000evil?"
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
            analyzer_result=malicious,
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
            assert final["status"] == "completed", final
            final_path = final["final_path"]
            safe_name = final_path.rsplit("/", 1)[-1]
            assert safe_name.startswith(".._.._etc_passwd")
            assert "\u0000" not in safe_name
            assert (settings.library_dir / "video" / safe_name).is_file()

    def test_long_title_keeps_extension_and_byte_budget(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        long_title = single_video_payload()
        long_title["title"] = "T" * 200
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
            analyzer_result=long_title,
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
            assert final["status"] == "completed", final
            safe_name = final["final_path"].rsplit("/", 1)[-1]
            assert safe_name == "T" * 146 + ".mp4"
            assert len(safe_name.encode("utf-8")) <= 150
            assert (settings.library_dir / "video" / safe_name).is_file()

    def test_existing_final_file_is_skipped_not_overwritten(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        (settings.library_dir / "video").mkdir(parents=True, exist_ok=True)
        (settings.library_dir / "video" / "Test Video.mp4").write_text("original")

        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
            assert final["status"] == "completed", final
            existing = settings.library_dir / "video" / "Test Video.mp4"
            assert existing.read_text() == "original"
            messages = _activity_messages(settings, job["id"])
            assert any("already exists" in message for message in messages)


class TestQueueConcurrency:
    @pytest.mark.parametrize(
        ("concurrency", "first_status"),
        [(2, "completed"), (1, "completed"), (1, "failed")],
    )
    def test_occupied_slots_bound_execution_and_release_drains_queue(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        concurrency: int,
        first_status: str,
    ) -> None:
        """Observe the real lifespan loop; gate only the downloader boundary.

        Runner instrumentation counts whole executions, including finalization
        and helper drain. The loop spy is passive: tests never claim work or
        notify the queue to make the waiting job advance.
        """
        condition = threading.Condition()
        releases = [threading.Event() for _ in range(concurrency + 1)]
        started: list[str] = []
        blocked: set[str] = set()
        active: set[str] = set()
        active_history: list[frozenset[str]] = []
        cycles = 0
        real_runner = queue_runtime.run_download_job
        real_download = YtDlpDownloader.download
        real_claim = queue_runtime.QueueManager._claim_and_run

        async def observed_runner(job_id: str, **kwargs: Any) -> None:
            with condition:
                started.append(job_id)
                active.add(job_id)
                active_history.append(frozenset(active))
                condition.notify_all()
            try:
                await real_runner(job_id, **kwargs)
            finally:
                with condition:
                    active.remove(job_id)
                    active_history.append(frozenset(active))
                    condition.notify_all()

        async def gated_download(downloader: YtDlpDownloader, **kwargs: Any) -> list[str]:
            job_id = Path(kwargs["output_template"]).parent.name
            with condition:
                index = started.index(job_id)
                blocked.add(job_id)
                condition.notify_all()
            assert await asyncio.to_thread(releases[index].wait, 10), "release timed out"
            if index == 0 and first_status == "failed":
                raise YtDlpError(YtDlpFailureKind.NETWORK_ERROR, "controlled failure")
            return await real_download(downloader, **kwargs)

        async def observed_claim(manager: queue_runtime.QueueManager) -> None:
            nonlocal cycles
            await real_claim(manager)
            with condition:
                cycles += 1
                condition.notify_all()

        def wait_until(predicate: Callable[[], bool]) -> None:
            with condition:
                assert condition.wait_for(predicate, timeout=10), (
                    f"queue barrier timed out: started={started}, active={active}, "
                    f"blocked={blocked}, cycles={cycles}"
                )

        def wait_for_download(job_id: str) -> None:
            wait_until(lambda: job_id in blocked)

        monkeypatch.setattr(queue_runtime, "run_download_job", observed_runner)
        monkeypatch.setattr(YtDlpDownloader, "download", gated_download)
        monkeypatch.setattr(queue_runtime.QueueManager, "_claim_and_run", observed_claim)
        with app_factory(
            yt_dlp=fake_ytdlp_ok(app_factory.binaries), concurrency=concurrency
        ) as client:
            try:
                analysis = create_analysis(client)
                jobs = []
                for _ in range(concurrency):
                    response = create_job(client, analysis)
                    assert response.status_code == 200
                    job = response.json()
                    assert job["status"] == "queued"
                    assert job["analysis_id"] == analysis["analysis_id"]
                    assert job["variant_id"] == MUXED_VARIANT
                    assert job["destination"] == "app_library"
                    jobs.append(job)
                    wait_for_download(job["id"])

                occupied = {job["id"] for job in jobs}
                with condition:
                    assert active == occupied
                    assert frozenset(occupied) in active_history

                response = create_job(client, analysis)
                assert response.status_code == 200
                queued = response.json()
                assert queued["status"] == "queued"
                assert queued["analysis_id"] == analysis["analysis_id"]
                assert queued["variant_id"] == MUXED_VARIANT
                assert queued["destination"] == "app_library"
                jobs.append(queued)

                # Several actual automatic loop passes with occupied slots,
                # not a fixed sleep that might precede the first queue check.
                with condition:
                    observed_cycles = cycles
                wait_until(lambda: cycles >= observed_cycles + 3)
                current = client.get(f"/api/v1/jobs/{queued['id']}")
                assert current.status_code == 200
                assert current.json()["status"] == "queued"
                with condition:
                    assert active == occupied
                    assert queued["id"] not in started
                for job_id in occupied:
                    current = client.get(f"/api/v1/jobs/{job_id}")
                    assert current.status_code == 200
                    assert current.json()["status"] == "downloading"

                releases[0].set()
                wait_for_download(queued["id"])
                first = wait_for_job(
                    client, jobs[0]["id"], statuses={"completed", "failed"}
                )
                assert first["status"] == first_status, first
                if first_status == "failed":
                    assert first["error"]["code"] == "network_error"
                with condition:
                    assert active == (occupied - {jobs[0]["id"]}) | {queued["id"]}
                if concurrency == 2:
                    current = client.get(f"/api/v1/jobs/{jobs[1]['id']}")
                    assert current.status_code == 200
                    assert current.json()["status"] == "downloading"

                for release in releases:
                    release.set()
                for job in jobs[1:]:
                    final = wait_for_job(
                        client, job["id"], statuses={"completed", "failed"}
                    )
                    assert final["status"] == "completed", final
                wait_until(lambda: not active)
                with condition:
                    assert started == [job["id"] for job in jobs]
                    assert max(map(len, active_history)) == concurrency
                    assert all(len(snapshot) <= concurrency for snapshot in active_history)
                assert {row[0]: row[1] for row in _job_rows(settings)} == {
                    job["id"]: first_status if index == 0 else "completed"
                    for index, job in enumerate(jobs)
                }
            finally:
                # Release before TestClient exits, even if any assertion fails.
                for release in releases:
                    release.set()


class TestCancellation:
    def test_cancel_running_job_terminates_and_cleans_staging(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_slow(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            wait_for_job(client, job["id"], statuses={"downloading"})
            staging_dir = settings.staging_dir / job["id"]
            # The downloading claim commits before the runner creates the
            # staging directory; wait for it instead of assuming it exists.
            deadline = time.monotonic() + 3.0
            while not staging_dir.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert staging_dir.exists()

            response = client.post(f"/api/v1/jobs/{job['id']}/cancel")

            assert response.status_code == 200
            final = wait_for_job(client, job["id"], statuses={"cancelled"})
            assert final["status"] == "cancelled"
            assert final["error"]["code"] == "cancelled"

            # Staging cleanup happens right after the cancelled status is
            # persisted; allow a short grace period for it.
            deadline = time.monotonic() + 3.0
            while staging_dir.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not staging_dir.exists()

    def test_cancel_during_cross_filesystem_finalization_does_not_publish_file(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        binaries = app_factory.binaries
        real_link = os.link
        real_copy2 = shutil.copy2
        copy_started = threading.Event()
        allow_copy = threading.Event()

        def cross_device_link(src: Path, dst: Path) -> None:
            if Path(src).is_relative_to(settings.staging_dir):
                raise OSError(errno.EXDEV, "cross-device link")
            real_link(src, dst)

        def blocked_copy(src: Path, dst: Path) -> str:
            copy_started.set()
            assert allow_copy.wait(timeout=5)
            return str(real_copy2(src, dst))

        monkeypatch.setattr(os, "link", cross_device_link)
        monkeypatch.setattr(shutil, "copy2", blocked_copy)

        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()
            assert copy_started.wait(timeout=5)

            try:
                response = client.post(f"/api/v1/jobs/{job['id']}/cancel")
            finally:
                allow_copy.set()

            assert response.status_code == 200
            final = wait_for_job(client, job["id"], statuses={"cancelled", "completed"})
            assert final["status"] == "cancelled", final
            assert not (settings.library_dir / "video" / "Test Video.mp4").exists()

    def test_cancel_queued_job_never_runs_it(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_slow(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
            concurrency=1,
        ) as client:
            analysis = create_analysis(client)
            first = create_job(client, analysis).json()
            wait_for_job(client, first["id"], statuses={"downloading"})

            second = create_job(client, analysis, variant_id=AUDIO_VARIANT).json()
            assert second["status"] == "queued"

            response = client.post(f"/api/v1/jobs/{second['id']}/cancel")
            assert response.status_code == 200

            final = wait_for_job(client, second["id"], statuses={"cancelled"})
            assert final["status"] == "cancelled"
            messages = _activity_messages(settings, second["id"])
            assert "Download started" not in messages

            client.post(f"/api/v1/jobs/{first['id']}/cancel")
            wait_for_job(client, first["id"], statuses={"cancelled"})

    def test_cancel_queued_publishes_committed_activity_before_job_snapshot(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """A stale REST history is completed once by ordered live cancellation."""
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_slow(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
            concurrency=1,
        ) as client:
            analysis = create_analysis(client)
            first = create_job(client, analysis).json()
            wait_for_job(client, first["id"], statuses={"downloading"})
            queued = create_job(client, analysis, variant_id=AUDIO_VARIANT).json()

            # This authoritative snapshot predates the cancellation insert.
            stale_events = client.get(
                f"/api/v1/jobs/{queued['id']}/activity"
            ).json()["events"]
            assert all(event["message"] != "Cancelled" for event in stale_events)

            bus = client.app.state.event_bus
            subscriber = bus.subscribe()
            try:
                response = client.post(f"/api/v1/jobs/{queued['id']}/cancel")
                assert response.status_code == 200
                received: list[dict[str, Any]] = []
                while not subscriber.empty():
                    received.append(subscriber.get_nowait())
            finally:
                bus.unsubscribe(subscriber)

            matching = [
                event
                for event in received
                if event["data"].get("job_id", event["data"].get("id"))
                == queued["id"]
            ]
            assert [event["event"] for event in matching] == [
                "activity.created",
                "job.cancelled",
            ]
            live_activity = matching[0]
            assert live_activity["data"]["message"] == "Cancelled"
            assert live_activity["id"] == live_activity["data"]["id"]
            created_at = datetime.fromisoformat(live_activity["data"]["created_at"])
            assert created_at.utcoffset() == timedelta(0)

            with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
                row = connection.execute(
                    "SELECT id FROM activity_events "
                    "WHERE job_id = ? AND message = 'Cancelled'",
                    (queued["id"],),
                ).fetchone()
            assert row is not None
            assert live_activity["id"] == row[0]

            merged = {
                event["id"]: event for event in [*stale_events, live_activity["data"]]
            }
            assert sum(
                event["message"] == "Cancelled" for event in merged.values()
            ) == 1

            client.post(f"/api/v1/jobs/{first['id']}/cancel")
            wait_for_job(client, first["id"], statuses={"cancelled"})

    def test_cancel_completed_job_returns_409(self, app_factory: Any) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()
            wait_for_job(client, job["id"], statuses={"completed"})

            response = client.post(f"/api/v1/jobs/{job['id']}/cancel")

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "invalid_state"

    def test_cancel_wins_claim_race_and_job_never_runs(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancel lands between the runner's queued read and its transition."""
        binaries = app_factory.binaries
        real_update_if_status = job_repo.update_job_if_status
        race_triggered = {"done": False}

        def hooked_update_if_status(session, job_id, expected_statuses, values):
            if not race_triggered["done"] and values.get("status") == "downloading":
                race_triggered["done"] = True
                connection = sqlite3.connect(settings.state_dir / "downloader.db")
                try:
                    connection.execute(
                        "UPDATE download_jobs SET status = 'cancelled', "
                        "error_code = 'cancelled', error_message = 'cancelled', "
                        "completed_at = ? WHERE id = ? AND status = 'queued'",
                        (datetime.now(UTC).isoformat(), job_id),
                    )
                    connection.commit()
                finally:
                    connection.close()
            return real_update_if_status(session, job_id, expected_statuses, values)

        monkeypatch.setattr(job_repo, "update_job_if_status", hooked_update_if_status)
        with app_factory(
            yt_dlp=fake_ytdlp_slow(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(
                client, job["id"], statuses={"cancelled", "completed", "failed"}
            )

            assert final["status"] == "cancelled", final
            messages = _activity_messages(settings, job["id"])
            assert "Download started" not in messages
            assert not (settings.staging_dir / job["id"]).exists()

    def test_cancel_with_stale_queued_snapshot_stops_active_job(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """The cancel API read `queued` while the runner already started."""
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_slow(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()
            wait_for_job(client, job["id"], statuses={"downloading"})

            service = client.app.state.job_service
            real_get_job = service.get_job
            staled = {"done": False}

            async def stale_first_get_job(actor, job_id):
                record = await real_get_job(actor, job_id)
                if not staled["done"] and record is not None:
                    staled["done"] = True
                    record.status = "queued"
                return record

            service.get_job = stale_first_get_job  # type: ignore[method-assign]

            response = client.post(f"/api/v1/jobs/{job['id']}/cancel")

            assert response.status_code == 200
            assert response.json()["status"] in {"downloading", "cancelled"}
            final = wait_for_job(client, job["id"], statuses={"cancelled"})
            assert final["status"] == "cancelled"
            assert final["error"]["code"] == "cancelled"

            deadline = time.monotonic() + 3.0
            while (settings.staging_dir / job["id"]).exists() and (
                time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert not (settings.staging_dir / job["id"]).exists()

    def test_cancel_between_video_and_audio_steps(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """Cancel at the step boundary must not start the audio download."""
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_steps(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis, variant_id=VIDEO_ONLY_VARIANT).json()

            video_file = settings.staging_dir / job["id"] / "video.mp4"
            deadline = time.monotonic() + 5.0
            while not video_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert video_file.exists()

            response = client.post(f"/api/v1/jobs/{job['id']}/cancel")

            assert response.status_code == 200
            final = wait_for_job(client, job["id"], statuses={"cancelled"})
            assert final["status"] == "cancelled"
            assert not (settings.library_dir / "video" / "Test Video.mkv").exists()

            deadline = time.monotonic() + 3.0
            while (settings.staging_dir / job["id"]).exists() and (
                time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert not (settings.staging_dir / job["id"]).exists()

    def test_cancel_during_merge_does_not_place_file(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """Cancel while FFmpeg is merging must not finalize the media."""
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=fake_ffmpeg_slow(binaries)
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis, variant_id=VIDEO_ONLY_VARIANT).json()

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                current = client.get(f"/api/v1/jobs/{job['id']}").json()
                if current["status"] == "post_processing":
                    break
                time.sleep(0.02)
            assert current["status"] == "post_processing"

            response = client.post(f"/api/v1/jobs/{job['id']}/cancel")

            assert response.status_code == 200
            final = wait_for_job(client, job["id"], statuses={"cancelled"})
            assert final["status"] == "cancelled"
            assert not (settings.library_dir / "video" / "Test Video.mkv").exists()

            deadline = time.monotonic() + 3.0
            while (settings.staging_dir / job["id"]).exists() and (
                time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert not (settings.staging_dir / job["id"]).exists()

    def test_process_registered_after_cancel_is_terminated(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A subprocess registered after cancellation must be terminated."""
        binaries = app_factory.binaries
        real_check = _JobExecution._check_cancelled
        armed = {"done": False}

        async def hooked_check_cancelled(execution):
            if not armed["done"]:
                armed["done"] = True
                execution._cancel_event.set()
                return
            await real_check(execution)

        monkeypatch.setattr(_JobExecution, "_check_cancelled", hooked_check_cancelled)
        with app_factory(
            yt_dlp=fake_ytdlp_steps(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis, variant_id=VIDEO_ONLY_VARIANT).json()

            final = wait_for_job(
                client, job["id"], statuses={"cancelled", "completed", "failed"}
            )

            assert final["status"] == "cancelled", final
            deadline = time.monotonic() + 3.0
            while (settings.staging_dir / job["id"]).exists() and (
                time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert not (settings.staging_dir / job["id"]).exists()

    def test_publish_wins_when_cancel_arrives_at_publish_boundary(
        self, app_factory: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the atomic publish wins the gate, the job completes instead."""
        binaries = app_factory.binaries
        real_link = os.link
        published = threading.Event()

        def hooked_link(src, dst):
            result = real_link(src, dst)
            if Path(dst).name == "Test Video.mp4":
                published.set()
            return result

        monkeypatch.setattr(os, "link", hooked_link)
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            assert published.wait(timeout=5), "final file was never published"
            response = client.post(f"/api/v1/jobs/{job['id']}/cancel")

            final = wait_for_job(client, job["id"], statuses={"completed"})
            assert final["status"] == "completed"
            assert final["final_path"] == "video/Test Video.mp4"
            assert (settings.library_dir / "video" / "Test Video.mp4").exists()
            assert response.status_code in {200, 409}


class TestRestartRecovery:
    def test_active_jobs_marked_interrupted_on_restart(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)

        async def _insert_active_job() -> None:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
            )
            factory = async_sessionmaker(engine)
            now = datetime.now(UTC)
            async with factory() as session:
                session.add(
                    DownloadJob(
                        id="a" * 32,
                        owner_id="local",
                        analysis_id=analysis["analysis_id"],
                        source_url=YOUTUBE_URL,
                        title="Stuck Job",
                        variant_id=MUXED_VARIANT,
                        destination="app_library",
                        conflict_policy="skip",
                        keep_app_copy=False,
                        status="downloading",
                        progress_percent=40.0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.commit()
            await engine.dispose()

        asyncio.run(_insert_active_job())

        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            response = client.get(f"/api/v1/jobs/{'a' * 32}")

        assert response.status_code == 200
        job = response.json()
        assert job["status"] == "failed"
        assert job["error"]["code"] == "interrupted_by_restart"
        assert job["error"]["retryable"] is True

    def test_queued_jobs_survive_restart(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)

        async def _insert_queued_job() -> None:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
            )
            factory = async_sessionmaker(engine)
            now = datetime.now(UTC)
            async with factory() as session:
                session.add(
                    DownloadJob(
                        id="b" * 32,
                        owner_id="local",
                        analysis_id=analysis["analysis_id"],
                        source_url=YOUTUBE_URL,
                        title="Queued Job",
                        variant_id=MUXED_VARIANT,
                        destination="app_library",
                        conflict_policy="skip",
                        keep_app_copy=False,
                        status="queued",
                        progress_percent=0.0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.commit()
            await engine.dispose()

        asyncio.run(_insert_queued_job())

        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            final = wait_for_job(client, "b" * 32, statuses={"completed", "failed"})

        assert final["status"] == "completed"


class TestJobErrorRetryabilityTaxonomy:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("interrupted_by_restart", True),
            ("network_error", True),
            ("timeout", True),
            ("cancelled", False),
            ("authentication_required", False),
            ("no_such_code", False),
        ],
    )
    def test_job_error_code_retryable(self, code: str, expected: bool) -> None:
        assert job_error_code_retryable(code) is expected


class TestSse:
    def test_subscriber_is_ready_at_response_start_and_cleans_up(
        self, app_factory: Any
    ) -> None:
        """Publishing synchronously from response.start cannot miss the subscriber."""
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            application = client.app

        async def scenario() -> bytes:
            bus = application.state.event_bus
            body_chunks: list[bytes] = []
            disconnect = asyncio.Event()

            async def receive() -> dict[str, str]:
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    assert len(bus._subscribers) == 1
                    bus.publish(
                        {
                            "event": "activity.created",
                            "id": 1,
                            "data": {
                                "id": 1,
                                "job_id": "job-1",
                                "created_at": "2026-09-28T12:00:00",
                            },
                        }
                    )
                elif message["type"] == "http.response.body" and message.get("body"):
                    body_chunks.append(message["body"])
                    if b"id: 1\n" in message["body"]:
                        disconnect.set()

            await asyncio.wait_for(
                application(_sse_scope(last_event_id=None), receive, send), timeout=5.0
            )
            assert bus._subscribers == set()
            return b"".join(body_chunks)

        body = asyncio.run(scenario())

        assert _delivered_activity_ids(body) == [1]
        payload = _sse_data_payloads(body)[0]
        assert datetime.fromisoformat(payload["created_at"]).utcoffset() == timedelta(0)

    def test_disconnect_at_response_start_unsubscribes(self, app_factory: Any) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            application = client.app

        async def scenario() -> None:
            disconnect = asyncio.Event()

            async def receive() -> dict[str, str]:
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    assert application.state.event_bus._subscribers
                    disconnect.set()

            await asyncio.wait_for(
                application(_sse_scope(last_event_id=None), receive, send), timeout=5.0
            )

        asyncio.run(scenario())
        assert application.state.event_bus._subscribers == set()

    def test_first_connect_without_last_event_id_replays_nothing(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """First connect (no Last-Event-ID) is live-only (R5 formalization).

        Even with a persisted backlog in the database, a fresh connection
        must not receive any replay: only events published live after the
        connection subscribed are delivered. Initial history comes from
        REST instead (GET /api/v1/jobs/{job_id}/activity).
        """
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            application = client.app

        hwm = asyncio.run(_seed_activity_events(settings, 5))
        assert hwm == 5

        async def scenario() -> bytes:
            bus = application.state.event_bus
            body_chunks: list[bytes] = []
            disconnect = asyncio.Event()

            async def receive() -> dict[str, str]:
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.body" and message.get("body"):
                    body_chunks.append(message["body"])

            app_task = asyncio.create_task(
                application(_sse_scope(last_event_id=None), receive, send)
            )
            deadline = time.monotonic() + 5.0
            while not bus._subscribers:
                assert time.monotonic() < deadline, "endpoint did not subscribe in time"
                await asyncio.sleep(0.01)
            # Backlog exists (ids 1..5); publish one live event on top.
            bus.publish(
                {
                    "event": "activity.created",
                    "id": hwm + 1,
                    "data": {"id": hwm + 1, "message": "live-only marker"},
                }
            )

            async def wait_live() -> None:
                while b"live-only marker" not in b"".join(body_chunks):
                    if app_task.done():
                        raise AssertionError(
                            "stream ended before the live event arrived"
                        )
                    await asyncio.sleep(0.02)

            try:
                await asyncio.wait_for(wait_live(), timeout=10.0)
            finally:
                disconnect.set()
            await asyncio.wait_for(asyncio.shield(app_task), timeout=5.0)
            return b"".join(body_chunks)

        body = asyncio.run(scenario())

        # Only the live event was delivered; the persisted backlog was
        # never replayed to this first connection.
        assert b"live-only marker" in body
        assert b"seeded activity" not in body
        assert _delivered_activity_ids(body) == [hwm + 1]

    def test_replays_activity_events_with_last_event_id(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            variant_id = next(
                fmt["variant_id"]
                for fmt in analysis["formats"]
                if fmt["kind"] == "muxed"
            )
            job = create_job(client, analysis, variant_id=variant_id).json()
            wait_for_job(client, job["id"], statuses={"completed"})
            application = client.app

        body, start_message = asyncio.run(
            _collect_sse_replay(application, timeout_seconds=10.0)
        )

        assert start_message["status"] == 200
        content_type = dict(start_message["headers"])[b"content-type"]
        assert content_type.startswith(b"text/event-stream")
        assert b"event: activity.created" in body
        assert b"id: " in body
        assert b"Download started" in body
        for payload in _sse_data_payloads(body):
            if payload.get("created_at") is not None:
                parsed = datetime.fromisoformat(payload["created_at"])
                assert parsed.utcoffset() == timedelta(0)

    def test_activity_overflow_ends_stream_cleanly_and_unsubscribes(
        self, app_factory: Any, settings: Settings
    ) -> None:
        from app.services.event_bus import _MAX_QUEUE_EVENTS

        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            application = client.app

        async def scenario() -> list[int]:
            bus = application.state.event_bus
            scope = _sse_scope(last_event_id=None)
            body_chunks: list[bytes] = []

            async def receive() -> dict[str, str]:
                # Never inject a client disconnect: the stream must end on
                # its own once the bus disconnects the subscriber.
                await asyncio.Event().wait()
                return {"type": "http.disconnect"}

            async def send(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.body" and message.get("body"):
                    body_chunks.append(message["body"])

            app_task = asyncio.create_task(application(scope, receive, send))
            deadline = time.monotonic() + 5.0
            while not bus._subscribers:
                assert time.monotonic() < deadline, "endpoint did not subscribe in time"
                await asyncio.sleep(0.01)
            for seq in range(1, _MAX_QUEUE_EVENTS + 20):
                bus.publish({"event": "activity.created", "id": seq, "data": {"seq": seq}})
            # The stream must finish without any client disconnect.
            await asyncio.wait_for(app_task, timeout=10.0)

            body = b"".join(body_chunks)
            return [
                int(line.removeprefix(b"id: "))
                for part in body.split(b"\n\n")
                for line in part.splitlines()
                if line.startswith(b"id: ")
            ]

        delivered_ids = asyncio.run(scenario())

        # Buffered activities are delivered gapless and in order; the
        # disconnect sentinel then ends the stream and the generator's
        # finally block unsubscribes, leaving no subscriber on the bus.
        assert delivered_ids == list(range(1, _MAX_QUEUE_EVENTS + 1))
        assert application.state.event_bus._subscribers == set()

    def test_replays_backlog_beyond_one_page_gapless(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """A reconnect replays the full backlog even past one page (200)."""
        total = 250
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            # Lifespan only: applies migrations so the state database exists.
            application = client.app

        hwm = asyncio.run(_seed_activity_events(settings, total))
        assert hwm == total

        body, start_message = asyncio.run(
            _collect_sse_replay(
                application,
                timeout_seconds=10.0,
                until=lambda body: f"id: {hwm}\n".encode() in body,
            )
        )

        assert start_message["status"] == 200
        # Ascending, gapless, duplicate-free delivery of the whole backlog.
        assert _delivered_activity_ids(body) == list(range(1, hwm + 1))

    def test_replayed_ids_from_bus_are_deduped_and_post_hwm_arrives_once(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """Bus activity covered by the replay is dropped; id > HWM arrives once."""
        total = 5
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            application = client.app

        hwm = asyncio.run(_seed_activity_events(settings, total))
        assert hwm == total

        async def scenario() -> bytes:
            bus = application.state.event_bus
            published = {"done": False}

            def publish_once(body: bytes) -> None:
                # Fires while the replay is still streaming (its first
                # event was just sent), i.e. before the live loop runs.
                if published["done"] or b"id: 1\n" not in body:
                    return
                published["done"] = True
                # Replay-covered id: must NOT be delivered a second time.
                bus.publish(
                    {"event": "activity.created", "id": 2, "data": {"again": True}}
                )
                # Committed after the HWM was read: exactly one live delivery.
                bus.publish(
                    {
                        "event": "activity.created",
                        "id": hwm + 1,
                        "data": {"live": True},
                    }
                )
                # Transient events are never deduped.
                bus.publish(
                    {"event": "job.updated", "id": None, "data": {"status": "queued"}}
                )

            return await _drive_sse_until(
                application,
                scope=_sse_scope(last_event_id=b"0"),
                on_body=publish_once,
                done=lambda body: b"event: job.updated" in body
                and f"id: {hwm + 1}\n".encode() in body,
            )

        body = asyncio.run(scenario())

        ids = _delivered_activity_ids(body)
        # Replay covered the whole backlog exactly once, in order.
        assert ids[:total] == list(range(1, total + 1))
        assert ids.count(2) == 1, "replay-covered id must not be re-delivered live"
        assert ids.count(hwm + 1) == 1, "post-HWM live id must arrive exactly once"
        assert ids == list(range(1, total + 1)) + [hwm + 1]
        # The transient event was delivered despite Last-Event-ID being set.
        assert b"event: job.updated" in body

    def test_live_ids_published_during_backlog_replay_arrive_after_it(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """Activity published mid-replay (id > HWM) is fully delivered after it."""
        total = 250  # above the replay page size
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            application = client.app

        hwm = asyncio.run(_seed_activity_events(settings, total))
        assert hwm == total

        async def scenario() -> bytes:
            bus = application.state.event_bus
            published = {"done": False}

            def publish_once(body: bytes) -> None:
                # Fires after the very first replay event of a 250-event
                # paged backlog, so the replay is guaranteed unfinished.
                if published["done"] or b"id: 1\n" not in body:
                    return
                published["done"] = True
                for extra in range(hwm + 1, hwm + 4):
                    bus.publish(
                        {"event": "activity.created", "id": extra, "data": {"id": extra}}
                    )

            return await _drive_sse_until(
                application,
                scope=_sse_scope(last_event_id=b"0"),
                on_body=publish_once,
                done=lambda body: f"id: {hwm + 3}\n".encode() in body,
            )

        body = asyncio.run(scenario())

        # Full backlog replayed gapless, then every post-HWM live id
        # exactly once: nothing skipped, nothing duplicated.
        ids = _delivered_activity_ids(body)
        assert ids == list(range(1, hwm + 4))

    def test_replay_is_bounded_by_high_water_mark_with_rows_above_it(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """Rows persisted above the HWM never appear in the paged replay."""
        total = 250  # above the replay page size (200)
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ):
            # Lifespan only: applies migrations so the state database exists.
            pass

        hwm = asyncio.run(_seed_activity_events(settings, total))
        assert hwm == total
        # Committed after the mark was read: present in the database
        # while the replay below runs, but bound to the live lane only.
        above_mark = asyncio.run(_seed_activity_events(settings, 3))
        assert above_mark == total + 3

        async def scenario() -> list[int]:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
            )
            factory = async_sessionmaker(engine)
            try:
                async with factory() as session:
                    body = "".join(
                        [chunk async for chunk in _replay_backlog(session, 0, hwm)]
                    )
            finally:
                await engine.dispose()
            return _delivered_activity_ids(body.encode())

        ids = asyncio.run(scenario())

        # Exactly 1..250: ascending, gapless, duplicate-free; the rows
        # 251..253 exist in the DB yet are never emitted by the replay.
        assert ids == list(range(1, hwm + 1))

    def test_list_events_after_optionally_bounds_by_max_id(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """max_id narrows the query to id <= max_id; omitting it keeps old behaviour."""
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ):
            # Lifespan only: applies migrations so the state database exists.
            pass

        hwm = asyncio.run(_seed_activity_events(settings, 5))
        assert hwm == 5

        async def scenario() -> None:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
            )
            factory = async_sessionmaker(engine)
            try:
                async with factory() as session:
                    # Without max_id: previous behaviour is unchanged.
                    unbounded = await activity_repo.list_events_after(session, 0)
                    assert [record.id for record in unbounded] == [1, 2, 3, 4, 5]
                    # With max_id: only last_id < id <= max_id comes back.
                    bounded = await activity_repo.list_events_after(
                        session, 0, max_id=3
                    )
                    assert [record.id for record in bounded] == [1, 2, 3]
                    windowed = await activity_repo.list_events_after(
                        session, 2, max_id=4
                    )
                    assert [record.id for record in windowed] == [3, 4]
                    exhausted = await activity_repo.list_events_after(
                        session, 3, max_id=3
                    )
                    assert exhausted == []
            finally:
                await engine.dispose()

        asyncio.run(scenario())

    def test_activity_events_are_persisted(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()
            wait_for_job(client, job["id"], statuses={"completed"})

        with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM activity_events WHERE job_id = ?",
                (job["id"],),
            ).fetchone()[0]
        assert count >= 3


def _sse_scope(last_event_id: bytes | None = b"0") -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = []
    if last_event_id is not None:
        headers.append((b"last-event-id", last_event_id))
    headers.append((b"host", b"testserver"))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }


async def _collect_sse_replay(
    application: Any,
    *,
    timeout_seconds: float,
    until: Callable[[bytes], bool] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Drive the SSE endpoint directly and inject a client disconnect.

    TestClient buffers whole responses, which can never complete for an
    infinite SSE stream; a raw ASGI call lets us read the replayed events and
    verify the generator honors client disconnects. ``until`` decides when
    enough body has been collected (defaults to the original replay check).
    """
    loop_t0 = time.monotonic()

    def stamp(message: str) -> None:
        print(f"[{time.monotonic() - loop_t0:5.2f}s] {message}", flush=True)

    def default_until(body: bytes) -> bool:
        return (
            b"event: activity.created" in body
            and b"id: " in body
            and b"Download started" in body
        )

    body_ready = until or default_until
    scope = _sse_scope()
    start_message: dict[str, Any] = {}
    body_chunks: list[bytes] = []
    disconnect = asyncio.Event()

    async def receive() -> dict[str, str]:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            start_message.update(message)
            stamp(f"response.start status={message['status']}")
            return
        if message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                body_chunks.append(chunk)
            if not message.get("more_body", False):
                stamp("response complete")
                disconnect.set()

    app_task = asyncio.create_task(application(scope, receive, send))

    async def wait_for_enough_events() -> None:
        while True:
            body = b"".join(body_chunks)
            if body_ready(body):
                return
            if app_task.done():
                raise AssertionError(f"stream ended early; body so far: {body!r}")
            await asyncio.sleep(0.05)

    try:
        await asyncio.wait_for(wait_for_enough_events(), timeout=timeout_seconds)
        stamp("collected expected replay events")
    finally:
        disconnect.set()

    try:
        await asyncio.wait_for(asyncio.shield(app_task), timeout=5.0)
        stamp("endpoint honored client disconnect")
    except TimeoutError:
        app_task.cancel()
        raise AssertionError("SSE generator ignored the injected disconnect") from None

    return b"".join(body_chunks), start_message


async def _seed_activity_events(settings: Settings, total: int) -> int:
    """Insert ``total`` activity rows directly; return the new high-water mark."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
    )
    factory = async_sessionmaker(engine)
    try:
        async with factory() as session:
            for seq in range(total):
                await activity_repo.add_activity_event(
                    session,
                    job_id=None,
                    level="info",
                    event_type="job",
                    message=f"seeded activity {seq}",
                )
            await session.commit()
            hwm = await activity_repo.max_event_id(session)
        return hwm
    finally:
        await engine.dispose()


def _delivered_activity_ids(body: bytes) -> list[int]:
    return [
        int(line.removeprefix(b"id: "))
        for part in body.split(b"\n\n")
        for line in part.splitlines()
        if line.startswith(b"id: ")
    ]


def _sse_data_payloads(body: bytes) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for part in body.split(b"\n\n"):
        data = b"\n".join(
            line.removeprefix(b"data: ")
            for line in part.splitlines()
            if line.startswith(b"data: ")
        )
        if data:
            payloads.append(json.loads(data))
    return payloads


async def _drive_sse_until(
    application: Any,
    *,
    scope: dict[str, Any],
    done: Callable[[bytes], bool],
    on_body: Callable[[bytes], None] | None = None,
    timeout_seconds: float = 10.0,
) -> bytes:
    """Raw-ASGI SSE driver with a body predicate and an optional hook.

    ``on_body`` runs on every received body chunk (used to publish to the
    bus while the replay is still streaming); ``done`` ends collection by
    injecting a client disconnect once satisfied.
    """
    body_chunks: list[bytes] = []
    disconnect = asyncio.Event()

    async def receive() -> dict[str, str]:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            body_chunks.append(message["body"])
            if on_body is not None:
                on_body(b"".join(body_chunks))

    app_task = asyncio.create_task(application(scope, receive, send))

    async def wait_done() -> None:
        while True:
            body = b"".join(body_chunks)
            if done(body):
                return
            if app_task.done():
                raise AssertionError(f"stream ended early; body so far: {body!r}")
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(wait_done(), timeout=timeout_seconds)
    finally:
        disconnect.set()

    await asyncio.wait_for(asyncio.shield(app_task), timeout=5.0)
    return b"".join(body_chunks)


class TestEventBus:
    def test_publish_reaches_subscribers(self) -> None:
        from app.services.event_bus import EventBus

        async def scenario() -> None:
            bus = EventBus()
            subscriber = bus.subscribe()
            bus.publish({"event": "job.updated", "data": {"status": "queued"}})
            received = await subscriber.get(timeout=1.0)
            assert received is not None
            bus.unsubscribe(subscriber)
            bus.publish({"event": "ignored", "data": {}})
            assert received["event"] == "job.updated"
            assert subscriber.empty()

        asyncio.run(scenario())

    def test_transient_overflow_drops_oldest(self) -> None:
        from app.services.event_bus import _MAX_QUEUE_EVENTS, EventBus

        async def scenario() -> None:
            bus = EventBus()
            subscriber = bus.subscribe()
            total = _MAX_QUEUE_EVENTS + 2
            for seq in range(1, total + 1):
                bus.publish({"event": "job.updated", "id": None, "data": {"seq": seq}})

            assert subscriber.qsize() == _MAX_QUEUE_EVENTS
            head = await subscriber.get(timeout=1.0)
            assert head is not None
            assert head["data"]["seq"] == 3

            last = head
            while not subscriber.empty():
                last = subscriber.get_nowait()
            assert last["data"]["seq"] == total

        asyncio.run(scenario())

    def test_activity_overflow_disconnects_without_gaps(self) -> None:
        from app.services.event_bus import _MAX_QUEUE_EVENTS, EventBus

        async def scenario() -> None:
            bus = EventBus()
            subscriber = bus.subscribe()
            total = _MAX_QUEUE_EVENTS + 50
            for seq in range(1, total + 1):
                bus.publish({"event": "activity.created", "id": seq, "data": {"seq": seq}})

            assert subscriber.qsize() == _MAX_QUEUE_EVENTS

            delivered: list[int] = []
            while True:
                event = await subscriber.get(timeout=1.0)
                if event is None:
                    break
                assert event["event"] == "activity.created"
                delivered.append(event["id"])

            # Buffered activities arrive gapless and in order, then the
            # disconnect sentinel ends the stream. Ids past the buffer are
            # never delivered (no drop-while-delivery-continues).
            assert delivered == list(range(1, _MAX_QUEUE_EVENTS + 1))

            # Publishing to a disconnected subscriber is a no-op.
            bus.publish({"event": "activity.created", "id": total + 1, "data": {}})
            bus.publish({"event": "job.updated", "id": None, "data": {}})
            assert await subscriber.get(timeout=1.0) is None
            assert subscriber.empty()

        asyncio.run(scenario())

    def test_slow_transient_overflow_does_not_affect_fast_subscriber(self) -> None:
        from app.services.event_bus import _MAX_QUEUE_EVENTS, EventBus

        async def scenario() -> None:
            bus = EventBus()
            fast = bus.subscribe()
            slow = bus.subscribe()

            for seq in range(1, _MAX_QUEUE_EVENTS + 11):
                bus.publish({"event": "job.updated", "id": None, "data": {"seq": seq}})
                received = fast.get_nowait()
                assert received["data"]["seq"] == seq

            assert fast.empty()
            assert slow.qsize() == _MAX_QUEUE_EVENTS
            assert slow.get_nowait()["data"]["seq"] == 11

            last = slow.get_nowait()
            while not slow.empty():
                last = slow.get_nowait()
            assert last["data"]["seq"] == _MAX_QUEUE_EVENTS + 10

        asyncio.run(scenario())

    def test_slow_activity_overflow_does_not_affect_fast_subscriber(self) -> None:
        from app.services.event_bus import _MAX_QUEUE_EVENTS, EventBus

        async def scenario() -> None:
            bus = EventBus()
            fast = bus.subscribe()
            slow = bus.subscribe()

            total = _MAX_QUEUE_EVENTS + 10
            for seq in range(1, total + 1):
                bus.publish({"event": "activity.created", "id": seq, "data": {"seq": seq}})
                received = fast.get_nowait()
                assert received["id"] == seq

            assert fast.empty()

            delivered: list[int] = []
            while True:
                event = await slow.get(timeout=1.0)
                if event is None:
                    break
                delivered.append(event["id"])
            assert delivered == list(range(1, _MAX_QUEUE_EVENTS + 1))

        asyncio.run(scenario())

    def test_reconnect_replay_recovers_activity_events_skipped_by_overflow(
        self, app_factory: Any, settings: Settings
    ) -> None:
        from app.services.event_bus import _MAX_QUEUE_EVENTS, EventBus

        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ):
            # Lifespan only: applies migrations so the state database exists.
            pass

        async def scenario() -> None:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
            )
            factory = async_sessionmaker(engine)
            try:
                total = _MAX_QUEUE_EVENTS + 50
                async with factory() as session:
                    for seq in range(1, total + 1):
                        await activity_repo.add_activity_event(
                            session,
                            job_id=None,
                            level="info",
                            event_type="job",
                            message=f"activity {seq}",
                        )
                    await session.commit()

                bus = EventBus()
                subscriber = bus.subscribe()
                async with factory() as session:
                    persisted = await activity_repo.list_events_after(
                        session, 0, limit=total
                    )
                for record in persisted:
                    bus.publish(
                        {
                            "event": "activity.created",
                            "id": record.id,
                            "data": {"id": record.id},
                        }
                    )

                delivered: list[int] = []
                while True:
                    event = await subscriber.get(timeout=1.0)
                    if event is None:
                        break
                    delivered.append(event["id"])
                last_delivered = delivered[-1]
                assert delivered == list(range(1, last_delivered + 1))

                # The reconnect path (Last-Event-ID -> list_events_after)
                # must return exactly the activities this connection
                # skipped, so nothing is permanently lost.
                async with factory() as session:
                    replayed = await activity_repo.list_events_after(session, last_delivered)
                assert [record.id for record in replayed] == list(
                    range(last_delivered + 1, total + 1)
                )
            finally:
                await engine.dispose()

        asyncio.run(scenario())


class TestModels:
    def test_jobs_tables_registered_on_metadata(self) -> None:
        from app import models  # noqa: F401
        from app.database import Base

        assert "download_jobs" in Base.metadata.tables
        assert "activity_events" in Base.metadata.tables
        job_table = Base.metadata.tables["download_jobs"]
        index_names = {index.name for index in job_table.indexes}
        assert "ix_download_jobs_status_created" in index_names
        assert "ix_download_jobs_owner_created" in index_names

    def test_activity_event_importable(self) -> None:
        assert ActivityEvent.__tablename__ == "activity_events"
