"""Library catalog persistence tests (Milestone 3, ARCHITECTURE.md §23/§37/§51).

Covers the real migration chain (applied on app startup, upgrade/downgrade),
catalog creation on successful App Library finalization, source-id carryover,
the media_files → library_items foreign key, and the completion+catalog
single-transaction guarantees.
"""

import asyncio
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.database import MIGRATIONS_DIR, create_engine_and_sessionmaker
from app.domain.actors import Actor
from app.domain.library import DeleteMediaFileOutcome, MediaStorageType
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs as job_repo
from app.repositories import library as library_repo
from app.services.library_service import LibraryService, stage_media_catalog_reference
from app.services.storage_service import (
    MediaFileMissingError,
    MediaFileTargetChangedError,
    PreparedMediaFile,
    StorageService,
)
from tests.conftest import FakeAnalyzer, build_application
from tests.fake_binaries import (
    fake_ffmpeg_ok,
    fake_ytdlp_fail,
    fake_ytdlp_ok,
    fake_ytdlp_slow,
)
from tests.yt_dlp_samples import single_video_payload

YOUTUBE_URL = "https://www.youtube.com/watch?v=abc123"
MUXED_VARIANT = "video-720"
VIDEO_ONLY_VARIANT = "video-1080"
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
        return TestClient(build_application(settings, analyzer, overrides))

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


def _connect(settings: Settings) -> sqlite3.Connection:
    return sqlite3.connect(settings.state_dir / "downloader.db")


def _fetch_one(settings: Settings, query: str, params: tuple = ()) -> tuple | None:
    with _connect(settings) as connection:
        return connection.execute(query, params).fetchone()


def _row_count(settings: Settings, table: str) -> int:
    row = _fetch_one(settings, f"SELECT COUNT(*) FROM {table}")
    assert row is not None
    return int(row[0])


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


class TestLibraryMigration:
    def test_app_startup_applies_library_migration(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            assert client.get("/api/v1/health").status_code == 200

        with _connect(settings) as connection:
            tables = _table_names(connection)
            version_row = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            item_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(library_items)")
            }
            file_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(media_files)")
            }
            # (id, seq, table, from, to, on_update, on_delete, match)
            foreign_keys = list(
                connection.execute("PRAGMA foreign_key_list(media_files)")
            )

        assert version_row is not None
        assert {"library_items", "media_files"} <= tables
        assert version_row[0] == "0006"
        assert {
            "id",
            "owner_id",
            "job_id",
            "source_url",
            "platform",
            "media_type",
            "title",
            "artist",
            "album",
            "track_number",
            "disc_number",
            "year",
            "format",
            "codec",
            "bitrate",
            "resolution",
            "duration",
            "created_at",
        } <= item_columns
        assert {
            "id",
            "library_item_id",
            "storage_type",
            "path",
            "display_path",
            "size_bytes",
            "checksum_optional",
            "created_at",
        } <= file_columns
        # media_files.library_item_id → library_items.id, cascading on the
        # catalog level only (PRODUCT_SPEC.md §21 Remove from Library).
        assert foreign_keys, "media_files foreign key missing"
        assert all(
            row[2] == "library_items"
            and row[3] == "library_item_id"
            and row[4] == "id"
            for row in foreign_keys
        )
        assert any(row[6] == "CASCADE" for row in foreign_keys)

    def test_migration_upgrades_and_downgrades_cleanly(self, tmp_path: Path) -> None:
        database = tmp_path / "migrate.db"
        alembic_config = Config()
        alembic_config.set_main_option("script_location", str(MIGRATIONS_DIR))
        alembic_config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")

        command.upgrade(alembic_config, "head")
        with sqlite3.connect(database) as connection:
            assert {"library_items", "media_files"} <= _table_names(connection)

        command.downgrade(alembic_config, "0003")
        with sqlite3.connect(database) as connection:
            tables = _table_names(connection)
        assert "library_items" not in tables
        assert "media_files" not in tables

        command.upgrade(alembic_config, "head")
        with sqlite3.connect(database) as connection:
            assert {"library_items", "media_files"} <= _table_names(connection)


class TestLibraryCatalogCreation:
    def test_successful_muxed_download_creates_one_catalog_pair(
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

        # Existing completion behaviour is unchanged.
        assert final["status"] == "completed", final
        assert final["final_path"] == "video/Test Video.mp4"

        item = _fetch_one(
            settings,
            "SELECT id, owner_id, job_id, source_url, source_id, platform, "
            "media_type, title, artist, album, track_number, disc_number, year, "
            "thumbnail_path, format, codec, bitrate, resolution, duration "
            "FROM library_items",
        )
        assert item is not None
        (
            item_id,
            owner_id,
            job_id,
            source_url,
            source_id,
            platform,
            media_type,
            title,
            artist,
            album,
            track_number,
            disc_number,
            year,
            thumbnail_path,
            format,
            codec,
            bitrate,
            resolution,
            duration,
        ) = item
        assert owner_id == "local"
        assert job_id == job["id"]
        assert source_url == YOUTUBE_URL
        # The raw source id from the persisted normalized analysis.
        assert source_id == "abc123"
        # No authoritative source exists yet for these fields.
        assert artist is None
        assert album is None
        assert track_number is None
        assert disc_number is None
        assert year is None
        assert thumbnail_path is None
        # Source-derived metadata.
        assert platform == "youtube"
        assert media_type == "video"
        assert title == "Test Video"
        assert format == "mp4"
        assert codec == "h264"
        assert bitrate is None  # video streams carry no normalized audio bitrate
        assert resolution == "720p"
        assert duration == pytest.approx(205.4)

        media = _fetch_one(
            settings,
            "SELECT library_item_id, storage_type, path, display_path, size_bytes, "
            "checksum_optional FROM media_files",
        )
        assert media is not None
        assert media[0] == item_id
        assert media[1] == "app_library"
        assert media[2] == "video/Test Video.mp4"
        assert media[3] is None  # no display-path mapping configured for the App Library
        assert media[4] == len(b"fake media")
        assert media[5] is None

        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1

    def test_audio_download_catalogs_audio_metadata(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis, variant_id=AUDIO_VARIANT).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})

        assert final["status"] == "completed", final
        assert final["final_path"] == "audio/Test Video.mp4"

        audio_row = _fetch_one(
            settings,
            "SELECT media_type, codec, bitrate, resolution FROM library_items",
        )
        assert audio_row is not None
        media_type, codec, bitrate, resolution = audio_row
        assert media_type == "audio"
        assert codec == "opus"
        assert bitrate == pytest.approx(160.0)
        assert resolution is None

        audio_file_row = _fetch_one(
            settings, "SELECT path, size_bytes FROM media_files"
        )
        assert audio_file_row is not None
        path, size_bytes = audio_file_row
        # The placed file (not the raw stream container) is the physical truth.
        assert path == "audio/Test Video.mp4"
        assert size_bytes == len(b"fake media")

    def test_merged_video_catalog_uses_placed_container(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=fake_ffmpeg_ok(binaries)
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis, variant_id=VIDEO_ONLY_VARIANT).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})

        assert final["status"] == "completed", final
        assert final["final_path"] == "video/Test Video.mkv"

        merged_row = _fetch_one(
            settings, "SELECT media_type, format, codec FROM library_items"
        )
        assert merged_row is not None
        media_type, format, codec = merged_row
        assert media_type == "video"
        assert format == "mkv"  # the merged container, not the raw stream ext
        assert codec == "h264"

        merged_file_row = _fetch_one(
            settings, "SELECT path, size_bytes FROM media_files"
        )
        assert merged_file_row is not None
        path, size_bytes = merged_file_row
        assert path == "video/Test Video.mkv"
        assert size_bytes == len(b"merged-media")

    def test_skip_existing_still_creates_catalog_for_placed_path(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """No catalog dedup rule exists for skip-existing conflicts yet.

        The narrowest behaviour preserving current completion semantics: the
        successful download still gets its own logical item and a physical
        row pointing at the existing file, with the existing file's size.
        """
        binaries = app_factory.binaries
        (settings.library_dir / "video").mkdir(parents=True, exist_ok=True)
        existing = settings.library_dir / "video" / "Test Video.mp4"
        existing.write_text("original")

        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})

        assert final["status"] == "completed", final
        assert existing.read_text() == "original"

        assert _row_count(settings, "library_items") == 1
        skipped_row = _fetch_one(
            settings, "SELECT path, size_bytes FROM media_files"
        )
        assert skipped_row is not None
        path, size_bytes = skipped_row
        assert path == "video/Test Video.mp4"
        assert size_bytes == len(b"original")

    def test_catalog_source_id_null_when_source_has_no_id(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """A source that reports no raw id leaves ``source_id`` null."""
        binaries = app_factory.binaries
        without_id = single_video_payload()
        del without_id["id"]
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
            analyzer_result=without_id,
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(client, job["id"], statuses={"completed", "failed"})

        assert final["status"] == "completed", final
        source_id_row = _fetch_one(settings, "SELECT source_id FROM library_items")
        assert source_id_row is not None
        assert source_id_row[0] is None

    def test_failed_download_creates_no_library_rows(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        failing = fake_ytdlp_fail(binaries, "Unsupported URL: https://example.com/x")
        with app_factory(yt_dlp=failing, ffmpeg=str(binaries / "ffmpeg-missing")) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(client, job["id"], statuses={"failed", "cancelled"})

        assert final["status"] == "failed"
        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0

    def test_cancelled_download_creates_no_library_rows(
        self, app_factory: Any, settings: Settings
    ) -> None:
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_slow(binaries),
            ffmpeg=str(binaries / "ffmpeg-missing"),
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()
            wait_for_job(client, job["id"], statuses={"downloading"})

            response = client.post(f"/api/v1/jobs/{job['id']}/cancel")

            assert response.status_code == 200
            final = wait_for_job(client, job["id"], statuses={"cancelled"})

        assert final["status"] == "cancelled"
        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0


class TestCatalogForeignKeys:
    """media_files → library_items foreign key (PRODUCT_SPEC.md §21).

    The cascade is catalog-level only: deleting a parent row removes its
    child catalog rows and never touches the physical file.
    """

    def test_migration_rejects_orphan_media_file_rows(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            assert client.get("/api/v1/health").status_code == 200

        with _connect(settings) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO media_files "
                    "(library_item_id, storage_type, path, size_bytes, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        "0" * 32,
                        "app_library",
                        "video/orphan.mp4",
                        11,
                        "2026-10-02 00:00:00",
                    ),
                )

    def test_app_engine_rejects_orphan_media_file_rows(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """The application engine enables foreign keys (ARCHITECTURE.md §22)."""
        with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
            assert client.get("/api/v1/health").status_code == 200

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )

        async def insert_orphan() -> None:
            async with session_factory() as session:
                session.add(
                    MediaFile(
                        library_item_id="0" * 32,
                        storage_type=MediaStorageType.APP_LIBRARY.value,
                        path="video/orphan.mp4",
                        size_bytes=1,
                        created_at=datetime.now(UTC),
                    )
                )
                await session.commit()

        try:
            with pytest.raises(IntegrityError):
                asyncio.run(insert_orphan())
        finally:
            asyncio.run(engine.dispose())

    def test_parent_delete_cascades_catalog_rows_and_keeps_physical_file(
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
        final_file = settings.library_dir / "video" / "Test Video.mp4"
        assert final_file.is_file()
        item_row = _fetch_one(settings, "SELECT id FROM library_items")
        assert item_row is not None
        assert _row_count(settings, "media_files") == 1

        with _connect(settings) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM library_items WHERE id = ?", (item_row[0],))
            connection.commit()

        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0
        # Remove from Library (database-only) leaves the file untouched.
        assert final_file.read_text() == "fake media"


class TestCompletionTransaction:
    def test_catalog_insert_failure_rolls_back_completion(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed catalog write must not leave a completed job behind."""

        async def failing_add_media_file(session: Any, record: Any) -> None:
            raise RuntimeError("media file insert failed")

        monkeypatch.setattr(library_repo, "add_media_file", failing_add_media_file)
        binaries = app_factory.binaries
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(
                client, job["id"], statuses={"failed", "cancelled", "completed"}
            )

        assert final["status"] == "failed", final
        assert final["error"]["code"] == "internal_error"
        failed_row = _fetch_one(
            settings,
            "SELECT status, final_path FROM download_jobs WHERE id = ?",
            (job["id"],),
        )
        assert failed_row is not None
        status, final_path = failed_row
        assert status == "failed"
        assert final_path is None
        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0
        # The finalized physical file remains on disk (ARCHITECTURE.md §51);
        # only the database reporting must stay honest.

    def test_cancel_wins_completion_race_writes_no_catalog(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale runner that lost the status race writes no catalog rows."""
        binaries = app_factory.binaries
        real_stage = job_repo.stage_job_update_if_status
        raced = {"done": False}

        def hooked_stage(
            session: Any,
            job_id: str,
            expected_statuses: set[str],
            values: dict[str, Any],
        ) -> Any:
            if not raced["done"] and values.get("status") == "completed":
                raced["done"] = True
                connection = sqlite3.connect(settings.state_dir / "downloader.db")
                try:
                    connection.execute(
                        "UPDATE download_jobs SET status = 'cancelled', "
                        "error_code = 'cancelled', error_message = 'cancelled', "
                        "completed_at = ? WHERE id = ?",
                        (datetime.now(UTC).isoformat(), job_id),
                    )
                    connection.commit()
                finally:
                    connection.close()
            return real_stage(session, job_id, expected_statuses, values)

        monkeypatch.setattr(
            job_repo, "stage_job_update_if_status", hooked_stage
        )
        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(
                client,
                job["id"],
                statuses={"cancelled", "completed", "failed"},
            )

        assert final["status"] == "cancelled", final
        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0

    def test_delete_file_win_rolls_back_completion(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M3-LIB-6-R1: a Delete File that commits while the completion
        is committing its catalog rows must not leave a completed job or
        a dangling media_files row behind (ARCHITECTURE.md §38, §51)."""
        binaries = app_factory.binaries
        real_stage = job_repo.stage_job_update_if_status
        raced = {"done": False}

        def _delete_seeded_reference() -> None:
            engine, session_factory = create_engine_and_sessionmaker(
                f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
            )
            storage = StorageService(settings.staging_dir, settings.library_dir)
            storage.initialize_delete_roots()
            service = LibraryService(session_factory, storage)
            try:
                outcome = asyncio.run(
                    service.delete_media_file(
                        Actor(), item_id="d" * 32, media_file_id=1
                    )
                )
            finally:
                asyncio.run(engine.dispose())
            assert outcome is DeleteMediaFileOutcome.DELETED

        def hooked_stage(
            session: Any,
            job_id: str,
            expected_statuses: set[str],
            values: dict[str, Any],
        ) -> Any:
            if not raced["done"] and values.get("status") == "completed":
                raced["done"] = True
                # The delete wins the write lock and fully commits (row
                # and physical file) before the completion's first write
                # statement executes — exactly the state a completion
                # that waited on the delete's lock observes.
                thread = threading.Thread(target=_delete_seeded_reference)
                thread.start()
                thread.join(timeout=10)
            return real_stage(session, job_id, expected_statuses, values)

        monkeypatch.setattr(job_repo, "stage_job_update_if_status", hooked_stage)

        with app_factory(
            yt_dlp=fake_ytdlp_ok(binaries), ffmpeg=str(binaries / "ffmpeg-missing")
        ) as client:
            # A pre-existing catalogued copy at the exact path the job's
            # skip-existing placement will observe.
            media_file = settings.library_dir / "video" / "Test Video.mp4"
            media_file.parent.mkdir(parents=True, exist_ok=True)
            media_file.write_text("real media bytes")
            with _connect(settings) as connection:
                connection.execute(
                    "INSERT INTO library_items "
                    "(id, owner_id, job_id, source_url, media_type, title, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "d" * 32,
                        "local",
                        "j" * 32,
                        YOUTUBE_URL,
                        "video",
                        "Test Video",
                        "2026-10-01 10:00:00",
                    ),
                )
                connection.execute(
                    "INSERT INTO media_files "
                    "(library_item_id, storage_type, path, size_bytes, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    ("d" * 32, "app_library", "video/Test Video.mp4", 17,
                     "2026-10-01 10:00:00"),
                )
                connection.commit()

            analysis = create_analysis(client)
            job = create_job(client, analysis).json()

            final = wait_for_job(
                client, job["id"], statuses={"failed", "cancelled", "completed"}
            )

        # The delete removed the file the placement had observed, so the
        # completion must not report success nor commit catalog rows.
        assert final["status"] == "failed", final
        assert final["error"]["code"] == "internal_error"
        failed_row = _fetch_one(
            settings,
            "SELECT status, final_path FROM download_jobs WHERE id = ?",
            (job["id"],),
        )
        assert failed_row is not None
        status, final_path = failed_row
        assert status == "failed"
        assert final_path is None
        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0
        assert not media_file.exists()


class TestConcurrentDeleteWriteProtocol:
    """M3-LIB-6-R1 regression coverage for the catalog-write protocol.

    A catalog write that commits a ``media_files`` row must hold the
    SQLite write lock and re-verify its physical target through the
    storage boundary first: a Delete File that won the lock must never
    leave a dangling committed reference behind, while a writer that
    committed first blocks the delete as a shared reference without
    mutation, and unrelated paths keep flowing.
    """

    @staticmethod
    def _writer_rows(path: str, size_bytes: int) -> tuple[LibraryItem, MediaFile]:
        now = datetime.now(UTC)
        item = LibraryItem(
            id="2" * 32,
            owner_id="local",
            job_id="w" * 32,
            source_url=YOUTUBE_URL,
            media_type="video",
            title="Test Video",
            created_at=now,
        )
        media_file = MediaFile(
            library_item_id=item.id,
            storage_type=MediaStorageType.APP_LIBRARY.value,
            path=path,
            size_bytes=size_bytes,
            created_at=now,
        )
        return item, media_file

    @staticmethod
    def _seed_reference(settings: Settings, *, path: str) -> int:
        with _connect(settings) as connection:
            connection.execute(
                "INSERT INTO library_items "
                "(id, owner_id, job_id, source_url, media_type, title, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "1" * 32,
                    "local",
                    "j" * 32,
                    YOUTUBE_URL,
                    "video",
                    "Test Video",
                    "2026-10-01 10:00:00",
                ),
            )
            cursor = connection.execute(
                "INSERT INTO media_files "
                "(library_item_id, storage_type, path, size_bytes, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("1" * 32, "app_library", path, 17, "2026-10-01 10:00:00"),
            )
            connection.commit()
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    @staticmethod
    def _write_source(settings: Settings, name: str, content: str) -> Path:
        staging = settings.staging_dir / "protocol-src"
        staging.mkdir(parents=True, exist_ok=True)
        source = staging / name
        source.write_text(content)
        return source

    def test_waiting_writer_rolls_back_after_delete_commits(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The core R1 race: the writer's catalog insert genuinely waits
        on the delete's SQLite write lock (busy_timeout), the delete then
        commits its unlink, and the woken writer must refuse to commit —
        no dangling media_files row, no item row."""
        with app_factory():
            media_file = settings.library_dir / "video" / "Test Video.mp4"
            media_file.parent.mkdir(parents=True, exist_ok=True)
            media_file.write_text("real media bytes")
            copy_id = self._seed_reference(
                settings, path="video/Test Video.mp4"
            )

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()
        service = LibraryService(session_factory, storage)

        delete_in_flight = threading.Event()
        allow_unlink = threading.Event()
        real_fs_delete = PreparedMediaFile.delete

        def gated_fs_delete(self: PreparedMediaFile) -> Path:
            delete_in_flight.set()
            assert allow_unlink.wait(timeout=5)
            return real_fs_delete(self)

        monkeypatch.setattr(PreparedMediaFile, "delete", gated_fs_delete)

        async def main() -> None:
            delete_task = asyncio.create_task(
                service.delete_media_file(
                    Actor(), item_id="1" * 32, media_file_id=copy_id
                )
            )
            assert await asyncio.to_thread(delete_in_flight.wait, 5)

            # The writer finalizes now and observes the still-existing
            # file: a real skip-existing placement captures its identity.
            source = self._write_source(
                settings, "Test Video.mp4", "real media bytes"
            )
            placement = await storage.place_final(
                source, media_kind="video", title="Test Video"
            )
            assert placement.skipped_existing is True
            item, writer_media = self._writer_rows(
                placement.relative_path, placement.size_bytes
            )

            async with session_factory() as session:
                writer_task = asyncio.create_task(
                    stage_media_catalog_reference(
                        session,
                        storage,
                        item,
                        writer_media,
                        placement_identity=placement.identity,
                    )
                )
                # The writer's first write statement now genuinely waits
                # on the delete's write lock; nothing can commit yet.
                await asyncio.sleep(0.3)
                assert not writer_task.done()
                allow_unlink.set()
                assert (await delete_task) is DeleteMediaFileOutcome.DELETED
                with pytest.raises(MediaFileMissingError):
                    await writer_task
                await session.rollback()

        try:
            asyncio.run(main())
        finally:
            allow_unlink.set()
            asyncio.run(engine.dispose())

        assert not media_file.exists()
        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0

    def test_writer_of_different_path_completes_after_delete_commits(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A writer targeting a different physical path waits for the
        lock like anyone else but must not be blocked beyond it: its
        verification passes and its catalog rows commit."""
        with app_factory():
            deleted_file = settings.library_dir / "video" / "Test Video.mp4"
            deleted_file.parent.mkdir(parents=True, exist_ok=True)
            deleted_file.write_text("real media bytes")
            copy_id = self._seed_reference(
                settings, path="video/Test Video.mp4"
            )

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()
        service = LibraryService(session_factory, storage)

        delete_in_flight = threading.Event()
        allow_unlink = threading.Event()
        real_fs_delete = PreparedMediaFile.delete

        def gated_fs_delete(self: PreparedMediaFile) -> Path:
            delete_in_flight.set()
            assert allow_unlink.wait(timeout=5)
            return real_fs_delete(self)

        monkeypatch.setattr(PreparedMediaFile, "delete", gated_fs_delete)

        async def main() -> None:
            delete_task = asyncio.create_task(
                service.delete_media_file(
                    Actor(), item_id="1" * 32, media_file_id=copy_id
                )
            )
            assert await asyncio.to_thread(delete_in_flight.wait, 5)

            source = self._write_source(settings, "Other.mp4", "other media")
            placement = await storage.place_final(
                source, media_kind="video", title="Other"
            )
            assert placement.skipped_existing is False
            item, writer_media = self._writer_rows(
                placement.relative_path, placement.size_bytes
            )

            async with session_factory() as session:
                writer_task = asyncio.create_task(
                    stage_media_catalog_reference(
                        session,
                        storage,
                        item,
                        writer_media,
                        placement_identity=placement.identity,
                    )
                )
                await asyncio.sleep(0.3)
                assert not writer_task.done()
                allow_unlink.set()
                assert (await delete_task) is DeleteMediaFileOutcome.DELETED
                await writer_task
                await session.commit()

        try:
            asyncio.run(main())
        finally:
            allow_unlink.set()
            asyncio.run(engine.dispose())

        assert not deleted_file.exists()
        other_file = settings.library_dir / "video" / "Other.mp4"
        assert other_file.read_text() == "other media"
        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1

    def test_committed_writer_reference_blocks_delete_without_mutation(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """Writer wins: once the writer's row is committed, a delete of
        the older reference sees it in the catalog-wide scan and blocks
        as a shared reference — no database or filesystem mutation."""
        with app_factory():
            media_file = settings.library_dir / "video" / "Test Video.mp4"
            media_file.parent.mkdir(parents=True, exist_ok=True)
            media_file.write_text("real media bytes")
            copy_id = self._seed_reference(
                settings, path="video/Test Video.mp4"
            )

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()
        service = LibraryService(session_factory, storage)

        async def main() -> DeleteMediaFileOutcome:
            source = self._write_source(
                settings, "Test Video.mp4", "real media bytes"
            )
            placement = await storage.place_final(
                source, media_kind="video", title="Test Video"
            )
            assert placement.skipped_existing is True
            item, writer_media = self._writer_rows(
                placement.relative_path, placement.size_bytes
            )
            async with session_factory() as session:
                await stage_media_catalog_reference(
                    session,
                    storage,
                    item,
                    writer_media,
                    placement_identity=placement.identity,
                )
                await session.commit()
            return await service.delete_media_file(
                Actor(), item_id="1" * 32, media_file_id=copy_id
            )

        try:
            outcome = asyncio.run(main())
        finally:
            asyncio.run(engine.dispose())

        assert outcome is DeleteMediaFileOutcome.SHARED_PATH
        assert media_file.read_text() == "real media bytes"
        assert _row_count(settings, "library_items") == 2
        assert _row_count(settings, "media_files") == 2

    def test_replaced_target_identity_rolls_back_catalog_write(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """Fail-closed identity validation: when the target was replaced
        by a different inode between placement and the catalog write,
        the protocol refuses to commit the referencing row."""
        with app_factory():
            media_file = settings.library_dir / "video" / "Test Video.mp4"
            media_file.parent.mkdir(parents=True, exist_ok=True)
            media_file.write_text("real media bytes")

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()

        async def main() -> None:
            source = self._write_source(
                settings, "Test Video.mp4", "real media bytes"
            )
            placement = await storage.place_final(
                source, media_kind="video", title="Test Video"
            )
            assert placement.skipped_existing is True
            # Replace the observed file with a different inode.
            media_file.unlink()
            media_file.write_text("replacement media")
            item, writer_media = self._writer_rows(
                placement.relative_path, placement.size_bytes
            )
            async with session_factory() as session:
                with pytest.raises(MediaFileTargetChangedError):
                    await stage_media_catalog_reference(
                        session,
                        storage,
                        item,
                        writer_media,
                        placement_identity=placement.identity,
                    )
                await session.rollback()

        try:
            asyncio.run(main())
        finally:
            asyncio.run(engine.dispose())

        assert media_file.read_text() == "replacement media"
        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0
