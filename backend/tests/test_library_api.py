"""Library list/detail/remove API tests (Milestone 3, PRODUCT_SPEC.md §20–21).

Owner-scoped catalog access: deterministic newest-first pages with
``has_more``, full detail with physical copies, owner isolation (same
404 as unknown ids), query validation via the generic 422 shape, and a
bounded-query guard against N+1 media-file loads.

"Remove from Library" is record-only (§21): the catalog rows go away in
one transaction, the physical media file stays on disk, and unknown or
foreign-owner ids share one 404 without leaking existence.
"""

import asyncio
import errno
import os
import sqlite3
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import create_engine_and_sessionmaker
from app.domain.actors import Actor
from app.domain.library import DeleteMediaFileOutcome, MediaStorageType
from app.models.library import LibraryItem, MediaFile
from app.repositories import library as library_repo
from app.services.library_mapping import (
    library_item_detail_dto,
    library_item_list_dto,
    media_file_to_dto,
)
from app.services.library_service import LibraryService
from app.services.storage_service import (
    PreparedMediaFile,
    StorageDeleteFailedError,
    StorageService,
)
from tests.conftest import FakeAnalyzer, build_application

YOUTUBE_URL = "https://www.youtube.com/watch?v=abc123"
JOB_ID = "j" * 32

T_OLD = "2026-10-01 10:00:00"
T_NEW = "2026-10-02 10:00:00"
T_NEWER = "2026-10-03 10:00:00"


@pytest.fixture
def app_factory(
    settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path
) -> Any:
    binaries = tmp_path / "bin"
    binaries.mkdir()

    def _make() -> TestClient:
        return TestClient(
            build_application(
                settings,
                analyzer,
                {
                    "download_concurrency": 2,
                    "queue_poll_interval_seconds": 0.05,
                    "progress_min_interval_seconds": 0.0,
                },
            )
        )

    _make.binaries = binaries  # type: ignore[attr-defined]
    return _make


def _connect(settings: Settings) -> sqlite3.Connection:
    return sqlite3.connect(settings.state_dir / "downloader.db")


def insert_library_item(
    settings: Settings,
    *,
    item_id: str,
    created_at: str,
    owner_id: str = "local",
    job_id: str = JOB_ID,
    title: str = "Test Video",
    media_type: str = "video",
    source_url: str = YOUTUBE_URL,
    source_id: str | None = "abc123",
    platform: str | None = "youtube",
    artist: str | None = None,
    album: str | None = None,
    item_format: str | None = "mp4",
    codec: str | None = "h264",
    bitrate: float | None = None,
    resolution: str | None = "720p",
    duration: float | None = 205.4,
) -> None:
    with _connect(settings) as connection:
        connection.execute(
            "INSERT INTO library_items "
            "(id, owner_id, job_id, source_url, source_id, platform, media_type, "
            "title, artist, album, format, codec, bitrate, resolution, duration, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                item_format,
                codec,
                bitrate,
                resolution,
                duration,
                created_at,
            ),
        )
        connection.commit()


def insert_media_file(
    settings: Settings,
    *,
    library_item_id: str,
    path: str,
    size_bytes: int,
    created_at: str,
    storage_type: str = "app_library",
    display_path: str | None = None,
) -> int:
    with _connect(settings) as connection:
        cursor = connection.execute(
            "INSERT INTO media_files "
            "(library_item_id, storage_type, path, display_path, size_bytes, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (library_item_id, storage_type, path, display_path, size_bytes, created_at),
        )
        connection.commit()
        row_id = cursor.lastrowid
        assert row_id is not None
    return row_id


def assert_utc_aware(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0)


def _row_count(settings: Settings, table: str) -> int:
    with _connect(settings) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def _column_values(settings: Settings, query: str) -> list[Any]:
    with _connect(settings) as connection:
        return [row[0] for row in connection.execute(query)]


class TestLibraryList:
    def test_empty_library_returns_empty_page_with_defaults(
        self, app_factory: Any
    ) -> None:
        with app_factory() as client:
            response = client.get("/api/v1/library")

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"items", "limit", "offset", "has_more"}
        assert body["items"] == []
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert body["has_more"] is False

    def test_list_maps_item_and_media_file_fields(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=11,
                created_at=T_NEW,
            )

            response = client.get("/api/v1/library")

        assert response.status_code == 200
        body = response.json()
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert body["has_more"] is False
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert set(item) == {
            "id",
            "job_id",
            "title",
            "media_type",
            "format",
            "created_at",
            "media_files",
        }
        assert item["id"] == "1" * 32
        assert item["job_id"] == JOB_ID
        assert item["title"] == "Test Video"
        assert item["media_type"] == "video"
        assert item["format"] == "mp4"
        assert_utc_aware(item["created_at"])
        assert len(item["media_files"]) == 1
        media = item["media_files"][0]
        assert set(media) == {
            "id",
            "storage_type",
            "path",
            "display_path",
            "size_bytes",
            "created_at",
        }
        assert media["storage_type"] == "app_library"
        assert media["path"] == "video/Test Video.mp4"
        assert media["display_path"] is None
        assert media["size_bytes"] == 11
        assert_utc_aware(media["created_at"])

    def test_list_is_newest_first_with_id_desc_tie_break(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(
                settings, item_id="0" * 32, created_at=T_OLD, title="Old"
            )
            insert_library_item(
                settings, item_id="a" * 32, created_at=T_NEW, title="Tie A"
            )
            insert_library_item(
                settings, item_id="b" * 32, created_at=T_NEW, title="Tie B"
            )
            insert_library_item(
                settings, item_id="c" * 32, created_at=T_NEWER, title="Newest"
            )

            response = client.get("/api/v1/library")

        assert response.status_code == 200
        titles = [item["title"] for item in response.json()["items"]]
        # created_at DESC, then id DESC for identical timestamps.
        assert titles == ["Newest", "Tie B", "Tie A", "Old"]

    def test_pagination_slices_pages_and_reports_has_more(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            for index in range(3):
                insert_library_item(
                    settings,
                    item_id=f"{index}" * 32,
                    created_at=f"2026-10-0{index + 1} 10:00:00",
                )

            first = client.get("/api/v1/library", params={"limit": 2})
            second = client.get(
                "/api/v1/library", params={"limit": 2, "offset": 2}
            )
            past_end = client.get(
                "/api/v1/library", params={"limit": 2, "offset": 4}
            )

        assert first.status_code == 200
        assert first.json()["limit"] == 2
        assert first.json()["offset"] == 0
        assert first.json()["has_more"] is True
        assert [item["id"] for item in first.json()["items"]] == [
            "2" * 32,
            "1" * 32,
        ]
        assert second.status_code == 200
        assert second.json()["offset"] == 2
        assert second.json()["has_more"] is False
        assert [item["id"] for item in second.json()["items"]] == ["0" * 32]
        assert past_end.status_code == 200
        assert past_end.json()["items"] == []
        assert past_end.json()["has_more"] is False

    @pytest.mark.parametrize(
        "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}]
    )
    def test_invalid_query_fails_generic_validation(
        self, app_factory: Any, params: dict[str, int]
    ) -> None:
        with app_factory() as client:
            response = client.get("/api/v1/library", params=params)

        assert response.status_code == 422
        error = response.json()["error"]
        # The stable part of the app-wide generic validation body. The
        # historical resource-specific extra field is app-wide contract
        # and deliberately not asserted here.
        assert error["code"] == "invalid_request"
        assert error["message"] == "The request payload is invalid."
        assert error["retryable"] is False

    def test_list_excludes_other_owners_items(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            insert_library_item(
                settings,
                item_id="2" * 32,
                created_at=T_NEWER,
                owner_id="someone-else",
                title="Foreign",
            )

            response = client.get("/api/v1/library")

        assert response.status_code == 200
        ids = [item["id"] for item in response.json()["items"]]
        assert ids == ["1" * 32]


class TestLibraryDetail:
    def test_detail_maps_all_catalog_fields(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(
                settings,
                item_id="1" * 32,
                created_at=T_NEW,
                media_type="audio",
                title="Song",
                source_id="vid987",
                artist="Artist",
                album="Album",
                item_format="mp4",
                codec="opus",
                bitrate=160.0,
                resolution=None,
                duration=195.5,
            )
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="audio/Song.mp4",
                size_bytes=4200,
                created_at=T_NEW,
            )

            response = client.get(f"/api/v1/library/{'1' * 32}")

        assert response.status_code == 200
        item = response.json()
        assert set(item) == {
            "id",
            "job_id",
            "title",
            "media_type",
            "source_url",
            "source_id",
            "platform",
            "artist",
            "album",
            "created_at",
            "format",
            "codec",
            "bitrate",
            "resolution",
            "duration",
            "media_files",
        }
        assert item["id"] == "1" * 32
        assert item["job_id"] == JOB_ID
        assert item["title"] == "Song"
        assert item["media_type"] == "audio"
        assert item["source_url"] == YOUTUBE_URL
        assert item["source_id"] == "vid987"
        assert item["platform"] == "youtube"
        assert item["artist"] == "Artist"
        assert item["album"] == "Album"
        assert_utc_aware(item["created_at"])
        assert item["format"] == "mp4"
        assert item["codec"] == "opus"
        assert item["bitrate"] == 160.0
        assert item["resolution"] is None
        assert item["duration"] == 195.5
        assert [media["path"] for media in item["media_files"]] == [
            "audio/Song.mp4"
        ]

    def test_detail_orders_multiple_copies_created_at_then_id(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """One logical item may have several physical copies
        (ARCHITECTURE.md §23); the copy order is stable."""
        with app_factory() as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            # Insertion order differs from copy order: ids 1, 2, 3 with
            # created_at NEW-early, OLD, NEW-early.
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="plex/Late.mp4",
                size_bytes=30,
                created_at="2026-10-02 12:05:00",
                storage_type="plex_music",
                display_path="/media/music/Late.mp4",
            )
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/First.mp4",
                size_bytes=10,
                created_at="2026-10-02 12:00:00",
            )
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Second.mp4",
                size_bytes=20,
                created_at="2026-10-02 12:00:00",
            )

            response = client.get(f"/api/v1/library/{'1' * 32}")

        assert response.status_code == 200
        copies = response.json()["media_files"]
        assert [copy["path"] for copy in copies] == [
            "video/First.mp4",
            "video/Second.mp4",
            "plex/Late.mp4",
        ]
        by_path = {copy["path"]: copy for copy in copies}
        assert by_path["plex/Late.mp4"]["storage_type"] == "plex_music"
        assert by_path["plex/Late.mp4"]["display_path"] == "/media/music/Late.mp4"
        assert by_path["video/First.mp4"]["display_path"] is None

    def test_unknown_item_returns_404(self, app_factory: Any) -> None:
        with app_factory() as client:
            response = client.get("/api/v1/library/deadbeef")

        assert response.status_code == 404
        assert response.json()["error"] == {
            "code": "library_item_not_found",
            "message": "This library item does not exist.",
            "retryable": False,
            "item_id": None,
        }

    def test_foreign_owner_item_returns_same_404_as_unknown(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(
                settings,
                item_id="f" * 32,
                created_at=T_NEW,
                owner_id="someone-else",
            )
            foreign = client.get(f"/api/v1/library/{'f' * 32}")
            unknown = client.get("/api/v1/library/doesnotexist")

        assert foreign.status_code == 404
        assert unknown.status_code == 404
        # Existence of a foreign item must not leak: identical bodies.
        assert foreign.json() == unknown.json()


class TestRemoveFromLibrary:
    """Record-only removal (PRODUCT_SPEC.md §21, ARCHITECTURE.md §38)."""

    def test_remove_deletes_catalog_rows_and_keeps_physical_file(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            video_dir = settings.library_dir / "video"
            video_dir.mkdir(parents=True, exist_ok=True)
            media_file = video_dir / "Test Video.mp4"
            media_file.write_text("real media bytes")

            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=17,
                created_at=T_NEW,
            )
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="plex/Test Video.mp4",
                size_bytes=19,
                created_at="2026-10-02 12:05:00",
                storage_type="plex_video",
                display_path="/media/videos/Test Video.mp4",
            )
            # A second item must survive the removal untouched.
            insert_library_item(
                settings, item_id="2" * 32, created_at=T_NEWER, title="Keep"
            )
            insert_media_file(
                settings,
                library_item_id="2" * 32,
                path="video/Keep.mp4",
                size_bytes=23,
                created_at=T_NEWER,
            )

            response = client.delete(f"/api/v1/library/{'1' * 32}")

            assert response.status_code == 204
            assert response.content == b""

        # The catalog record and every bound physical-copy row are gone...
        assert _column_values(settings, "SELECT id FROM library_items") == ["2" * 32]
        assert _column_values(settings, "SELECT path FROM media_files") == [
            "video/Keep.mp4"
        ]
        # ...but the real media file on disk is untouched (record only).
        assert media_file.read_text() == "real media bytes"

    def test_remove_unknown_item_returns_404(self, app_factory: Any) -> None:
        with app_factory() as client:
            response = client.delete("/api/v1/library/deadbeef")

        assert response.status_code == 404
        assert response.json()["error"] == {
            "code": "library_item_not_found",
            "message": "This library item does not exist.",
            "retryable": False,
            "item_id": None,
        }

    def test_remove_foreign_owner_item_returns_same_404_and_keeps_rows(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(
                settings,
                item_id="f" * 32,
                created_at=T_NEW,
                owner_id="someone-else",
            )
            insert_media_file(
                settings,
                library_item_id="f" * 32,
                path="video/Foreign.mp4",
                size_bytes=7,
                created_at=T_NEW,
            )

            foreign = client.delete(f"/api/v1/library/{'f' * 32}")
            unknown = client.delete("/api/v1/library/doesnotexist")

        assert foreign.status_code == 404
        assert unknown.status_code == 404
        # Existence of a foreign item must not leak: identical bodies.
        assert foreign.json() == unknown.json()
        # The foreign catalog rows are unchanged.
        assert _column_values(settings, "SELECT id FROM library_items") == ["f" * 32]
        assert _column_values(settings, "SELECT path FROM media_files") == [
            "video/Foreign.mp4"
        ]

    def test_removed_item_is_unknown_after_success(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            first = client.delete(f"/api/v1/library/{'1' * 32}")
            second = client.delete(f"/api/v1/library/{'1' * 32}")
            detail = client.get(f"/api/v1/library/{'1' * 32}")

        assert first.status_code == 204
        assert second.status_code == 404
        assert detail.status_code == 404

    def test_remove_failure_before_commit_rolls_back_catalog_rows(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failure between the staged delete and the commit must leave
        every catalog row in place (single-transaction semantics)."""
        with app_factory():
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=11,
                created_at=T_NEW,
            )

        real_delete = library_repo.delete_library_item

        async def failing_delete(session: Any, item_id: str, owner_id: str) -> bool:
            # Stage the real owner-scoped delete, then fail before any commit.
            await real_delete(session, item_id, owner_id)
            raise RuntimeError("delete failed before commit")

        monkeypatch.setattr(library_repo, "delete_library_item", failing_delete)

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()
        service = LibraryService(session_factory, storage)
        try:
            with pytest.raises(RuntimeError, match="delete failed before commit"):
                asyncio.run(service.remove_item(Actor(), item_id="1" * 32))
        finally:
            asyncio.run(engine.dispose())

        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1


class TestDeleteFile:
    """Physical file deletion (PRODUCT_SPEC.md §21, ARCHITECTURE.md §38).

    Deletion targets one catalogued copy by id, never a client path.
    Blocked outcomes must leave the database and the file untouched.
    """

    @staticmethod
    def _write_media_file(settings: Settings, relative: str, content: str) -> Path:
        target = settings.library_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def test_unique_app_library_copy_deletes_file_and_rows(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            media_file = self._write_media_file(
                settings, "video/Test Video.mp4", "real media bytes"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=17,
                created_at=T_NEW,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 204
            assert response.content == b""

        assert not media_file.exists()
        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0
        # The deleted item is gone: repeat and detail both 404.
        with app_factory() as client:
            repeat = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )
            detail = client.get(f"/api/v1/library/{'1' * 32}")
        assert repeat.status_code == 404
        assert detail.status_code == 404

    def test_selected_copy_only_preserves_sibling_copy_and_parent(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            first = self._write_media_file(settings, "video/A.mp4", "a")
            second = self._write_media_file(settings, "video/B.mp4", "b")
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            first_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/A.mp4",
                size_bytes=1,
                created_at=T_NEW,
            )
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/B.mp4",
                size_bytes=1,
                created_at="2026-10-02 12:05:00",
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{first_id}"
            )

            assert response.status_code == 204

        assert not first.exists()
        assert second.read_text() == "b"
        assert _column_values(settings, "SELECT id FROM library_items") == ["1" * 32]
        assert _column_values(settings, "SELECT path FROM media_files") == [
            "video/B.mp4"
        ]

    def test_last_copy_deletion_removes_parent_item(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            self._write_media_file(settings, "video/Solo.mp4", "solo")
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Solo.mp4",
                size_bytes=4,
                created_at=T_NEW,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 204

        # No empty logical item survives its last physical copy.
        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0

    def test_shared_path_within_same_item_blocks_without_mutation(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            media_file = self._write_media_file(
                settings, "video/Shared.mp4", "shared"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            first_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Shared.mp4",
                size_bytes=6,
                created_at=T_NEW,
            )
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Shared.mp4",
                size_bytes=6,
                created_at="2026-10-02 12:05:00",
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{first_id}"
            )

            assert response.status_code == 409
            error = response.json()["error"]
            assert error["code"] == "media_file_shared_path"
            assert error["retryable"] is False
            assert error["message"]

        assert media_file.read_text() == "shared"
        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 2

    def test_shared_path_across_items_blocks_without_mutation(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """The physical file is shared catalog-wide, not per owner: a
        foreign-owner row referencing the same path also blocks."""
        with app_factory() as client:
            media_file = self._write_media_file(
                settings, "video/Shared.mp4", "shared"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Shared.mp4",
                size_bytes=6,
                created_at=T_NEW,
            )
            insert_library_item(
                settings,
                item_id="f" * 32,
                created_at=T_NEWER,
                owner_id="someone-else",
                title="Foreign",
            )
            insert_media_file(
                settings,
                library_item_id="f" * 32,
                path="video/Shared.mp4",
                size_bytes=6,
                created_at=T_NEWER,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "media_file_shared_path"

        assert media_file.read_text() == "shared"
        assert _row_count(settings, "library_items") == 2
        assert _row_count(settings, "media_files") == 2

    def test_shared_path_via_different_storage_mapping_blocks(
        self, settings: Settings, analyzer: FakeAnalyzer
    ) -> None:
        """A matching storage-resolved location blocks: rows that
        reach the same file through different configured roots (here a
        Plex root nested inside the App Library root) also conflict."""
        plex_root = settings.library_dir / "plex"
        plex_settings = settings.model_copy(
            update={"plex_music_dir": plex_root}
        )
        # The plex tree must exist before startup: delete-root identities
        # are captured at startup only.
        media_file = self._write_media_file(plex_settings, "plex/Song.mp3", "song")
        with TestClient(build_application(plex_settings, analyzer)) as client:
            insert_library_item(plex_settings, item_id="1" * 32, created_at=T_NEW)
            app_copy_id = insert_media_file(
                plex_settings,
                library_item_id="1" * 32,
                path="plex/Song.mp3",
                size_bytes=4,
                created_at=T_NEW,
            )
            insert_library_item(
                plex_settings, item_id="2" * 32, created_at=T_NEWER, title="Plex"
            )
            insert_media_file(
                plex_settings,
                library_item_id="2" * 32,
                path="Song.mp3",
                size_bytes=4,
                created_at=T_NEWER,
                storage_type="plex_music",
                display_path="/media/music/Song.mp3",
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{app_copy_id}"
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "media_file_shared_path"

        assert media_file.read_text() == "song"
        assert _row_count(plex_settings, "media_files") == 2

    @pytest.mark.parametrize(
        "alias_mode", ["controlled_case", "native_case", "startup_mapping",
                       "startup_mapping_controlled_case"]
    )
    def test_same_entry_alias_blocks_catalog_wide(
        self, settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, alias_mode: str,
    ) -> None:
        target = self._write_media_file(settings, "video/Foo.mp4", "shared")
        if alias_mode == "native_case" and not (target.parent / "foo.mp4").exists():
            pytest.skip("volume does not support this case-insensitive lookup")
        cross_mapping = alias_mode.startswith("startup_mapping")
        if cross_mapping:
            alias_root = tmp_path / "startup-plex-alias"
            alias_root.symlink_to(settings.library_dir, target_is_directory=True)
            settings = settings.model_copy(update={"plex_music_dir": alias_root})
        other_path = (
            "video/Foo.mp4" if alias_mode == "startup_mapping" else "video/foo.mp4"
        )
        with TestClient(build_application(settings, analyzer)) as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings, library_item_id="1" * 32, path="video/Foo.mp4",
                size_bytes=6, created_at=T_NEW,
            )
            insert_library_item(
                settings, item_id="f" * 32, owner_id="someone-else",
                created_at=T_NEWER,
            )
            other_id = insert_media_file(
                settings, library_item_id="f" * 32, path=other_path,
                storage_type="plex_music" if cross_mapping else "app_library",
                size_bytes=6, created_at=T_NEWER,
            )
            real_stat = os.stat
            alias_lookups: list[str] = []

            def case_alias_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
                if path == "foo.mp4" and kwargs.get("dir_fd") is not None:
                    assert kwargs["follow_symlinks"] is False
                    alias_lookups.append(path)
                    path = "Foo.mp4"
                return real_stat(path, *args, **kwargs)

            def forbidden_unlink(*args: Any, **kwargs: Any) -> None:
                raise AssertionError("shared physical target must not be unlinked")

            with monkeypatch.context() as patch:
                if "controlled_case" in alias_mode:
                    patch.setattr(os, "stat", case_alias_stat)
                patch.setattr(os, "unlink", forbidden_unlink)
                response = client.delete(
                    f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
                )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "media_file_shared_path"
            assert response.json()["error"]["retryable"] is False
            if "controlled_case" in alias_mode:
                assert alias_lookups == ["foo.mp4"]
            assert _column_values(settings, "SELECT id FROM media_files ORDER BY id") == [
                copy_id, other_id,
            ]
            assert _row_count(settings, "library_items") == 2
            assert target.read_text() == "shared"
            if cross_mapping:
                assert alias_root.is_symlink()
                assert (alias_root / "video/Foo.mp4").read_text() == "shared"

    @pytest.mark.parametrize("other_path", ["Foo.mp4", "missing/Foo.mp4"])
    def test_different_inode_or_missing_cross_mapping_reference_allows_delete(
        self, settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path,
        other_path: str,
    ) -> None:
        target = self._write_media_file(settings, "video/Foo.mp4", "target")
        plex_root = tmp_path / "separate-plex"
        plex_root.mkdir()
        unrelated = plex_root / "Foo.mp4"
        unrelated.write_text("unrelated")
        assert target.stat().st_ino != unrelated.stat().st_ino
        settings = settings.model_copy(update={"plex_music_dir": plex_root})
        with TestClient(build_application(settings, analyzer)) as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings, library_item_id="1" * 32, path="video/Foo.mp4",
                size_bytes=6, created_at=T_NEW,
            )
            insert_library_item(settings, item_id="2" * 32, created_at=T_NEWER)
            other_id = insert_media_file(
                settings, library_item_id="2" * 32, path=other_path,
                storage_type="plex_music", size_bytes=9, created_at=T_NEWER,
            )
            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )
            assert response.status_code == 204
        assert not target.exists()
        assert unrelated.read_text() == "unrelated"
        assert _column_values(settings, "SELECT id FROM media_files") == [other_id]
        assert _column_values(settings, "SELECT id FROM library_items") == ["2" * 32]

    def test_reference_resolution_programming_error_propagates_and_rolls_back(
        self, app_factory: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with app_factory() as client:
            target = self._write_media_file(settings, "video/Foo.mp4", "target")
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings, library_item_id="1" * 32, path="video/Foo.mp4",
                size_bytes=6, created_at=T_NEW,
            )
            insert_media_file(
                settings, library_item_id="1" * 32, path="video/Other.mp4",
                size_bytes=6, created_at=T_NEWER,
            )

            def broken_resolve(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("reference coding error")

            monkeypatch.setattr(StorageService, "resolve_media_file", broken_resolve)
            with pytest.raises(RuntimeError, match="reference coding error"):
                client.delete(f"/api/v1/library/{'1' * 32}/media-files/{copy_id}")
        assert target.read_text() == "target"
        assert _row_count(settings, "media_files") == 2
        assert _row_count(settings, "library_items") == 1

    def test_configured_plex_root_copy_deletes(
        self, settings: Settings, analyzer: FakeAnalyzer
    ) -> None:
        plex_root = settings.library_dir.parent / "plex-music"
        plex_root.mkdir(parents=True, exist_ok=True)
        media_file = plex_root / "Song.mp3"
        media_file.write_text("song")
        plex_settings = settings.model_copy(
            update={"plex_music_dir": plex_root}
        )
        with TestClient(build_application(plex_settings, analyzer)) as client:
            insert_library_item(
                plex_settings,
                item_id="1" * 32,
                created_at=T_NEW,
                media_type="audio",
                title="Song",
            )
            copy_id = insert_media_file(
                plex_settings,
                library_item_id="1" * 32,
                path="Song.mp3",
                size_bytes=4,
                created_at=T_NEW,
                storage_type="plex_music",
                display_path="/media/music/Song.mp3",
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 204

        assert not media_file.exists()
        assert _row_count(plex_settings, "library_items") == 0
        assert _row_count(plex_settings, "media_files") == 0

    def test_unconfigured_storage_target_fails_closed(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="Song.mp3",
                size_bytes=4,
                created_at=T_NEW,
                storage_type="plex_music",
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 409
            error = response.json()["error"]
            assert error["code"] == "media_file_invalid_target"
            assert error["retryable"] is False

        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1

    def test_unverifiable_other_reference_blocks(
        self, app_factory: Any, settings: Settings
    ) -> None:
        """Another row whose storage mapping cannot be resolved (Plex
        not configured here) is a potential reference: fail closed."""
        with app_factory() as client:
            media_file = self._write_media_file(
                settings, "video/A.mp4", "a"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/A.mp4",
                size_bytes=1,
                created_at=T_NEW,
            )
            insert_library_item(
                settings, item_id="2" * 32, created_at=T_NEWER, title="Plex"
            )
            insert_media_file(
                settings,
                library_item_id="2" * 32,
                path="Song.mp3",
                size_bytes=4,
                created_at=T_NEWER,
                storage_type="plex_music",
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 409
            error = response.json()["error"]
            assert error["code"] == "media_file_reference_unverifiable"
            assert error["retryable"] is False
            # The message is generic-accurate: unconfigured storage is
            # one example, not the only unverifiable cause.
            assert "cannot be verified" in error["message"]

        assert media_file.read_text() == "a"
        assert _row_count(settings, "library_items") == 2
        assert _row_count(settings, "media_files") == 2

    @pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
    @pytest.mark.parametrize("failure_site", ["reference_parent", "reference_final", "target"])
    def test_resolution_io_failure_preserves_files_and_all_database_rows(
        self, app_factory: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch,
        error_number: int, failure_site: str,
    ) -> None:
        """Real storage errno translation, not a mocked service outcome.

        Reference failures are stable conflicts; preparation failures
        remain storage errors. Both must roll back the staged row delete.
        """
        with app_factory() as client:
            target = self._write_media_file(settings, "video/A.mp4", "target")
            other = self._write_media_file(settings, "other/B.mp4", "other")
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings, library_item_id="1" * 32, path="video/A.mp4",
                size_bytes=6, created_at=T_NEW,
            )
            insert_library_item(
                settings, item_id="2" * 32, created_at=T_NEWER,
                owner_id="someone-else", title="Other",
            )
            insert_media_file(
                settings, library_item_id="2" * 32, path="other/B.mp4",
                size_bytes=5, created_at=T_NEWER,
            )
            with _connect(settings) as connection:
                before = list(connection.iterdump())
            real_open, real_stat = os.open, os.stat
            failures: list[int] = []
            unlink_calls: list[Any] = []

            def failing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
                blocked = {"reference_parent": "other", "target": "A.mp4"}
                if path == blocked.get(failure_site) and kwargs.get("dir_fd") is not None:
                    failures.append(error_number)
                    raise OSError(error_number, "controlled resolution failure")
                return real_open(path, flags, *args, **kwargs)

            def failing_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
                if (
                    failure_site == "reference_final" and path == "B.mp4"
                    and kwargs.get("dir_fd") is not None
                ):
                    failures.append(error_number)
                    raise OSError(error_number, "controlled resolution failure")
                return real_stat(path, *args, **kwargs)

            def forbidden_unlink(*args: Any, **kwargs: Any) -> None:
                unlink_calls.append((args, kwargs))
                raise AssertionError("unverifiable deletion must not unlink")

            with monkeypatch.context() as patch:
                patch.setattr(os, "open", failing_open)
                patch.setattr(os, "stat", failing_stat)
                patch.setattr(os, "unlink", forbidden_unlink)
                response = client.delete(
                    f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
                )

            error = response.json()["error"]
            if failure_site == "target":
                assert response.status_code == 500
                assert error["code"] == "storage_delete_failed"
                assert error["retryable"] is True
            else:
                assert response.status_code == 409
                assert error["code"] == "media_file_reference_unverifiable"
                assert error["retryable"] is False
                assert "cannot be verified" in error["message"]
            assert failures == [error_number]
            assert not unlink_calls
            assert target.read_text() == "target"
            assert other.read_text() == "other"
            with _connect(settings) as connection:
                assert list(connection.iterdump()) == before

    def test_traversal_path_target_blocked(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            outside = settings.library_dir.parent / "escape.mp4"
            outside.write_text("keep me")
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/../../escape.mp4",
                size_bytes=7,
                created_at=T_NEW,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "media_file_invalid_target"

        assert outside.read_text() == "keep me"
        assert _row_count(settings, "media_files") == 1

    def test_symlink_escape_target_blocked(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            outside = settings.library_dir.parent / "outside.mp4"
            outside.write_text("outside media")
            video_dir = settings.library_dir / "video"
            video_dir.mkdir(parents=True, exist_ok=True)
            link = video_dir / "Linked.mp4"
            link.symlink_to(outside)
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Linked.mp4",
                size_bytes=13,
                created_at=T_NEW,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "media_file_invalid_target"

        assert outside.read_text() == "outside media"
        # The symlink itself is never unlinked either.
        assert link.is_symlink()

    def test_storage_root_target_blocked(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path=".",
                size_bytes=1,
                created_at=T_NEW,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "media_file_invalid_target"

        assert settings.library_dir.is_dir()
        assert _row_count(settings, "media_files") == 1

    def test_directory_target_blocked(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            (settings.library_dir / "video").mkdir(parents=True, exist_ok=True)
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video",
                size_bytes=1,
                created_at=T_NEW,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "media_file_invalid_target"

        assert (settings.library_dir / "video").is_dir()
        assert _row_count(settings, "media_files") == 1

    def test_writerless_fifo_rolls_back_and_releases_write_lock(
        self, app_factory: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with app_factory() as client:
            target = settings.library_dir / "video" / "Pipe.mp4"
            os.mkfifo(target)
            before = target.stat()
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings, library_item_id="1" * 32, path="video/Pipe.mp4",
                size_bytes=0, created_at=T_NEW,
            )
            with _connect(settings) as connection:
                items_before = connection.execute("SELECT * FROM library_items").fetchall()
                files_before = connection.execute("SELECT * FROM media_files").fetchall()

            active: set[int] = set()
            final_opens: list[int] = []
            real_open, real_close = os.open, os.close

            def guarded_open(name: Any, flags: int, dir_fd: int | None = None) -> int:
                if name == target.name:
                    # Never issue a blocking FIFO open, even on broken code.
                    assert flags & os.O_NONBLOCK
                    assert flags & os.O_NOFOLLOW
                    assert flags & os.O_ACCMODE == os.O_RDONLY
                    # The real staged delete already owns the write lock.
                    with sqlite3.connect(
                        settings.state_dir / "downloader.db", timeout=0.05,
                    ) as writer:
                        with pytest.raises(sqlite3.OperationalError, match="locked"):
                            writer.execute("BEGIN IMMEDIATE")
                    final_opens.append(flags)
                fd = real_open(name, flags, dir_fd=dir_fd)
                active.add(fd)
                return fd

            def tracked_close(fd: int) -> None:
                real_close(fd)
                active.remove(fd)

            def forbidden_unlink(*args: Any, **kwargs: Any) -> None:
                raise AssertionError("FIFO rejection must never unlink")

            with monkeypatch.context() as patch:
                patch.setattr(os, "open", guarded_open)
                patch.setattr(os, "close", tracked_close)
                patch.setattr(os, "unlink", forbidden_unlink)
                started = time.monotonic()
                response = client.delete(
                    f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
                )
                assert time.monotonic() - started < 2
                assert response.status_code == 409
                error = response.json()["error"]
                assert error["code"] == "media_file_invalid_target"
                assert len(final_opens) == 1
                assert not active

            # Independent, bounded real writer succeeds only after rollback
            # releases the lock; a SELECT alone would not prove this.
            started = time.monotonic()
            with sqlite3.connect(
                settings.state_dir / "downloader.db", timeout=0.2,
            ) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE library_items SET title = title WHERE id = ?",
                               ("1" * 32,))
                writer.commit()
                assert writer.execute("SELECT * FROM library_items").fetchall() == items_before
                assert writer.execute("SELECT * FROM media_files").fetchall() == files_before
            assert time.monotonic() - started < 2
            after = target.stat()
            assert stat.S_ISFIFO(after.st_mode)
            assert after == before

    def test_missing_file_returns_conflict_and_keeps_rows(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Gone.mp4",
                size_bytes=4,
                created_at=T_NEW,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 409
            error = response.json()["error"]
            assert error["code"] == "media_file_missing"
            assert error["retryable"] is False

        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1

    def test_unknown_and_foreign_item_share_one_404(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(
                settings,
                item_id="f" * 32,
                created_at=T_NEW,
                owner_id="someone-else",
            )
            foreign_copy_id = insert_media_file(
                settings,
                library_item_id="f" * 32,
                path="video/Foreign.mp4",
                size_bytes=7,
                created_at=T_NEW,
            )

            foreign = client.delete(
                f"/api/v1/library/{'f' * 32}/media-files/{foreign_copy_id}"
            )
            unknown = client.delete(
                f"/api/v1/library/deadbeef/media-files/{foreign_copy_id}"
            )

        assert foreign.status_code == 404
        assert unknown.status_code == 404
        # Existence of a foreign item must not leak: identical bodies.
        assert foreign.json() == unknown.json()
        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1

    def test_unknown_media_file_returns_404(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/9999"
            )

        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "media_file_not_found"
        assert error["retryable"] is False
        assert error["message"]

    def test_copy_of_another_item_returns_404_without_mutation(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            self._write_media_file(settings, "video/A.mp4", "a")
            self._write_media_file(settings, "video/B.mp4", "b")
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            insert_library_item(
                settings, item_id="2" * 32, created_at=T_NEWER, title="Other"
            )
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/A.mp4",
                size_bytes=1,
                created_at=T_NEW,
            )
            second_copy_id = insert_media_file(
                settings,
                library_item_id="2" * 32,
                path="video/B.mp4",
                size_bytes=1,
                created_at=T_NEWER,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{second_copy_id}"
            )

            assert response.status_code == 404
            assert response.json()["error"]["code"] == "media_file_not_found"

        assert _row_count(settings, "library_items") == 2
        assert _row_count(settings, "media_files") == 2
        assert (settings.library_dir / "video" / "A.mp4").read_text() == "a"
        assert (settings.library_dir / "video" / "B.mp4").read_text() == "b"

    def test_non_integer_media_file_id_fails_generic_validation(
        self, app_factory: Any
    ) -> None:
        with app_factory() as client:
            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/not-an-int"
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    def test_storage_failure_rolls_back_catalog(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def failing_delete(self: PreparedMediaFile) -> Path:
            raise StorageDeleteFailedError("permission denied")

        monkeypatch.setattr(PreparedMediaFile, "delete", failing_delete)
        with app_factory() as client:
            media_file = self._write_media_file(
                settings, "video/Test Video.mp4", "real media bytes"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=17,
                created_at=T_NEW,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 500
            error = response.json()["error"]
            assert error["code"] == "storage_delete_failed"
            assert error["retryable"] is True

        assert media_file.read_text() == "real media bytes"
        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1

    def test_db_failure_before_file_delete_leaves_everything(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The staged catalog delete runs before the filesystem delete,
        so a database failure there must leave the file untouched."""
        with app_factory():
            media_file = self._write_media_file(
                settings, "video/Test Video.mp4", "real media bytes"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=17,
                created_at=T_NEW,
            )

        real_delete_row = library_repo.delete_media_file_row

        async def failing_delete_row(session: Any, media_file_id: int) -> bool:
            await real_delete_row(session, media_file_id)
            raise RuntimeError("catalog delete failed before file delete")

        monkeypatch.setattr(
            library_repo, "delete_media_file_row", failing_delete_row
        )

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()
        service = LibraryService(session_factory, storage)
        try:
            with pytest.raises(
                RuntimeError, match="catalog delete failed before file delete"
            ):
                asyncio.run(
                    service.delete_media_file(
                        Actor(), item_id="1" * 32, media_file_id=copy_id
                    )
                )
        finally:
            asyncio.run(engine.dispose())

        assert media_file.read_text() == "real media bytes"
        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1

    def test_catalog_rows_stage_before_file_delete(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ordering proof: the staged copy delete (which takes the
        SQLite write lock) precedes the target preparation, the
        reference scan runs inside that lock, and the physical deletion
        comes last."""
        with app_factory():
            self._write_media_file(settings, "video/Test Video.mp4", "media")
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=5,
                created_at=T_NEW,
            )

        calls: list[str] = []
        real_delete_row = library_repo.delete_media_file_row

        async def recording_delete_row(session: Any, media_file_id: int) -> bool:
            calls.append("catalog-stage")
            return await real_delete_row(session, media_file_id)

        monkeypatch.setattr(
            library_repo, "delete_media_file_row", recording_delete_row
        )
        real_prepare = StorageService.prepare_media_file

        def recording_prepare(
            self: StorageService, storage_type: str, path: str
        ) -> PreparedMediaFile:
            calls.append("prepare")
            return real_prepare(self, storage_type, path)

        monkeypatch.setattr(
            StorageService, "prepare_media_file", recording_prepare
        )
        real_scan = library_repo.list_media_files_excluding

        async def recording_scan(session: Any, exclude_id: int) -> list[MediaFile]:
            calls.append("ref-scan")
            return await real_scan(session, exclude_id)

        monkeypatch.setattr(
            library_repo, "list_media_files_excluding", recording_scan
        )
        real_fs_delete = PreparedMediaFile.delete

        def recording_fs_delete(self: PreparedMediaFile) -> Path:
            calls.append("file-delete")
            return real_fs_delete(self)

        monkeypatch.setattr(PreparedMediaFile, "delete", recording_fs_delete)

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()
        service = LibraryService(session_factory, storage)
        try:
            outcome = asyncio.run(
                service.delete_media_file(
                    Actor(), item_id="1" * 32, media_file_id=copy_id
                )
            )
        finally:
            asyncio.run(engine.dispose())

        assert outcome is DeleteMediaFileOutcome.DELETED
        assert calls == ["catalog-stage", "prepare", "ref-scan", "file-delete"]

    def test_write_lock_spans_the_file_delete(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Serialization: while the physical delete is in flight, the
        open transaction holds the SQLite write lock, so a concurrent
        catalog insert of another reference to the same path cannot
        commit inside the check→delete window."""
        with app_factory():
            self._write_media_file(settings, "video/Test Video.mp4", "media")
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=5,
                created_at=T_NEW,
            )
            insert_library_item(
                settings, item_id="2" * 32, created_at=T_NEWER, title="Other"
            )

        delete_started = threading.Event()
        allow_delete = threading.Event()
        real_fs_delete = PreparedMediaFile.delete

        def blocking_fs_delete(self: PreparedMediaFile) -> Path:
            delete_started.set()
            assert allow_delete.wait(timeout=5)
            return real_fs_delete(self)

        monkeypatch.setattr(PreparedMediaFile, "delete", blocking_fs_delete)

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()
        service = LibraryService(session_factory, storage)

        def try_concurrent_insert() -> None:
            connection = sqlite3.connect(
                settings.state_dir / "downloader.db", timeout=0.2
            )
            try:
                connection.execute(
                    "INSERT INTO media_files "
                    "(library_item_id, storage_type, path, display_path, "
                    "size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("2" * 32, "app_library", "video/Test Video.mp4", None, 5, T_NEW),
                )
                connection.commit()
            finally:
                connection.close()

        async def run() -> DeleteMediaFileOutcome:
            task = asyncio.create_task(
                service.delete_media_file(
                    Actor(), item_id="1" * 32, media_file_id=copy_id
                )
            )
            assert await asyncio.to_thread(delete_started.wait, 5)
            with pytest.raises(sqlite3.OperationalError):
                await asyncio.to_thread(try_concurrent_insert)
            allow_delete.set()
            return await task

        try:
            outcome = asyncio.run(run())
        finally:
            allow_delete.set()
            asyncio.run(engine.dispose())

        assert outcome is DeleteMediaFileOutcome.DELETED
        assert not (settings.library_dir / "video" / "Test Video.mp4").exists()
        # The concurrent same-path insert could not commit inside the
        # window (SQLITE_BUSY), so only the losing item remains.
        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 0

    def test_reference_committed_before_scan_blocks_delete(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Scan-inside-lock safety: a same-path reference committed
        after the validation reads but before the staged delete must
        either become visible to the in-transaction scan (blocked) or
        force a busy failure — never an unsafe success."""
        with app_factory():
            media_file = self._write_media_file(
                settings, "video/Test Video.mp4", "media"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=5,
                created_at=T_NEW,
            )
            insert_library_item(
                settings, item_id="2" * 32, created_at=T_NEWER, title="Other"
            )

        stage_reached = threading.Event()
        allow_stage = threading.Event()
        real_delete_row = library_repo.delete_media_file_row

        async def gating_delete_row(session: Any, media_file_id: int) -> bool:
            stage_reached.set()
            assert await asyncio.to_thread(allow_stage.wait, 5)
            return await real_delete_row(session, media_file_id)

        monkeypatch.setattr(
            library_repo, "delete_media_file_row", gating_delete_row
        )

        def insert_same_path_reference() -> None:
            with _connect(settings) as connection:
                connection.execute(
                    "INSERT INTO media_files "
                    "(library_item_id, storage_type, path, display_path, "
                    "size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("2" * 32, "app_library", "video/Test Video.mp4", None, 5, T_NEW),
                )
                connection.commit()

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()
        service = LibraryService(session_factory, storage)

        async def run() -> DeleteMediaFileOutcome:
            task = asyncio.create_task(
                service.delete_media_file(
                    Actor(), item_id="1" * 32, media_file_id=copy_id
                )
            )
            assert await asyncio.to_thread(stage_reached.wait, 5)
            await asyncio.to_thread(insert_same_path_reference)
            allow_stage.set()
            return await task

        try:
            outcome = asyncio.run(run())
        finally:
            allow_stage.set()
            asyncio.run(engine.dispose())

        # Either the scan saw the committed reference (blocked) or the
        # staging failed busy — both safe; DELETED would be unsafe.
        assert outcome in {
            DeleteMediaFileOutcome.SHARED_PATH,
            DeleteMediaFileOutcome.DATABASE_BUSY,
        }
        assert media_file.read_text() == "media"
        assert _row_count(settings, "library_items") == 2
        assert _row_count(settings, "media_files") == 2

    @pytest.mark.parametrize("failure_site", [
        "get_library_item", "get_media_file_for_item", "delete_media_file_row",
        "list_media_files_excluding", "delete_library_item",
    ])
    @pytest.mark.parametrize("native,sqlite_code,message,busy", [
        pytest.param(True, sqlite3.SQLITE_BUSY, "database is locked", True, id="busy"),
        pytest.param(True, sqlite3.SQLITE_LOCKED, "database table is locked", True,
                     id="locked"),
        pytest.param(True, sqlite3.SQLITE_BUSY_SNAPSHOT, "snapshot failure", True,
                     id="extended-busy"),
        pytest.param(True, sqlite3.SQLITE_LOCKED_SHAREDCACHE, "shared cache failure", True,
                     id="extended-locked"),
        pytest.param(True, sqlite3.SQLITE_READONLY, "attempt to write a readonly database",
                     False, id="readonly"),
        pytest.param(True, sqlite3.SQLITE_ERROR, "no such table: media_files", False,
                     id="no-table"),
        pytest.param(True, sqlite3.SQLITE_IOERR_READ, "disk I/O error", False, id="io"),
        pytest.param(True, sqlite3.SQLITE_READONLY, "database is locked", False,
                     id="nonbusy-code-overrides-lock-message"),
        pytest.param(True, sqlite3.SQLITE_BUSY, "disk I/O error", True,
                     id="busy-code-overrides-message"),
        pytest.param(True, None, "database is locked", True, id="native-no-code-busy"),
        pytest.param(True, None, "database table is locked", True,
                     id="native-no-code-table-locked"),
        pytest.param(True, None, "database schema is locked", True,
                     id="native-no-code-schema-locked"),
        pytest.param(True, None, "no such table: database is locked", False,
                     id="native-no-code-misleading"),
        pytest.param(True, None, "disk I/O error", False, id="native-no-code-io"),
        pytest.param(False, None, "database is locked", False, id="other-provider"),
        pytest.param(False, sqlite3.SQLITE_BUSY, "database is locked", False,
                     id="other-provider-with-code"),
    ])
    def test_operational_error_before_unlink_preserves_file_and_database(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        failure_site: str,
        native: bool,
        sqlite_code: int | None,
        message: str,
        busy: bool,
    ) -> None:
        """All pre-unlink phases share code-first SQLite classification.

        Run the real repository operation first, including staged deletes,
        to prove that rollback restores every row before returning the API error.
        """
        original = sqlite3.OperationalError(message) if native else Exception(message)
        if sqlite_code is not None:
            original.sqlite_errorcode = sqlite_code  # type: ignore[attr-defined]
        real_operation = getattr(library_repo, failure_site)
        calls: list[str] = []

        async def failing_operation(*args: Any, **kwargs: Any) -> Any:
            await real_operation(*args, **kwargs)
            calls.append(failure_site)
            raise OperationalError("controlled catalog operation", None, original)

        with app_factory() as client:
            media_file = self._write_media_file(
                settings, "video/Test Video.mp4", "media"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=5,
                created_at=T_NEW,
            )
            with _connect(settings) as connection:
                before = list(connection.iterdump())

            def forbidden_delete(self: PreparedMediaFile) -> Path:
                raise AssertionError("pre-unlink database failure must not delete a file")

            with monkeypatch.context() as patch:
                patch.setattr(library_repo, failure_site, failing_operation)
                patch.setattr(PreparedMediaFile, "delete", forbidden_delete)
                response = client.delete(
                    f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
                )

            assert response.status_code == (503 if busy else 500)
            error = response.json()["error"]
            assert error["code"] == ("database_busy" if busy else "library_database_failed")
            assert error["retryable"] is True
            assert "nothing was changed" in error["message"]
            assert calls == [failure_site]

        assert media_file.read_text() == "media"
        with _connect(settings) as connection:
            assert list(connection.iterdump()) == before

    def test_cancellation_during_file_delete_completes_consistently(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The unlink runs shielded: cancelling the caller while the
        worker is blocked must not abandon the transaction — the file
        result is observed and the commit/rollback decision follows it,
        so no late background deletion lands on rolled-back rows."""
        with app_factory():
            self._write_media_file(settings, "video/Test Video.mp4", "media")
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=5,
                created_at=T_NEW,
            )

        delete_started = threading.Event()
        allow_delete = threading.Event()
        real_fs_delete = PreparedMediaFile.delete

        def blocking_fs_delete(self: PreparedMediaFile) -> Path:
            delete_started.set()
            assert allow_delete.wait(timeout=5)
            return real_fs_delete(self)

        monkeypatch.setattr(PreparedMediaFile, "delete", blocking_fs_delete)

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()
        service = LibraryService(session_factory, storage)

        async def run() -> DeleteMediaFileOutcome:
            task = asyncio.create_task(
                service.delete_media_file(
                    Actor(), item_id="1" * 32, media_file_id=copy_id
                )
            )
            assert await asyncio.to_thread(delete_started.wait, 5)
            task.cancel()
            allow_delete.set()
            return await task

        try:
            outcome = asyncio.run(run())
        finally:
            allow_delete.set()
            asyncio.run(engine.dispose())

        assert outcome is DeleteMediaFileOutcome.DELETED
        assert not (settings.library_dir / "video" / "Test Video.mp4").exists()
        assert _row_count(settings, "library_items") == 0
        assert _row_count(settings, "media_files") == 0

    def test_target_swap_between_prepare_and_delete_returns_conflict(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A replaced final entry between preparation and deletion is
        detected at the closest-possible verification and fails closed."""
        real_prepare = StorageService.prepare_media_file

        def swapping_prepare(
            self: StorageService, storage_type: str, path: str
        ) -> PreparedMediaFile:
            prepared = real_prepare(self, storage_type, path)
            prepared.resolved_path.unlink()
            prepared.resolved_path.write_text("replacement")
            return prepared

        monkeypatch.setattr(
            StorageService, "prepare_media_file", swapping_prepare
        )
        with app_factory() as client:
            media_file = self._write_media_file(
                settings, "video/Test Video.mp4", "media"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=5,
                created_at=T_NEW,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 409
            error = response.json()["error"]
            assert error["code"] == "media_file_target_changed"
            assert error["retryable"] is True

        assert media_file.read_text() == "replacement"
        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1

    @pytest.mark.parametrize("sqlite_code", [None, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_IOERR])
    def test_commit_failure_after_file_delete_reports_catalog_inconsistency(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        sqlite_code: int | None,
    ) -> None:
        """The one unrecoverable window (§51): any database-level commit
        failure (generic SQLAlchemyError included) after a successful
        unlink returns a distinct catalog-consistency error whose body
        does not claim everything is unchanged."""
        with app_factory() as client:
            self._write_media_file(settings, "video/Test Video.mp4", "media")
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=5,
                created_at=T_NEW,
            )

            async def failing_commit(self: AsyncSession) -> None:
                if sqlite_code is not None:
                    original = sqlite3.OperationalError("commit failed")
                    original.sqlite_errorcode = sqlite_code
                    raise OperationalError("COMMIT", None, original)
                raise SQLAlchemyError("commit failed: disk error")

            # Installed after startup so lifespan commits still work.
            monkeypatch.setattr(AsyncSession, "commit", failing_commit)

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 500
            error = response.json()["error"]
            assert error["code"] == "media_file_catalog_commit_failed"
            assert error["retryable"] is False
            # The body reports the out-of-sync state, not "unchanged".
            assert "out of sync" in error["message"]

        # The file is gone (unrecoverable window) but the failure is
        # loud and the catalog still shows the copy rather than lying.
        assert not (settings.library_dir / "video" / "Test Video.mp4").exists()
        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1

    def test_generic_db_error_before_file_delete_returns_stable_500(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-operational database failures at pre-unlink persistence
        points map to a stable typed 500 with safe-retry semantics and
        mutate nothing (file and catalog unchanged)."""

        async def failing_delete_row(session: Any, media_file_id: int) -> bool:
            del session, media_file_id
            raise SQLAlchemyError("generic database failure")

        monkeypatch.setattr(
            library_repo, "delete_media_file_row", failing_delete_row
        )
        with app_factory() as client:
            media_file = self._write_media_file(
                settings, "video/Test Video.mp4", "media"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=5,
                created_at=T_NEW,
            )

            response = client.delete(
                f"/api/v1/library/{'1' * 32}/media-files/{copy_id}"
            )

            assert response.status_code == 500
            error = response.json()["error"]
            assert error["code"] == "library_database_failed"
            assert error["retryable"] is True

        assert media_file.read_text() == "media"
        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1

    def test_parent_row_invariant_violation_propagates(
        self,
        app_factory: Any,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A vanished parent row inside the locked transaction is an
        invariant violation: it propagates loudly (no outcome masking)
        and nothing is mutated."""

        async def vanishing_parent_delete(
            session: Any, item_id: str, owner_id: str
        ) -> bool:
            del session, item_id, owner_id
            return False

        monkeypatch.setattr(
            library_repo, "delete_library_item", vanishing_parent_delete
        )
        with app_factory():
            media_file = self._write_media_file(
                settings, "video/Test Video.mp4", "media"
            )
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            copy_id = insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=5,
                created_at=T_NEW,
            )

        engine, session_factory = create_engine_and_sessionmaker(
            f"sqlite+aiosqlite:///{settings.state_dir / 'downloader.db'}"
        )
        storage = StorageService(settings.staging_dir, settings.library_dir)
        storage.initialize_delete_roots()
        service = LibraryService(session_factory, storage)
        try:
            with pytest.raises(RuntimeError, match="disappeared inside"):
                asyncio.run(
                    service.delete_media_file(
                        Actor(), item_id="1" * 32, media_file_id=copy_id
                    )
                )
        finally:
            asyncio.run(engine.dispose())

        assert media_file.read_text() == "media"
        assert _row_count(settings, "library_items") == 1
        assert _row_count(settings, "media_files") == 1


class TestBoundedQueries:
    """Guard against N+1 media-file loads (list page + single IN query)."""

    @staticmethod
    def _record_catalog_statements(statements: list[str]) -> Any:
        def _before_execute(*args: Any) -> None:
            statement = args[2]
            if "FROM library_items" in statement or "FROM media_files" in statement:
                statements.append(statement)

        return _before_execute

    def test_list_page_uses_two_catalog_queries(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            for index in range(5):
                insert_library_item(
                    settings,
                    item_id=f"{index}" * 32,
                    created_at=f"2026-10-0{index + 1} 10:00:00",
                )
                insert_media_file(
                    settings,
                    library_item_id=f"{index}" * 32,
                    path=f"video/item{index}.mp4",
                    size_bytes=10 + index,
                    created_at=f"2026-10-0{index + 1} 10:00:00",
                )

            statements: list[str] = []
            hook = self._record_catalog_statements(statements)
            engine = client.app.state.engine.sync_engine
            event.listen(engine, "before_cursor_execute", hook)
            try:
                response = client.get("/api/v1/library")
            finally:
                event.remove(engine, "before_cursor_execute", hook)

        assert response.status_code == 200
        assert len(response.json()["items"]) == 5
        assert len(statements) == 2, statements

    def test_detail_uses_two_catalog_queries(
        self, app_factory: Any, settings: Settings
    ) -> None:
        with app_factory() as client:
            insert_library_item(settings, item_id="1" * 32, created_at=T_NEW)
            insert_media_file(
                settings,
                library_item_id="1" * 32,
                path="video/Test Video.mp4",
                size_bytes=11,
                created_at=T_NEW,
            )

            statements: list[str] = []
            hook = self._record_catalog_statements(statements)
            engine = client.app.state.engine.sync_engine
            event.listen(engine, "before_cursor_execute", hook)
            try:
                response = client.get(f"/api/v1/library/{'1' * 32}")
            finally:
                event.remove(engine, "before_cursor_execute", hook)

        assert response.status_code == 200
        assert len(statements) == 2, statements


def _orm_media_file(**overrides: Any) -> MediaFile:
    values: dict[str, Any] = {
        "id": 1,
        "library_item_id": "1" * 32,
        "storage_type": "app_library",
        "path": "video/Test Video.mp4",
        "size_bytes": 11,
        "created_at": datetime(2026, 10, 2, 10, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return MediaFile(**values)


def _orm_library_item(**overrides: Any) -> LibraryItem:
    values: dict[str, Any] = {
        "id": "1" * 32,
        "owner_id": "local",
        "job_id": JOB_ID,
        "source_url": YOUTUBE_URL,
        "media_type": "video",
        "title": "Test Video",
        "created_at": datetime(2026, 10, 2, 10, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return LibraryItem(**values)


class TestDtoBoundaryValidation:
    """Closed vocabularies are enforced where catalog rows become DTOs."""

    def test_invalid_storage_type_rejected_at_mapping_boundary(self) -> None:
        # ValueError covers the explicit enum conversion in the mapping;
        # pydantic's ValidationError is a ValueError subclass.
        with pytest.raises(ValueError, match="cloud_storage"):
            media_file_to_dto(_orm_media_file(storage_type="cloud_storage"))

    def test_invalid_media_type_rejected_for_list_and_detail(self) -> None:
        item = _orm_library_item(media_type="ebook")
        with pytest.raises(ValueError, match="ebook"):
            library_item_list_dto(item, [])
        with pytest.raises(ValueError, match="ebook"):
            library_item_detail_dto(item, [])

    def test_valid_enum_values_accepted_and_serialize_as_strings(self) -> None:
        media = media_file_to_dto(_orm_media_file(storage_type="plex_music"))
        assert media.storage_type is MediaStorageType.PLEX_MUSIC
        # Runtime JSON stays a plain string value.
        assert media.model_dump(mode="json")["storage_type"] == "plex_music"

        list_item = library_item_list_dto(
            _orm_library_item(media_type="audio"), []
        )
        assert list_item.model_dump(mode="json")["media_type"] == "audio"


class TestOpenApiContracts:
    def test_library_routes_publish_typed_schemas(
        self, app_factory: Any
    ) -> None:
        with app_factory() as client:
            spec = client.get("/openapi.json").json()

        list_response = spec["paths"]["/api/v1/library"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]
        assert list_response == {"$ref": "#/components/schemas/LibraryListResponse"}

        detail_response = spec["paths"]["/api/v1/library/{item_id}"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        assert detail_response == {"$ref": "#/components/schemas/LibraryItemDto"}

        schemas = spec["components"]["schemas"]

        media_properties = schemas["LibraryMediaFileDto"]["properties"]
        assert set(media_properties) == {
            "id",
            "storage_type",
            "path",
            "display_path",
            "size_bytes",
            "created_at",
        }
        # Closed vocabulary published as a referenced enum schema.
        assert media_properties["storage_type"] == {
            "$ref": "#/components/schemas/MediaStorageType"
        }
        assert schemas["MediaStorageType"]["enum"] == [
            "app_library",
            "plex_music",
            "plex_video",
        ]

        list_properties = schemas["LibraryListItemDto"]["properties"]
        assert set(list_properties) == {
            "id",
            "job_id",
            "title",
            "media_type",
            "format",
            "created_at",
            "media_files",
        }
        assert list_properties["media_type"] == {
            "$ref": "#/components/schemas/LibraryMediaType"
        }
        assert schemas["LibraryMediaType"]["enum"] == ["audio", "video"]
        assert (
            list_properties["media_files"]["items"]["$ref"]
            == "#/components/schemas/LibraryMediaFileDto"
        )

        detail_properties = schemas["LibraryItemDto"]["properties"]
        assert set(detail_properties) == {
            "id",
            "job_id",
            "title",
            "media_type",
            "source_url",
            "source_id",
            "platform",
            "artist",
            "album",
            "created_at",
            "format",
            "codec",
            "bitrate",
            "resolution",
            "duration",
            "media_files",
        }
        assert detail_properties["media_type"] == {
            "$ref": "#/components/schemas/LibraryMediaType"
        }
        assert (
            detail_properties["media_files"]["items"]["$ref"]
            == "#/components/schemas/LibraryMediaFileDto"
        )

        page_properties = schemas["LibraryListResponse"]["properties"]
        assert set(page_properties) == {"items", "limit", "offset", "has_more"}
        assert (
            page_properties["items"]["items"]["$ref"]
            == "#/components/schemas/LibraryListItemDto"
        )

    def test_delete_library_route_documents_no_content_success(
        self, app_factory: Any
    ) -> None:
        with app_factory() as client:
            spec = client.get("/openapi.json").json()

        delete_responses = spec["paths"]["/api/v1/library/{item_id}"]["delete"][
            "responses"
        ]
        # The narrow success contract for a removal: 204, no response body.
        # (422 is FastAPI's default validation response for path params.)
        assert "200" not in delete_responses
        assert "content" not in delete_responses["204"]

    def test_delete_media_file_route_documents_no_content_success(
        self, app_factory: Any
    ) -> None:
        with app_factory() as client:
            spec = client.get("/openapi.json").json()

        path = "/api/v1/library/{item_id}/media-files/{media_file_id}"
        delete_operation = spec["paths"][path]["delete"]
        delete_responses = delete_operation["responses"]
        # Narrow success contract: 204, no response body.
        assert "200" not in delete_responses
        assert "content" not in delete_responses["204"]
        # The copy target is a typed integer id under the owner-scoped
        # library item — no path-shaped parameter anywhere.
        parameters = delete_operation["parameters"]
        media_file_param = next(
            parameter
            for parameter in parameters
            if parameter["name"] == "media_file_id"
        )
        assert media_file_param["in"] == "path"
        assert media_file_param["schema"]["type"] == "integer"

    def test_delete_media_file_route_publishes_typed_error_schema(
        self, app_factory: Any
    ) -> None:
        with app_factory() as client:
            spec = client.get("/openapi.json").json()

        path = "/api/v1/library/{item_id}/media-files/{media_file_id}"
        delete_responses = spec["paths"][path]["delete"]["responses"]
        # Every documented failure status carries the same typed
        # envelope schema (ARCHITECTURE.md §43).
        for status in ("404", "409", "500", "503"):
            schema = delete_responses[status]["content"]["application/json"][
                "schema"
            ]
            assert schema == {"$ref": "#/components/schemas/LibraryErrorResponse"}

        schemas = spec["components"]["schemas"]
        envelope = schemas["LibraryErrorEnvelope"]
        assert set(envelope["properties"]) == {
            "code",
            "message",
            "retryable",
            "item_id",
            "media_file_id",
        }
        assert set(envelope["required"]) == {"code", "message", "retryable"}
        assert (
            schemas["LibraryErrorResponse"]["properties"]["error"]
            == {"$ref": "#/components/schemas/LibraryErrorEnvelope"}
        )
        assert set(schemas["LibraryErrorResponse"]["required"]) == {"error"}
