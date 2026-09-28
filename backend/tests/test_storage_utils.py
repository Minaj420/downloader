import asyncio
import errno
import os
import shutil
import threading
import uuid
from pathlib import Path

import pytest

from app.services.storage_service import StorageService
from app.utils.filenames import sanitize_filename, sanitize_media_filename
from app.utils.paths import ensure_within_root


class TestSanitizeFilename:
    def test_preserves_normal_names(self) -> None:
        assert sanitize_filename("Led Zeppelin IV") == "Led Zeppelin IV"
        assert sanitize_filename("Café Del Mar") == "Café Del Mar"

    def test_replaces_path_separators_and_traversal(self) -> None:
        assert "/" not in sanitize_filename("../../../etc/passwd")
        assert sanitize_filename("..") == "download"
        assert sanitize_filename(".") == "download"
        assert sanitize_filename("") == "download"
        assert sanitize_filename("   ") == "download"

    def test_strips_control_characters_and_nul(self) -> None:
        cleaned = sanitize_filename("naïve\u0000name\n\t")
        assert "\u0000" not in cleaned
        assert "\n" not in cleaned
        assert "\t" not in cleaned
        assert "naïve" in cleaned

    def test_strips_windows_reserved_characters(self) -> None:
        cleaned = sanitize_filename('a/b\\c:d*e?f"g<h>i|j')
        for character in '\\/:*?"<>|':
            assert character not in cleaned

    def test_trims_trailing_dots_and_spaces(self) -> None:
        assert sanitize_filename("name. ") == "name"
        assert sanitize_filename("name...") == "name"

    def test_caps_length(self) -> None:
        cleaned = sanitize_filename("x" * 500, max_length=100)
        assert len(cleaned) == 100


class TestSanitizeMediaFilename:
    def test_long_ascii_title_keeps_extension_within_byte_budget(self) -> None:
        cleaned = sanitize_media_filename("x" * 200 + ".mp4")

        assert cleaned.endswith(".mp4")
        assert len(cleaned.encode("utf-8")) <= 150
        assert cleaned == "x" * 146 + ".mp4"

    def test_exact_budget_name_is_preserved(self) -> None:
        cleaned = sanitize_media_filename("y" * 146 + ".mp4")

        assert cleaned == "y" * 146 + ".mp4"
        assert len(cleaned.encode("utf-8")) == 150

    def test_long_cjk_title_fits_byte_budget_without_splitting_characters(self) -> None:
        cleaned = sanitize_media_filename("視" * 100 + ".webm")

        assert cleaned.endswith(".webm")
        encoded = cleaned.encode("utf-8")
        assert len(encoded) <= 150
        assert encoded.decode("utf-8") == cleaned
        body = cleaned[: -len(".webm")]
        assert set(body) == {"視"}
        assert len(body.encode("utf-8")) <= 150 - len(".webm")

    def test_long_emoji_title_fits_byte_budget(self) -> None:
        cleaned = sanitize_media_filename("🎵" * 80 + ".mp3")

        assert cleaned.endswith(".mp3")
        assert len(cleaned.encode("utf-8")) <= 150
        assert set(cleaned[: -len(".mp3")]) == {"🎵"}

    def test_multi_dot_name_keeps_only_last_suffix_as_extension(self) -> None:
        assert sanitize_media_filename("video.final.mp4") == "video.final.mp4"
        assert (
            sanitize_media_filename("My.Video.Final.Cut.mp4")
            == "My.Video.Final.Cut.mp4"
        )

    def test_long_suffix_is_kept_as_extension_within_byte_budget(self) -> None:
        cleaned = sanitize_media_filename("x" * 200 + ".longextxx")

        assert cleaned.endswith(".longextxx")
        assert cleaned == "x" * 140 + ".longextxx"
        assert len(cleaned.encode("utf-8")) <= 150

    def test_multi_dot_name_treats_long_final_part_as_extension(self) -> None:
        cleaned = sanitize_media_filename(".a.b.clongext")

        assert cleaned.endswith(".clongext")
        assert cleaned == ".a.b.clongext"

    def test_suffix_with_forbidden_characters_is_treated_as_body(self) -> None:
        assert sanitize_media_filename("name.???") == "name.___"

    def test_suffix_that_cleans_away_is_treated_as_body(self) -> None:
        assert sanitize_media_filename("name.\t") == "name"
        assert sanitize_media_filename("name.  ") == "name"

    def test_extension_longer_than_budget_is_truncated_deterministically(self) -> None:
        cleaned = sanitize_media_filename("body." + "e" * 200)

        assert cleaned == "b." + "e" * 148
        assert len(cleaned.encode("utf-8")) <= 150

    def test_falls_back_when_body_budget_is_exhausted(self) -> None:
        assert sanitize_media_filename(".mp4") == "download.mp4"
        assert sanitize_media_filename("...") == "download"
        assert sanitize_media_filename("") == "download"
        assert sanitize_media_filename("   .webm") == "download.webm"

    def test_sanitizes_body_and_extension_independently(self) -> None:
        assert sanitize_media_filename("bad/name?.mp4") == "bad_name_.mp4"


class TestEnsureWithinRoot:
    def test_accepts_paths_inside_root(self, tmp_path: Path) -> None:
        target = tmp_path / "video" / "clip.mp4"
        target.parent.mkdir()
        target.write_text("x")

        resolved = ensure_within_root(tmp_path, target)

        assert resolved.is_relative_to(tmp_path.resolve())

    def test_rejects_escaping_paths(self, tmp_path: Path) -> None:
        outside = tmp_path / ".." / "escape.mp4"

        with pytest.raises(ValueError):
            ensure_within_root(tmp_path, outside)

    def test_rejects_root_itself(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            ensure_within_root(tmp_path, tmp_path)

    def test_rejects_symlink_resolving_to_root(self, tmp_path: Path) -> None:
        root = tmp_path / "staging"
        root.mkdir()
        link = root / "link-to-root"
        link.symlink_to(root, target_is_directory=True)

        with pytest.raises(ValueError):
            ensure_within_root(root, link)


class TestRemoveStaging:
    def test_rejects_job_id_escaping_staging_root(self, tmp_path: Path) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")

        with pytest.raises(ValueError):
            storage.remove_staging("../outside")

        assert outside.exists()
        assert (outside / "keep.txt").read_text() == "keep"

    def test_removes_existing_job_staging_dir(self, tmp_path: Path) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        job_dir = storage.job_staging_dir("job-1")
        media = job_dir / "media.mp4"
        media.write_text("media")

        storage.remove_staging("job-1")

        assert not media.exists()
        assert not job_dir.exists()

    def test_removes_uuid_hex_job_staging_dir(self, tmp_path: Path) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        job_id = uuid.uuid4().hex
        job_dir = storage.job_staging_dir(job_id)
        media = job_dir / "media.mp4"
        media.write_text("media")

        storage.remove_staging(job_id)

        assert not media.exists()
        assert not job_dir.exists()

    def test_rejects_invalid_job_ids_without_touching_staging_root(
        self, tmp_path: Path
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        staging_root = tmp_path / "staging"
        marker_file = staging_root / "marker.txt"
        marker_dir = staging_root / "marker-dir"
        marker_file.write_text("keep")
        marker_dir.mkdir()

        invalid_job_ids = [
            "",
            ".",
            "..",
            "x/..",
            "../..",
            "a/b",
            "a\\b",
            str(tmp_path),
            "/etc",
        ]

        for job_id in invalid_job_ids:
            with pytest.raises(ValueError):
                storage.remove_staging(job_id)
            with pytest.raises(ValueError):
                storage.job_staging_dir(job_id)

        assert marker_file.read_text() == "keep"
        assert marker_dir.exists()


class TestFinalTarget:
    def test_remains_strictly_inside_library_root(self, tmp_path: Path) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()

        target = storage.final_target("video", "Clip.mp4")

        assert target == (tmp_path / "library" / "video" / "Clip.mp4").resolve()
        assert target != storage.library_root.resolve()

    def test_long_title_keeps_extension_within_byte_budget(
        self, tmp_path: Path
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()

        target = storage.final_target("video", "x" * 200 + ".mp4")

        assert target.name == "x" * 146 + ".mp4"
        assert len(target.name.encode("utf-8")) <= 150

    def test_long_title_with_long_extension_keeps_extension(
        self, tmp_path: Path
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()

        target = storage.final_target("video", "x" * 200 + ".longextxx")

        assert target.name == "x" * 140 + ".longextxx"
        assert target.name.endswith(".longextxx")
        assert len(target.name.encode("utf-8")) <= 150


class TestStorageFinalization:
    def test_concurrent_placements_never_overwrite_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        sources = []
        for index in range(4):
            source = storage.job_staging_dir(str(index)) / "media.mp4"
            source.write_text(f"media-{index}")
            sources.append(source)
        target = tmp_path / "library" / "video" / "Same Title.mp4"
        self._synchronize_target_checks(monkeypatch, target, len(sources))

        async def place_all():
            return await asyncio.gather(
                *(
                    storage.place_final(source, media_kind="video", title="Same Title")
                    for source in sources
                )
            )

        results = asyncio.run(place_all())
        assert sum(not result.skipped_existing for result in results) == 1
        assert sum(result.skipped_existing for result in results) == 3
        assert target.read_text() in {f"media-{index}" for index in range(4)}

    def test_concurrent_cross_filesystem_copies_never_overwrite_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        sources = []
        for index in range(4):
            source = storage.job_staging_dir(str(index)) / "media.mp4"
            source.write_text(f"media-{index}")
            sources.append(source)
        source_paths = {source.resolve() for source in sources}
        real_link = os.link
        target = tmp_path / "library" / "video" / "Same Title.mp4"
        self._synchronize_target_checks(monkeypatch, target, len(sources))

        def cross_device_sources(src: Path, dst: Path) -> None:
            if Path(src).resolve() in source_paths:
                raise OSError(errno.EXDEV, "cross-device link")
            real_link(src, dst)

        monkeypatch.setattr(os, "link", cross_device_sources)

        async def place_all():
            return await asyncio.gather(
                *(
                    storage.place_final(source, media_kind="video", title="Same Title")
                    for source in sources
                )
            )

        results = asyncio.run(place_all())
        assert sum(not result.skipped_existing for result in results) == 1
        assert sum(result.skipped_existing for result in results) == 3
        assert target.read_text() in {f"media-{index}" for index in range(4)}
        assert not list(target.parent.glob("*.part"))

    def test_cross_filesystem_copy_is_hidden_and_does_not_block_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        source = storage.job_staging_dir("job") / "media.mp4"
        source.write_text("media")
        target = tmp_path / "library" / "video" / "Title.mp4"
        real_link = os.link
        real_copy2 = shutil.copy2
        copy_started = threading.Event()
        allow_copy = threading.Event()

        def cross_device_link(src: Path, dst: Path) -> None:
            if Path(src) == source:
                raise OSError(errno.EXDEV, "cross-device link")
            real_link(src, dst)

        def blocked_copy(src: Path, dst: Path) -> str:
            copy_started.set()
            assert allow_copy.wait(timeout=2)
            return str(real_copy2(src, dst))

        monkeypatch.setattr(os, "link", cross_device_link)
        monkeypatch.setattr(shutil, "copy2", blocked_copy)

        async def run() -> None:
            placement_task = asyncio.create_task(
                storage.place_final(source, media_kind="video", title="Title")
            )
            assert await asyncio.to_thread(copy_started.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
            assert not target.exists()
            allow_copy.set()
            result = await placement_task
            assert not result.skipped_existing

        asyncio.run(run())

        assert target.read_text() == "media"
        assert not source.exists()
        assert not list(target.parent.glob("*.part"))

    def test_cross_filesystem_copy_failure_leaves_no_visible_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        source = storage.job_staging_dir("job") / "media.mp4"
        source.write_text("media")
        target = tmp_path / "library" / "video" / "Title.mp4"

        def cross_device_link(_src: Path, _dst: Path) -> None:
            raise OSError(errno.EXDEV, "cross-device link")

        def failed_copy(_src: Path, dst: Path) -> None:
            Path(dst).write_text("partial")
            raise OSError("copy failed")

        monkeypatch.setattr(os, "link", cross_device_link)
        monkeypatch.setattr(shutil, "copy2", failed_copy)

        with pytest.raises(OSError, match="copy failed"):
            asyncio.run(storage.place_final(source, media_kind="video", title="Title"))

        assert source.read_text() == "media"
        assert not target.exists()
        assert not list(target.parent.glob("*.part"))

    def test_started_finalization_finishes_when_waiter_is_cancelled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        source = storage.job_staging_dir("job") / "media.mp4"
        source.write_text("media")
        target = tmp_path / "library" / "video" / "Title.mp4"
        real_link = os.link
        real_copy2 = shutil.copy2
        copy_started = threading.Event()
        allow_copy = threading.Event()

        def cross_device_link(src: Path, dst: Path) -> None:
            if Path(src) == source:
                raise OSError(errno.EXDEV, "cross-device link")
            real_link(src, dst)

        def blocked_copy(src: Path, dst: Path) -> str:
            copy_started.set()
            assert allow_copy.wait(timeout=2)
            return str(real_copy2(src, dst))

        monkeypatch.setattr(os, "link", cross_device_link)
        monkeypatch.setattr(shutil, "copy2", blocked_copy)

        async def run() -> None:
            placement_task = asyncio.create_task(
                storage.place_final(source, media_kind="video", title="Title")
            )
            assert await asyncio.to_thread(copy_started.wait, 2)
            placement_task.cancel()
            allow_copy.set()
            result = await asyncio.wait_for(placement_task, timeout=2)
            assert not result.skipped_existing

        asyncio.run(run())

        assert target.read_text() == "media"
        assert not source.exists()

    def test_long_title_with_long_extension_finalizes_with_extension(
        self, tmp_path: Path
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        source = storage.job_staging_dir("job") / "media.longextxx"
        source.write_text("media")

        result = asyncio.run(
            storage.place_final(source, media_kind="video", title="x" * 200)
        )

        assert result.final_path.name == "x" * 140 + ".longextxx"
        assert result.final_path.read_text() == "media"

    @staticmethod
    def _synchronize_target_checks(
        monkeypatch: pytest.MonkeyPatch, target: Path, participants: int
    ) -> None:
        barrier = threading.Barrier(participants)
        real_exists = Path.exists

        def synchronized_exists(path: Path) -> bool:
            exists = real_exists(path)
            if path == target:
                barrier.wait(timeout=2)
            return exists

        monkeypatch.setattr(Path, "exists", synchronized_exists)
