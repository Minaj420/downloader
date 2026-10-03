import asyncio
import errno
import os
import shutil
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.services.storage_service import (
    MediaFileMissingError,
    MediaFileNotRegularError,
    MediaFileTargetChangedError,
    ResolvedMediaFile,
    StorageDeleteFailedError,
    StoragePathError,
    StorageService,
    StorageTargetError,
)
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


class TestDeleteRootTrust:
    """Startup root-identity capture and per-operation re-validation
    (ARCHITECTURE.md §38, §49): the constructor trusts nothing, the
    filesystem root is never a delete root, and replaced/symlinked
    ancestors fail closed."""

    @staticmethod
    def _storage(tmp_path: Path, **kwargs: Path | None) -> StorageService:
        return StorageService(
            tmp_path / "staging",
            tmp_path / "library",
            plex_music_root=kwargs.get("plex_music_root"),
            plex_video_root=kwargs.get("plex_video_root"),
        )

    def test_constructor_does_not_trust_roots_before_initialization(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)
        storage.ensure_layout()
        target = tmp_path / "library" / "video" / "Clip.mp4"
        target.write_text("media")

        # Before initialize_delete_roots every delete-root use fails
        # closed, even though the file exists.
        with pytest.raises(StorageTargetError):
            storage.prepare_media_file("app_library", "video/Clip.mp4")
        assert storage.try_resolve_media_files(
            [("app_library", "video/Clip.mp4")]
        ) == [None]

        storage.initialize_delete_roots()
        prepared = storage.prepare_media_file("app_library", "video/Clip.mp4")
        try:
            assert prepared.resolved_path == target.resolve()
        finally:
            prepared.close()

    def test_initialize_requires_reachable_app_root(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path)  # no ensure_layout: root missing
        with pytest.raises(StorageTargetError):
            storage.initialize_delete_roots()

    def test_initialize_rejects_filesystem_root_as_app_root(
        self, tmp_path: Path
    ) -> None:
        storage = StorageService(tmp_path / "staging", Path("/"))
        with pytest.raises(StoragePathError):
            storage.initialize_delete_roots()

    def test_filesystem_root_plex_stays_unavailable(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path, plex_music_root=Path("/"))
        storage.ensure_layout()
        # Optional root: startup stays up, deletions fail closed.
        storage.initialize_delete_roots()
        with pytest.raises(StorageTargetError):
            storage.prepare_media_file("plex_music", "video/Clip.mp4")

    def test_missing_plex_root_stays_unavailable_after_it_appears(
        self, tmp_path: Path
    ) -> None:
        """Identities are captured at startup only: a Plex root that
        appears later is still untrusted (fail-closed)."""
        plex_root = tmp_path / "plex-music"
        storage = self._storage(tmp_path, plex_music_root=plex_root)
        storage.ensure_layout()
        storage.initialize_delete_roots()

        plex_root.mkdir()
        (plex_root / "Song.mp3").write_text("song")

        with pytest.raises(StorageTargetError):
            storage.prepare_media_file("plex_music", "Song.mp3")
        assert storage.try_resolve_media_files([("plex_music", "Song.mp3")]) == [None]

    def test_replaced_root_directory_fails_closed(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path)
        storage.ensure_layout()
        storage.initialize_delete_roots()
        target = tmp_path / "library" / "video" / "Clip.mp4"
        target.write_text("media")

        # Replace the root directory itself with a fresh one: the
        # per-operation identity check must refuse.
        shutil.rmtree(tmp_path / "library")
        (tmp_path / "library" / "video").mkdir(parents=True, exist_ok=True)
        (tmp_path / "library" / "video" / "Clip.mp4").write_text("impostor")

        with pytest.raises(StoragePathError):
            storage.prepare_media_file("app_library", "video/Clip.mp4")
        assert (tmp_path / "library" / "video" / "Clip.mp4").read_text() == "impostor"

    def test_ancestor_swap_to_root_copy_fails_closed(self, tmp_path: Path) -> None:
        library_root = tmp_path / "a" / "b" / "library"
        storage = StorageService(tmp_path / "staging", library_root)
        storage.ensure_layout()
        storage.initialize_delete_roots()
        (library_root / "video" / "Clip.mp4").write_text("media")

        # Swap the ancestor so the root path now resolves to a COPY of
        # the tree (different root inode) behind a symlink: the
        # per-operation identity check must refuse, the copy survives.
        copy_root = tmp_path / "a" / "elsewhere"
        shutil.copytree(tmp_path / "a" / "b", copy_root)
        shutil.rmtree(tmp_path / "a" / "b")
        (tmp_path / "a" / "b").symlink_to(copy_root, target_is_directory=True)

        with pytest.raises(StoragePathError):
            storage.prepare_media_file("app_library", "video/Clip.mp4")
        assert (copy_root / "library" / "video" / "Clip.mp4").read_text() == "media"

    @pytest.mark.parametrize("storage_type", ["app_library", "plex_music", "plex_video"])
    @pytest.mark.parametrize("swapped_component", ["ancestor", "root"])
    def test_same_inode_moved_root_symlink_fails_closed(
        self, tmp_path: Path, storage_type: str, swapped_component: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ancestor = tmp_path / "configured"
        root = ancestor / "library"
        root.mkdir(parents=True)
        storage = StorageService(
            tmp_path / "staging",
            root if storage_type == "app_library" else tmp_path / "app",
            plex_music_root=root if storage_type == "plex_music" else None,
            plex_video_root=root if storage_type == "plex_video" else None,
        )
        storage.ensure_layout()
        (root / "video").mkdir(exist_ok=True)
        target = root / "video" / "Clip.mp4"
        target.write_text("media")
        identity = (target.stat().st_dev, target.stat().st_ino)
        storage.initialize_delete_roots()
        root_identity = (root.stat().st_dev, root.stat().st_ino)
        swapped = ancestor if swapped_component == "ancestor" else root
        moved = tmp_path / "moved"
        swapped.rename(moved)
        swapped.symlink_to(moved, target_is_directory=True)
        # This is the ORIGINAL root inode, not a copied tree. The old
        # resolve()+inode policy therefore incorrectly accepted it.
        assert (root.stat().st_dev, root.stat().st_ino) == root_identity
        assert target.resolve().read_text() == "media"

        active: set[int] = set()
        real_open, real_close = os.open, os.close

        def tracked_open(path: Any, flags: int, dir_fd: int | None = None) -> int:
            assert flags & os.O_NOFOLLOW
            fd = real_open(path, flags, dir_fd=dir_fd)
            active.add(fd)
            return fd

        def tracked_close(fd: int) -> None:
            real_close(fd)
            active.remove(fd)

        monkeypatch.setattr(os, "open", tracked_open)
        monkeypatch.setattr(os, "close", tracked_close)
        with pytest.raises(StoragePathError):
            storage.prepare_media_file(storage_type, "video/Clip.mp4")
        assert not active
        with pytest.raises(StoragePathError):
            storage.resolve_media_file(storage_type, "video/Clip.mp4")
        assert not active
        with pytest.raises(StoragePathError):
            storage.verify_media_file(storage_type, "video/Clip.mp4", identity)
        assert not active
        assert storage.try_resolve_media_files(
            [(storage_type, "video/Clip.mp4")]
        ) == [None]
        assert not active
        assert target.read_text() == "media"

    @pytest.mark.parametrize("alias_target", ["absolute", "relative", "nested"])
    def test_startup_alias_preserved_and_later_substitution_rejected(
        self, tmp_path: Path, alias_target: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        physical = tmp_path / "physical"
        physical.mkdir()
        alias = tmp_path / "alias"
        if alias_target == "nested":
            intermediate = tmp_path / "intermediate"
            intermediate.symlink_to("physical", target_is_directory=True)
            alias.symlink_to("intermediate", target_is_directory=True)
        else:
            alias.symlink_to(
                physical if alias_target == "absolute" else Path("physical"),
                target_is_directory=True,
            )
        storage = StorageService(
            tmp_path / "staging", alias / "library",
            plex_music_root=physical / "library",
        )
        storage.ensure_layout()
        target = physical / "library" / "video" / "Clip.mp4"
        target.write_text("media")
        identity = (target.stat().st_dev, target.stat().st_ino)
        expected = target.resolve()
        storage.initialize_delete_roots()

        def forbidden_resolve(*args: Any, **kwargs: Any) -> Path:
            raise AssertionError("operation must not re-resolve startup paths")

        monkeypatch.setattr(Path, "resolve", forbidden_resolve)
        for storage_type in ("app_library", "plex_music"):
            assert storage.resolve_media_file(
                storage_type, "video/Clip.mp4"
            ) == ResolvedMediaFile(expected, identity)
            assert storage.verify_media_file(
                storage_type, "video/Clip.mp4", identity
            ) == expected
            prepared = storage.prepare_media_file(storage_type, "video/Clip.mp4")
            try:
                assert prepared.resolved_path == expected
            finally:
                prepared.close()

        # Even a replacement alias reaching the SAME root is not trusted.
        replaced = intermediate if alias_target == "nested" else alias
        replaced.rename(tmp_path / "saved-alias")
        replaced.symlink_to(physical, target_is_directory=True)
        with pytest.raises(StoragePathError):
            storage.prepare_media_file("app_library", "video/Clip.mp4")
        assert storage.try_resolve_media_files(
            [("app_library", "video/Clip.mp4"), ("plex_music", "video/Clip.mp4")]
        ) == [None, ResolvedMediaFile(expected, identity)]
        assert target.read_text() == "media"

    def test_canonical_ancestor_substitution_behind_startup_alias_fails_closed(
        self, tmp_path: Path,
    ) -> None:
        physical = tmp_path / "physical"
        physical.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(physical, target_is_directory=True)
        storage = StorageService(tmp_path / "staging", alias / "library")
        storage.ensure_layout()
        storage.initialize_delete_roots()
        target = physical / "library" / "video" / "Clip.mp4"
        target.write_text("media")
        identity = (target.stat().st_dev, target.stat().st_ino)
        physical.rename(tmp_path / "moved")
        physical.symlink_to(tmp_path / "moved", target_is_directory=True)
        with pytest.raises(StoragePathError):
            storage.prepare_media_file("app_library", "video/Clip.mp4")
        with pytest.raises(StoragePathError):
            storage.resolve_media_file("app_library", "video/Clip.mp4")
        with pytest.raises(StoragePathError):
            storage.verify_media_file("app_library", "video/Clip.mp4", identity)
        assert target.read_text() == "media"

    def test_relative_startup_root_is_anchored_to_startup_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        storage = StorageService(Path("staging"), Path("library"))
        storage.ensure_layout()
        storage.initialize_delete_roots()
        target = tmp_path / "library" / "video" / "Clip.mp4"
        target.write_text("media")
        expected = target.resolve()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert storage.resolve_media_file(
            "app_library", "video/Clip.mp4"
        ) == ResolvedMediaFile(expected, (target.stat().st_dev, target.stat().st_ino))

    def test_try_resolve_propagates_programming_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = self._storage(tmp_path)
        storage.ensure_layout()
        storage.initialize_delete_roots()

        def broken_resolve(
            _self: StorageService, storage_type: str, path: str
        ) -> ResolvedMediaFile:
            raise RuntimeError("coding error")

        monkeypatch.setattr(StorageService, "resolve_media_file", broken_resolve)
        with pytest.raises(RuntimeError, match="coding error"):
            storage.try_resolve_media_files([("app_library", "video/x.mp4")])


class TestMediaFileResolution:
    """Catalogued copy resolution through validated roots (§37, §49)."""

    @staticmethod
    def _storage(tmp_path: Path, **kwargs: Path | None) -> StorageService:
        storage = StorageService(
            tmp_path / "staging",
            tmp_path / "library",
            plex_music_root=kwargs.get("plex_music_root"),
            plex_video_root=kwargs.get("plex_video_root"),
        )
        storage.ensure_layout()
        storage.initialize_delete_roots()
        return storage

    def test_storage_root_for_maps_configured_types(self, tmp_path: Path) -> None:
        plex_music = tmp_path / "plex-music"
        plex_video = tmp_path / "plex-video"
        storage = self._storage(
            tmp_path,
            plex_music_root=plex_music,
            plex_video_root=plex_video,
        )

        assert storage.storage_root_for("app_library") == tmp_path / "library"
        assert storage.storage_root_for("plex_music") == plex_music
        assert storage.storage_root_for("plex_video") == plex_video

    def test_different_spelling_preserves_existing_entry_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = self._storage(tmp_path)
        target = storage.library_root / "video" / "Foo.mp4"
        target.write_text("media")
        real_stat = os.stat
        lookups: list[str] = []

        def case_alias_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            # Emulate only this final-name lookup, not DTO equality or
            # root validation: portable even on case-sensitive volumes.
            if path == "foo.mp4" and kwargs.get("dir_fd") is not None:
                assert kwargs["follow_symlinks"] is False
                lookups.append(path)
                path = "Foo.mp4"
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", case_alias_stat)
        reference = storage.resolve_media_file("app_library", "video/foo.mp4")
        prepared = storage.prepare_media_file("app_library", "video/Foo.mp4")
        try:
            assert reference.resolved_path != prepared.resolved_path
            assert reference.identity == prepared.identity
            assert lookups == ["foo.mp4"]
        finally:
            prepared.close()

    @pytest.mark.parametrize(
        "kwargs, storage_type",
        [
            ({}, "plex_music"),
            ({}, "plex_video"),
            ({"plex_music_root": None}, "plex_music"),
            ({}, "cloud_storage"),
            ({}, ""),
        ],
    )
    def test_storage_root_for_fails_closed_when_unconfigured(
        self, tmp_path: Path, kwargs: dict[str, Path | None], storage_type: str
    ) -> None:
        storage = self._storage(tmp_path, **kwargs)

        with pytest.raises(StorageTargetError):
            storage.storage_root_for(storage_type)

    def test_resolve_media_file_returns_contained_resolved_path(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)
        target = tmp_path / "library" / "video" / "Clip.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")

        resolved = storage.resolve_media_file("app_library", "video/Clip.mp4")

        assert resolved.resolved_path == target.resolve()
        assert resolved.identity == (target.stat().st_dev, target.stat().st_ino)

    def test_resolve_media_file_accepts_plex_root(
        self, tmp_path: Path
    ) -> None:
        plex_root = tmp_path / "plex-music"
        plex_root.mkdir()
        (plex_root / "Song.mp3").write_text("s")
        storage = self._storage(tmp_path, plex_music_root=plex_root)

        resolved = storage.resolve_media_file("plex_music", "Song.mp3")

        target = plex_root / "Song.mp3"
        assert resolved.resolved_path == target.resolve()
        assert resolved.identity == (target.stat().st_dev, target.stat().st_ino)

    @pytest.mark.parametrize(
        "path",
        [
            "video/../../escape.mp4",
            "../outside.mp4",
            ".",
            "",
            "/etc/passwd",
        ],
    )
    def test_resolve_media_file_rejects_escaping_and_root_paths(
        self, tmp_path: Path, path: str
    ) -> None:
        storage = self._storage(tmp_path)

        with pytest.raises((StoragePathError, StorageTargetError)):
            storage.resolve_media_file("app_library", path)

    def test_resolve_media_file_rejects_symlink_escape(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)
        library_root = tmp_path / "library"
        video_dir = library_root / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside.mp4"
        outside.write_text("secret")
        (video_dir / "Linked.mp4").symlink_to(outside)

        with pytest.raises(StoragePathError):
            storage.resolve_media_file("app_library", "video/Linked.mp4")

        assert outside.read_text() == "secret"

    def test_try_resolve_media_files_reports_unresolvable_as_none(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)
        target = tmp_path / "library" / "video" / "Clip.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")

        resolved = storage.try_resolve_media_files(
            [
                ("app_library", "video/Clip.mp4"),
                ("plex_music", "Song.mp3"),  # unconfigured mapping
                ("app_library", "../escape.mp4"),  # containment escape
                ("app_library", "/etc/passwd"),  # absolute catalog path
            ]
        )

        assert resolved[0] == ResolvedMediaFile(
            target.resolve(), (target.stat().st_dev, target.stat().st_ino)
        )
        assert resolved[1:] == [None, None, None]


    @pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
    @pytest.mark.parametrize("failure_site", ["root", "parent", "final"])
    def test_resolution_io_failures_are_unverifiable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        error_number: int, failure_site: str,
    ) -> None:
        storage = self._storage(tmp_path)
        target = tmp_path / "library" / "video" / "Clip.mp4"
        target.write_text("media")
        real_open, real_stat, real_close = os.open, os.stat, os.close
        active: set[int] = set()
        failures: list[int] = []

        def failing_open(path: Any, flags: int, dir_fd: int | None = None) -> int:
            if path == {"root": "library", "parent": "video"}.get(failure_site):
                failures.append(error_number)
                raise OSError(error_number, "controlled resolution failure")
            fd = real_open(path, flags, dir_fd=dir_fd)
            active.add(fd)
            return fd

        def failing_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            if failure_site == "final" and path == "Clip.mp4":
                failures.append(error_number)
                raise OSError(error_number, "controlled resolution failure")
            return real_stat(path, *args, **kwargs)

        def tracked_close(fd: int) -> None:
            real_close(fd)
            active.remove(fd)

        monkeypatch.setattr(os, "open", failing_open)
        monkeypatch.setattr(os, "stat", failing_stat)
        monkeypatch.setattr(os, "close", tracked_close)
        with pytest.raises(StorageDeleteFailedError) as raised:
            storage.resolve_media_file("app_library", "video/Clip.mp4")
        assert isinstance(raised.value.__cause__, OSError)
        assert raised.value.__cause__.errno == error_number
        assert not active
        assert storage.try_resolve_media_files(
            [("app_library", "video/Clip.mp4")]
        ) == [None]
        assert failures == [error_number, error_number]
        assert not active
        assert target.read_text() == "media"

    @pytest.mark.parametrize("path", ["missing/Clip.mp4", "video/Missing.mp4"])
    def test_missing_reference_keeps_lexical_identity(
        self, tmp_path: Path, path: str,
    ) -> None:
        storage = self._storage(tmp_path)
        expected = storage.library_root.resolve() / path
        assert storage.try_resolve_media_files([("app_library", path)]) == [
            ResolvedMediaFile(expected, None)
        ]


class TestMediaFileDeletion:
    """Descriptor-anchored physical file deletion (§21, §38, §49).

    ``prepare_media_file`` walks fail-closed from an identity-checked
    root (no symlink components, no final symlinks, no traversal) and
    the unlink runs under the held parent descriptor with a
    closest-possible inode re-verification.
    """

    @staticmethod
    def _storage(tmp_path: Path) -> StorageService:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        storage.initialize_delete_roots()
        return storage

    def test_prepare_and_delete_resolved_regular_file(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)
        target = tmp_path / "library" / "video" / "Clip.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("media")

        prepared = storage.prepare_media_file("app_library", "video/Clip.mp4")
        try:
            # The identity used for shared-reference scans.
            assert prepared.resolved_path == target.resolve()
            deleted_path = prepared.delete()
        finally:
            prepared.close()

        assert deleted_path == target.resolve()
        assert not target.exists()

    def test_prepare_closes_descriptors_on_success_lifecycle(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)
        target = tmp_path / "library" / "video" / "Clip.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("media")

        prepared = storage.prepare_media_file("app_library", "video/Clip.mp4")
        parent_fd, pinned_fd = prepared._parent_fd, prepared._pinned_fd
        prepared.close()
        prepared.close()  # idempotent

        with pytest.raises(OSError):
            os.fstat(parent_fd)
        with pytest.raises(OSError):
            os.fstat(pinned_fd)
        # A closed handle refuses to delete.
        with pytest.raises(StorageDeleteFailedError):
            prepared.delete()
        assert target.read_text() == "media"

    def test_missing_file_fails_at_prepare(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)
        (tmp_path / "library").mkdir(parents=True, exist_ok=True)

        with pytest.raises(MediaFileMissingError):
            storage.prepare_media_file("app_library", "video/Gone.mp4")

    def test_directory_target_fails_at_prepare(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path)
        (tmp_path / "library" / "video").mkdir(parents=True, exist_ok=True)

        with pytest.raises(MediaFileNotRegularError):
            storage.prepare_media_file("app_library", "video")

    @pytest.mark.parametrize("path", ["Pipe.mp4", "video/Pipe.mp4"])
    def test_writerless_fifo_rejected_without_blocking_or_leaking_fds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str,
    ) -> None:
        storage = self._storage(tmp_path)
        target = storage.library_root / path
        os.mkfifo(target)
        before = target.stat()
        active: set[int] = set()
        final_opens: list[int] = []
        real_open, real_dup, real_close = os.open, os.dup, os.close

        def guarded_open(name: Any, flags: int, dir_fd: int | None = None) -> int:
            if name == target.name:
                # Guard BEFORE the real syscall: a regression fails instead
                # of leaving a thread blocked forever on a writerless FIFO.
                assert flags & os.O_NONBLOCK
                assert flags & os.O_NOFOLLOW
                assert flags & os.O_ACCMODE == os.O_RDONLY
                final_opens.append(flags)
            fd = real_open(name, flags, dir_fd=dir_fd)
            active.add(fd)
            return fd

        def tracked_dup(fd: int) -> int:
            duplicate = real_dup(fd)
            active.add(duplicate)
            return duplicate

        def tracked_close(fd: int) -> None:
            real_close(fd)
            active.remove(fd)

        def forbidden_unlink(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("FIFO rejection must never unlink")

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", guarded_open)
            patch.setattr(os, "dup", tracked_dup)
            patch.setattr(os, "close", tracked_close)
            patch.setattr(os, "unlink", forbidden_unlink)
            started = time.monotonic()
            with pytest.raises(MediaFileNotRegularError):
                storage.prepare_media_file("app_library", path)
            assert time.monotonic() - started < 2
            assert len(final_opens) == 1
            assert not active

        after = target.stat()
        assert stat.S_ISFIFO(after.st_mode)
        assert after == before

    @pytest.mark.parametrize("path", [".", "", "/etc/passwd", "a/../../x"])
    def test_root_absolute_and_traversal_paths_fail_closed(
        self, tmp_path: Path, path: str
    ) -> None:
        storage = self._storage(tmp_path)
        (tmp_path / "library").mkdir(parents=True, exist_ok=True)

        with pytest.raises((StoragePathError, StorageTargetError)):
            storage.prepare_media_file("app_library", path)

        assert (tmp_path / "library").is_dir()

    def test_unconfigured_storage_fails_without_filesystem_access(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)

        with pytest.raises(StorageTargetError):
            storage.prepare_media_file("plex_music", "Song.mp3")
        with pytest.raises(StoragePathError):
            storage.prepare_media_file("app_library", "../escape.mp4")

    def test_final_symlink_rejected_not_followed(self, tmp_path: Path) -> None:
        """Fail-closed symlink policy: a catalogued symlink is refused
        (never resolved-and-deleted), whatever it points at."""
        storage = self._storage(tmp_path)
        library_root = tmp_path / "library"
        video_dir = library_root / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        real_file = library_root / "video" / "Real.mp4"
        real_file.write_text("media")
        link = video_dir / "Linked.mp4"
        link.symlink_to(real_file)

        with pytest.raises(StoragePathError):
            storage.prepare_media_file("app_library", "video/Linked.mp4")

        # Nothing was touched: the real file and the link survive.
        assert real_file.read_text() == "media"
        assert link.is_symlink()

    def test_symlink_component_rejected(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path)
        library_root = tmp_path / "library"
        library_root.mkdir(exist_ok=True)
        real_dir = library_root / "video"
        real_dir.mkdir(exist_ok=True)
        (real_dir / "Clip.mp4").write_text("media")
        link_dir = library_root / "linkdir"
        link_dir.symlink_to(real_dir, target_is_directory=True)

        with pytest.raises(StoragePathError):
            storage.prepare_media_file("app_library", "linkdir/Clip.mp4")

        assert (real_dir / "Clip.mp4").read_text() == "media"

    def test_parent_swap_cannot_redirect_deletion_outside_root(
        self, tmp_path: Path
    ) -> None:
        """Controlled parent swap: after preparation, replacing the
        parent *name* with a symlink to an outside directory cannot
        redirect the unlink — it still targets the pinned inode under
        the held parent descriptor, and the outside file survives."""
        storage = self._storage(tmp_path)
        library_root = tmp_path / "library"
        video_dir = library_root / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        target = video_dir / "Clip.mp4"
        target.write_text("media")

        prepared = storage.prepare_media_file("app_library", "video/Clip.mp4")
        expected_path = prepared.resolved_path
        try:
            outside_dir = tmp_path / "outside"
            outside_dir.mkdir()
            outside_file = outside_dir / "Clip.mp4"
            outside_file.write_text("outside media")
            # Swap the parent name to a symlink pointing outside.
            moved_dir = library_root / "video_moved"
            os.rename(video_dir, moved_dir)
            video_dir.symlink_to(outside_dir, target_is_directory=True)

            deleted_path = prepared.delete()
        finally:
            prepared.close()

        # The pinned (original) file is what got unlinked — the delete
        # returned the prepare-time identity, the entry under the held
        # (renamed) parent descriptor is gone — and the outside file
        # behind the swapped symlink is untouched.
        assert deleted_path == expected_path
        assert not (moved_dir / "Clip.mp4").exists()
        assert outside_file.read_text() == "outside media"
        assert video_dir.is_symlink()

    def test_final_entry_swap_fails_closed(self, tmp_path: Path) -> None:
        """Controlled final-target swap: a replaced entry (different
        inode at the same name) is detected at the closest-possible
        verification and nothing is deleted."""
        storage = self._storage(tmp_path)
        video_dir = tmp_path / "library" / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        target = video_dir / "Clip.mp4"
        target.write_text("original")

        prepared = storage.prepare_media_file("app_library", "video/Clip.mp4")
        try:
            target.unlink()
            target.write_text("replacement")

            with pytest.raises(MediaFileTargetChangedError):
                prepared.delete()
        finally:
            prepared.close()

        assert target.read_text() == "replacement"

    def test_final_entry_swap_to_symlink_fails_closed(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)
        video_dir = tmp_path / "library" / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        target = video_dir / "Clip.mp4"
        target.write_text("original")
        outside = tmp_path / "outside.mp4"
        outside.write_text("outside media")

        prepared = storage.prepare_media_file("app_library", "video/Clip.mp4")
        try:
            target.unlink()
            target.symlink_to(outside)

            with pytest.raises(MediaFileNotRegularError):
                prepared.delete()
        finally:
            prepared.close()

        assert outside.read_text() == "outside media"
        assert target.is_symlink()

    def test_unlink_failure_raises_typed_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = self._storage(tmp_path)
        target = tmp_path / "library" / "video" / "Clip.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("media")

        def failing_unlink(
            name: str, dir_fd: int | None = None
        ) -> None:
            raise PermissionError("read-only filesystem")

        monkeypatch.setattr(os, "unlink", failing_unlink)
        prepared = storage.prepare_media_file("app_library", "video/Clip.mp4")
        try:
            with pytest.raises(StorageDeleteFailedError):
                prepared.delete()
        finally:
            prepared.close()

        assert target.read_text() == "media"

    def test_prepare_closes_descriptors_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure mid-walk must not leak the descriptors opened so
        far (exception-safe lifecycle)."""
        storage = self._storage(tmp_path)
        video_dir = tmp_path / "library" / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        (video_dir / "Clip.mp4").write_text("media")
        opened: list[int] = []
        real_open = os.open

        def leaking_open(path: Any, flags: int, dir_fd: int | None = None) -> int:
            if opened:  # fail when walking beyond the already-open root fd
                raise PermissionError("no traversal for you")
            fd = real_open(path, flags, dir_fd=dir_fd)
            opened.append(fd)
            return fd

        monkeypatch.setattr(os, "open", leaking_open)

        with pytest.raises(StorageDeleteFailedError):
            storage.prepare_media_file("app_library", "video/Clip.mp4")

        # The root descriptor opened before the failure was closed.
        for fd in opened:
            with pytest.raises(OSError):
                os.fstat(fd)


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
