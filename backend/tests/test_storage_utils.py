import asyncio
import errno
import os
import shutil
import stat
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.services.storage_service import (
    MediaFileMissingError,
    MediaFileNotRegularError,
    MediaFileTargetChangedError,
    PlacementCancelled,
    ResolvedMediaFile,
    StorageDeleteFailedError,
    StoragePathError,
    StorageService,
    StorageTargetError,
)
from app.utils.filenames import (
    MAX_CONFLICT_COUNTER,
    conflict_media_filenames,
    sanitize_filename,
    sanitize_media_filename,
    sanitize_path_segment,
)
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


class TestSanitizePathSegment:
    def test_preserves_useful_unicode_with_nfc(self) -> None:
        decomposed = "Café Björk"
        assert sanitize_path_segment(decomposed) == "Café Björk"
        assert sanitize_path_segment("坂本龍一 🎹") == "坂本龍一 🎹"

    def test_keeps_inner_dots_without_extension_treatment(self) -> None:
        assert sanitize_path_segment("Vol. 1.5 (Live)") == "Vol. 1.5 (Live)"
        assert sanitize_path_segment("a..b") == "a..b"
        assert sanitize_path_segment("Album.mp3") == "Album.mp3"
        # A long dotted name is cut as one segment, never "body.ext".
        assert sanitize_path_segment("x" * 200 + ".flac") == "x" * 150

    @pytest.mark.parametrize("separator", ["\x00", "\x07", "​"])
    def test_renormalizes_after_dropping_characters(self, separator: str) -> None:
        name = f"e{separator}́"
        assert sanitize_path_segment(name) == "é"
        assert sanitize_path_segment(name, max_bytes=2) == "é"
        assert unicodedata.is_normalized("NFC", sanitize_path_segment(name))
        assert sanitize_path_segment("é") == "é"  # idempotent

    def test_drops_control_nul_format_and_lone_surrogates(self) -> None:
        cleaned = sanitize_path_segment("a\x00b\nc\td​e\ud800f\x7fg")
        assert cleaned == "abcdefg"
        cleaned.encode("utf-8")  # no lone surrogate survives

    def test_neutralizes_separators_and_forbidden_characters(self) -> None:
        assert sanitize_path_segment("AC/DC") == "AC_DC"
        assert sanitize_path_segment("a\\b") == "a_b"
        assert sanitize_path_segment('a:b*c?d"e<f>g|h') == "a_b_c_d_e_f_g_h"
        assert sanitize_path_segment("../../etc") == ".._.._etc"
        assert sanitize_path_segment("/etc/passwd") == "_etc_passwd"

    @pytest.mark.parametrize(
        "name", ["", "   ", ".", "..", "...", " . . ", "\x00", "​"]
    )
    def test_empty_and_dot_only_use_fallback(self, name: str) -> None:
        assert sanitize_path_segment(name) == "download"

    def test_trims_trailing_dots_and_spaces(self) -> None:
        assert sanitize_path_segment("  Album... ") == "Album"
        assert sanitize_path_segment(".hidden") == ".hidden"

    def test_ascii_byte_bound(self) -> None:
        assert sanitize_path_segment("a" * 150) == "a" * 150
        assert sanitize_path_segment("a" * 151) == "a" * 150

    def test_cjk_byte_bound_on_character_boundary(self) -> None:
        cleaned = sanitize_path_segment("視" * 60)
        assert cleaned == "視" * 50
        assert len(cleaned.encode("utf-8")) == 150

    def test_emoji_byte_bound_on_character_boundary(self) -> None:
        cleaned = sanitize_path_segment("🎵" * 50)
        assert cleaned == "🎵" * 37
        assert len(cleaned.encode("utf-8")) <= 150

    def test_truncation_retrims_trailing_dots_and_spaces(self) -> None:
        assert sanitize_path_segment("a" * 148 + " .b") == "a" * 148

    def test_is_deterministic(self) -> None:
        name = "Artist/Name: 視🎵..."
        assert sanitize_path_segment(name) == sanitize_path_segment(name)

    @pytest.mark.parametrize(
        "name, max_bytes, expected",
        [
            ("", 1, "d"),
            ("..", 3, "dow"),
            ("視", 2, "do"),
            ("ab", 1, "a"),
            ("x" * 10, 8, "x" * 8),
        ],
    )
    def test_small_positive_budget_never_exceeded(
        self, name: str, max_bytes: int, expected: str
    ) -> None:
        cleaned = sanitize_path_segment(name, max_bytes=max_bytes)
        assert cleaned == expected
        assert 0 < len(cleaned.encode("utf-8")) <= max_bytes

    @pytest.mark.parametrize("max_bytes", [0, -1])
    def test_non_positive_budget_rejected(self, max_bytes: int) -> None:
        with pytest.raises(ValueError):
            sanitize_path_segment("Album", max_bytes=max_bytes)

    @pytest.mark.parametrize("max_bytes", [True, False, 1.5, "10", None])
    def test_non_int_budget_rejected(self, max_bytes: Any) -> None:
        with pytest.raises(TypeError):
            sanitize_path_segment("Album", max_bytes=max_bytes)

    def test_existing_sanitizers_unchanged(self) -> None:
        assert sanitize_filename("..") == "download"
        assert sanitize_filename("x" * 200) == "x" * 150
        assert sanitize_media_filename("Album.mp3") == "Album.mp3"
        assert sanitize_media_filename("x" * 200 + ".flac") == "x" * 145 + ".flac"


class TestConflictMediaFilenames:
    def test_ordered_candidates_from_one_to_999(self) -> None:
        names = conflict_media_filenames("Title", "mp4", qualifier=" (720p)")
        assert MAX_CONFLICT_COUNTER == 999
        assert len(names) == 999
        assert names[:3] == ["Title (720p).mp4", "Title (720p) (2).mp4", "Title (720p) (3).mp4"]
        assert names[-1] == "Title (720p) (999).mp4"
        assert len(set(names)) == 999

    def test_audio_and_unmeasured_names_have_no_qualifier(self) -> None:
        assert conflict_media_filenames("Title", "m4a")[:2] == ["Title.m4a", "Title (2).m4a"]

    def test_prefix_keeps_collection_order(self) -> None:
        names = conflict_media_filenames("Title", "mp4", prefix="07 - ", qualifier=" (1080p)")
        assert names[:2] == ["07 - Title (1080p).mp4", "07 - Title (1080p) (2).mp4"]

    @pytest.mark.parametrize("prefix,qualifier,extension", [
        ("", "", "mp4"), ("", " (2160p)", "webm"), ("12345 - ", " (720p)", "mp4"),
        ("", "", "longextxx"),
    ])
    def test_long_unicode_body_is_cut_once_within_150_bytes(
        self, prefix: str, qualifier: str, extension: str
    ) -> None:
        title = "Şarkı ğüé 🎵 日本語 " * 20
        names = conflict_media_filenames(title, extension, prefix=prefix, qualifier=qualifier)
        head = names[0].removesuffix(f"{qualifier}.{extension}")
        assert head.startswith(prefix)
        budget = 150 - len(f"{prefix}{qualifier} (999).{extension}".encode())
        # The widest counter is reserved up front: the body never shrinks later.
        assert len(head.removeprefix(prefix).encode()) <= budget
        assert len(head.removeprefix(prefix).encode()) > budget - 8
        for counter, name in enumerate(names, start=1):
            suffix = "" if counter == 1 else f" ({counter})"
            assert name == f"{head}{qualifier}{suffix}.{extension}"
            assert len(name.encode()) <= 150
            name.encode().decode("utf-8")  # valid UTF-8, no split character

    @pytest.mark.parametrize("title", ["", "   ", "\x00\x01", ".", "..", "...", " . "])
    def test_unusable_title_falls_back(self, title: str) -> None:
        assert conflict_media_filenames(title, "mp4", prefix="07 - ")[:2] == [
            "07 - download.mp4", "07 - download (2).mp4",
        ]

    @pytest.mark.parametrize("title", ["../../etc/passwd", "a/b\\c", "/abs", "x\x00y"])
    def test_path_shaped_titles_stay_single_segment(self, title: str) -> None:
        for name in conflict_media_filenames(title, "mp4", max_counter=3):
            assert "/" not in name and "\\" not in name and "\x00" not in name
            assert name not in {".", ".."}

    @pytest.mark.parametrize("max_counter", [0, -1, True, 2.0])
    def test_invalid_max_counter_rejected(self, max_counter: Any) -> None:
        with pytest.raises(ValueError):
            conflict_media_filenames("Title", "mp4", max_counter=max_counter)

    def test_fixed_parts_without_room_for_title_rejected(self) -> None:
        with pytest.raises(ValueError):
            conflict_media_filenames("Title", "mp4", prefix="x" * 150)


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


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int, str | None]]:
    """Every entry below ``root`` with its mode, size and link text."""
    snapshot: dict[str, tuple[int, int, str | None]] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        for name in [*names, *files]:
            entry = Path(directory) / name
            info = entry.lstat()
            link = os.readlink(entry) if entry.is_symlink() else None
            snapshot[str(entry.relative_to(root))] = (
                info.st_mode, info.st_size, link
            )
    return snapshot


class TestFinalTargetFolders:
    """Nested App Library planning (M6.2): one sanitized segment per
    folder, containment at resolve time, no filesystem mutation."""

    @staticmethod
    def _storage(tmp_path: Path) -> StorageService:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        return storage

    @staticmethod
    def _forbid_mkdir(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        calls: list[Any] = []

        def spy(*args: Any, **kwargs: Any) -> None:
            calls.append(args)
            raise AssertionError("final_target must not create directories")

        monkeypatch.setattr(Path, "mkdir", spy)
        monkeypatch.setattr(os, "mkdir", spy)
        monkeypatch.setattr(os, "makedirs", spy)
        return calls

    @pytest.mark.parametrize("media_kind", ["audio", "video"])
    @pytest.mark.parametrize(
        "filename", ["Clip.mp4", "x" * 200 + ".mp4", "x" * 200 + ".longextxx"]
    )
    def test_empty_folders_match_existing_flat_target(
        self, tmp_path: Path, media_kind: str, filename: str
    ) -> None:
        storage = self._storage(tmp_path)

        target = storage.final_target(media_kind, filename, folders=())

        assert target == storage.final_target(media_kind, filename)
        expected = tmp_path / "library" / media_kind / sanitize_media_filename(
            filename
        )
        assert target == expected.resolve()

    def test_nested_segments_are_sanitized(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path)

        target = storage.final_target(
            "audio", "01 - Song?.m4a", folders=("AC/DC", "Album: Vol. 1...")
        )

        assert target == (
            tmp_path / "library" / "audio" / "AC_DC" / "Album_ Vol. 1"
            / "01 - Song_.m4a"
        ).resolve()

    @pytest.mark.parametrize(
        "folder, expected",
        [
            ("..", "download"),
            (".", "download"),
            ("../../outside", ".._.._outside"),
            ("a/../../b", "a_.._.._b"),
            ("/etc", "_etc"),
            ("\\\\server\\share", "__server_share"),
        ],
    )
    def test_traversal_and_absolute_folders_stay_one_segment(
        self, tmp_path: Path, folder: str, expected: str
    ) -> None:
        storage = self._storage(tmp_path)

        target = storage.final_target("video", "Clip.mp4", folders=(folder,))

        assert target == (
            tmp_path / "library" / "video" / expected / "Clip.mp4"
        ).resolve()
        assert target.parent.parent == (tmp_path / "library" / "video").resolve()

    def test_unsupported_media_kind_rejected(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path)

        for media_kind in ("", "music", "../audio", "audio/x"):
            with pytest.raises(ValueError):
                storage.final_target(media_kind, "Clip.mp4", folders=("A",))

    @pytest.mark.parametrize("folders", ["Artist", "", ["Artist"], [], None])
    def test_non_tuple_folders_rejected(self, tmp_path: Path, folders: Any) -> None:
        storage = self._storage(tmp_path)

        with pytest.raises(TypeError):
            storage.final_target("audio", "a.mp3", folders=folders)

    @pytest.mark.parametrize("element", [1, None, b"Artist", Path("Artist")])
    def test_non_string_folder_elements_rejected(
        self, tmp_path: Path, element: Any
    ) -> None:
        storage = self._storage(tmp_path)

        with pytest.raises(TypeError):
            storage.final_target("audio", "a.mp3", folders=("Artist", element))

    @pytest.mark.parametrize("blank", ["", " ", "\t\n"])
    def test_blank_folders_rejected(self, tmp_path: Path, blank: str) -> None:
        storage = self._storage(tmp_path)

        with pytest.raises(ValueError):
            storage.final_target("audio", "a.mp3", folders=("Artist", blank))

    def test_unicode_segments_are_byte_bounded(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path)

        target = storage.final_target(
            "audio", "Song.opus", folders=("視" * 60, "🎵" * 50, "a" * 200)
        )

        relative = target.relative_to((tmp_path / "library").resolve())
        assert relative.parts[1:4] == ("視" * 50, "🎵" * 37, "a" * 150)
        for part in relative.parts:
            assert len(part.encode("utf-8")) <= 150

    def test_planning_does_not_mutate_filesystem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = self._storage(tmp_path)
        before = _tree_snapshot(tmp_path)
        calls = self._forbid_mkdir(monkeypatch)

        target = storage.final_target(
            "audio", "01 - Song.m4a", folders=("Artist", "Album")
        )

        assert calls == []
        assert not target.parent.exists()
        assert _tree_snapshot(tmp_path) == before

    @pytest.mark.parametrize("dangling", [False, True])
    def test_folder_symlink_escaping_root_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dangling: bool
    ) -> None:
        storage = self._storage(tmp_path)
        outside = tmp_path / "outside"
        if not dangling:
            outside.mkdir()
            (outside / "keep.txt").write_text("keep")
        (tmp_path / "library" / "audio" / "Artist").symlink_to(
            outside, target_is_directory=True
        )
        before = _tree_snapshot(tmp_path)
        calls = self._forbid_mkdir(monkeypatch)

        with pytest.raises(ValueError):
            storage.final_target("audio", "Song.m4a", folders=("Artist", "Album"))

        assert calls == []
        assert _tree_snapshot(tmp_path) == before
        if dangling:
            assert not outside.exists()
        else:
            assert (outside / "keep.txt").read_text() == "keep"
            assert not (outside / "Album").exists()

    def test_media_kind_symlink_escaping_root_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        (tmp_path / "library").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "library" / "audio").symlink_to(outside, target_is_directory=True)
        before = _tree_snapshot(tmp_path)
        calls = self._forbid_mkdir(monkeypatch)

        for folders in ((), ("Artist",)):
            with pytest.raises(ValueError):
                storage.final_target("audio", "Song.m4a", folders=folders)

        assert calls == []
        assert _tree_snapshot(tmp_path) == before
        assert list(outside.iterdir()) == []

    def test_symlink_inside_root_keeps_existing_behavior(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)
        real = tmp_path / "library" / "audio" / "Real Artist"
        real.mkdir()
        (tmp_path / "library" / "audio" / "Alias").symlink_to(
            real, target_is_directory=True
        )

        target = storage.final_target("audio", "Song.m4a", folders=("Alias",))

        assert target == (real / "Song.m4a").resolve()


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
    _RACE_NAMES = {
        "Same Title.mp4", "Same Title (2).mp4", "Same Title (3).mp4", "Same Title (4).mp4",
    }

    def test_concurrent_placements_publish_separate_numbered_paths(
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
        # Every racer attempts the same first name at the same moment.
        self._synchronize_first_link(monkeypatch, target, len(sources))

        async def place_all():
            return await asyncio.gather(
                *(
                    storage.place_final(
                        source, media_kind="video", title="Same Title",
                        staging_key=str(index),
                    )
                    for index, source in enumerate(sources)
                )
            )

        results = asyncio.run(place_all())
        assert {result.final_path.name for result in results} == self._RACE_NAMES
        contents = {result.final_path.read_text() for result in results}
        assert contents == {f"media-{index}" for index in range(4)}
        for result in results:
            placed = result.final_path.stat()
            assert result.identity == (placed.st_dev, placed.st_ino)
        assert not any(source.exists() for source in sources)

    def test_concurrent_cross_filesystem_copies_publish_separate_paths(
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
        real_link = os.link
        barrier = threading.Barrier(len(sources))
        waited: set[str] = set()
        lock = threading.Lock()

        def cross_device_sources(src: Any, dst: Any, **kwargs: Any) -> None:
            if not _is_temp(src):
                raise OSError(errno.EXDEV, "cross-device link")
            # Every temporary copy races for the same first name.
            with lock:
                first = str(dst) == target.name and str(src) not in waited
                if first:
                    waited.add(str(src))
            if first:
                barrier.wait(timeout=2)
            real_link(src, dst, **kwargs)

        monkeypatch.setattr(os, "link", cross_device_sources)

        async def place_all():
            return await asyncio.gather(
                *(
                    storage.place_final(
                        source, media_kind="video", title="Same Title",
                        staging_key=str(index),
                    )
                    for index, source in enumerate(sources)
                )
            )

        results = asyncio.run(place_all())
        assert {result.final_path.name for result in results} == self._RACE_NAMES
        contents = {result.final_path.read_text() for result in results}
        assert contents == {f"media-{index}" for index in range(4)}
        assert not list(target.parent.glob("*.part"))

    def test_cross_filesystem_copy_is_hidden_and_does_not_block_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        source = storage.job_staging_dir("job") / "media.mp4"
        source.write_text("media")
        target = tmp_path / "library" / "video" / "Title.mp4"
        real_copy = shutil.copyfileobj
        copy_started = threading.Event()
        allow_copy = threading.Event()

        def blocked_copy(original: Any, copied: Any) -> None:
            copy_started.set()
            assert allow_copy.wait(timeout=2)
            real_copy(original, copied)

        monkeypatch.setattr(os, "link", _exdev_for_source(os.link))
        monkeypatch.setattr(shutil, "copyfileobj", blocked_copy)

        async def run() -> None:
            placement_task = asyncio.create_task(
                storage.place_final(
                    source, media_kind="video", title="Title", staging_key="job",
                )
            )
            assert await asyncio.to_thread(copy_started.wait, 2)
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
            assert not target.exists()
            allow_copy.set()
            result = await placement_task
            assert result.final_path.name == target.name

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

        def cross_device_link(*_args: Any, **_kwargs: Any) -> None:
            raise OSError(errno.EXDEV, "cross-device link")

        def failed_copy(_original: Any, copied: Any) -> None:
            copied.write(b"partial")
            raise OSError("copy failed")

        monkeypatch.setattr(os, "link", cross_device_link)
        monkeypatch.setattr(shutil, "copyfileobj", failed_copy)

        with pytest.raises(OSError, match="copy failed"):
            asyncio.run(storage.place_final(
                source, media_kind="video", title="Title", staging_key="job",
            ))

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
        real_copy = shutil.copyfileobj
        copy_started = threading.Event()
        allow_copy = threading.Event()

        def blocked_copy(original: Any, copied: Any) -> None:
            copy_started.set()
            assert allow_copy.wait(timeout=2)
            real_copy(original, copied)

        monkeypatch.setattr(os, "link", _exdev_for_source(os.link))
        monkeypatch.setattr(shutil, "copyfileobj", blocked_copy)

        async def run() -> None:
            placement_task = asyncio.create_task(
                storage.place_final(
                    source, media_kind="video", title="Title", staging_key="job",
                )
            )
            assert await asyncio.to_thread(copy_started.wait, 2)
            placement_task.cancel()
            allow_copy.set()
            result = await asyncio.wait_for(placement_task, timeout=2)
            assert result.final_path.name == target.name

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
            storage.place_final(
                source, media_kind="video", title="x" * 200, staging_key="job",
            )
        )

        # 150 bytes minus ".longextxx" and the reserved widest " (999)".
        assert result.final_path.name == "x" * 134 + ".longextxx"
        assert result.final_path.read_text() == "media"

    @staticmethod
    def _storage(tmp_path: Path) -> StorageService:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        return storage

    @staticmethod
    def _source(storage: StorageService, content: str = "new", suffix: str = ".mp4") -> Path:
        source = storage.job_staging_dir("job") / f"media{suffix}"
        source.write_text(content)
        return source

    @pytest.mark.parametrize("short_edge,name", [
        (720, "Title (720p).mp4"), (1080, "Title (1080p).mp4"), (None, "Title.mp4"),
    ])
    def test_video_name_carries_measured_short_edge_or_nothing(
        self, tmp_path: Path, short_edge: int | None, name: str
    ) -> None:
        storage = self._storage(tmp_path)
        source = self._source(storage)
        result = asyncio.run(storage.place_final(
            source, media_kind="video", title="Title", video_short_edge=short_edge,
        ))
        assert result.relative_path == f"video/{name}"
        assert result.final_path.read_text() == "new"
        assert not source.exists()

    @pytest.mark.parametrize("media_kind,short_edge,existing,expected", [
        ("video", 720, ["Title (720p).mp4"], "Title (720p) (2).mp4"),
        ("video", 720, ["Title (720p).mp4", "Title (720p) (2).mp4"], "Title (720p) (3).mp4"),
        ("video", None, ["Title.mp4"], "Title (2).mp4"),
        ("audio", None, ["Title.m4a"], "Title (2).m4a"),
        # A historical suffix-less file is kept and is no conflict for 720p.
        ("video", 720, ["Title.mp4"], "Title (720p).mp4"),
        # A different measured resolution is a different name.
        ("video", 720, ["Title (1080p).mp4"], "Title (720p).mp4"),
    ])
    def test_conflict_publishes_next_free_number_and_keeps_existing(
        self, tmp_path: Path, media_kind: str, short_edge: int | None,
        existing: list[str], expected: str,
    ) -> None:
        storage = self._storage(tmp_path)
        suffix = ".m4a" if media_kind == "audio" else ".mp4"
        folder = tmp_path / "library" / media_kind
        for name in existing:
            (folder / name).write_text(f"old {name}")
        source = self._source(storage, suffix=suffix)
        result = asyncio.run(storage.place_final(
            source, media_kind=media_kind, title="Title", video_short_edge=short_edge,
        ))
        assert result.relative_path == f"{media_kind}/{expected}"
        assert result.final_path.read_text() == "new"
        placed = result.final_path.stat()
        assert result.identity == (placed.st_dev, placed.st_ino)
        assert result.size_bytes == len("new")
        for name in existing:
            assert (folder / name).read_text() == f"old {name}"
        assert not source.exists()

    def test_same_request_twice_publishes_numbered_copy(self, tmp_path: Path) -> None:
        # Approved interim behaviour before 3b-2 "Already exists".
        storage = self._storage(tmp_path)
        first = asyncio.run(storage.place_final(
            self._source(storage, "one"), media_kind="video", title="T", video_short_edge=720,
        ))
        second = asyncio.run(storage.place_final(
            self._source(storage, "two"), media_kind="video", title="T", video_short_edge=720,
        ))
        assert (first.final_path.name, second.final_path.name) == (
            "T (720p).mp4", "T (720p) (2).mp4")
        assert (first.final_path.read_text(), second.final_path.read_text()) == ("one", "two")

    def test_long_unicode_title_shares_body_across_numbers(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path)
        title = "Şarkı ğüé 🎵 " * 30
        results = [
            asyncio.run(storage.place_final(
                self._source(storage, str(index)), media_kind="video", title=title,
                video_short_edge=720,
            ))
            for index in range(3)
        ]
        names = [result.final_path.name for result in results]
        body = names[0].removesuffix(" (720p).mp4")
        assert names == [
            f"{body} (720p).mp4", f"{body} (720p) (2).mp4", f"{body} (720p) (3).mp4",
        ]
        assert body.startswith("Şarkı")
        for name in names:
            assert len(name.encode()) <= 150
            assert unicodedata.is_normalized("NFC", name)

    @pytest.mark.parametrize("kind", ["file", "symlink", "dangling", "outside", "directory"])
    def test_any_occupied_leaf_is_skipped_never_followed(
        self, tmp_path: Path, kind: str
    ) -> None:
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "video"
        occupied = folder / "Title (720p).mp4"
        inside = folder / "inside.mp4"
        inside.write_text("inside")
        outside = tmp_path / "outside.mp4"
        outside.write_text("outside")
        if kind == "file":
            occupied.write_text("old")
        elif kind == "symlink":
            occupied.symlink_to(inside)
        elif kind == "dangling":
            occupied.symlink_to(folder / "missing.mp4")
        elif kind == "outside":
            occupied.symlink_to(outside)
        else:
            occupied.mkdir()
        source = self._source(storage)
        result = asyncio.run(storage.place_final(
            source, media_kind="video", title="Title", video_short_edge=720,
        ))
        assert result.final_path.parent == folder.resolve()
        assert result.final_path.name == "Title (720p) (2).mp4"
        assert not result.final_path.is_symlink()
        assert result.final_path.read_text() == "new"
        assert inside.read_text() == "inside"
        assert outside.read_text() == "outside"
        assert not (folder / "missing.mp4").exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "library", "outside.mp4", "staging",
        ]
        if kind == "file":
            assert occupied.read_text() == "old"
        elif kind == "directory":
            assert occupied.is_dir() and not list(occupied.iterdir())
        else:
            assert occupied.is_symlink()

    @pytest.mark.parametrize("cross_filesystem", [False, True])
    def test_counter_exhaustion_fails_without_touching_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cross_filesystem: bool
    ) -> None:
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "video"
        names = conflict_media_filenames("Title", "mp4", qualifier=" (720p)")
        assert len(names) == 999 and names[-1] == "Title (720p) (999).mp4"
        for name in names:
            (folder / name).write_text("old")
        source = self._source(storage)
        if cross_filesystem:
            monkeypatch.setattr(os, "link", _exdev_for_source(os.link))
        with pytest.raises(FileExistsError):
            asyncio.run(storage.place_final(
                source, media_kind="video", title="Title", video_short_edge=720,
                staging_key="job",
            ))
        assert source.read_text() == "new"
        assert sorted(p.name for p in folder.iterdir()) == sorted(names)
        assert all((folder / name).read_text() == "old" for name in names)

    def test_last_counter_999_is_still_published(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "audio"
        names = conflict_media_filenames("Title", "m4a")
        for name in names[:-1]:
            (folder / name).write_text("old")
        result = asyncio.run(storage.place_final(
            self._source(storage, suffix=".m4a"), media_kind="audio", title="Title",
        ))
        assert result.final_path.name == "Title (999).m4a"
        assert result.final_path.read_text() == "new"

    def test_exdev_copies_once_and_continues_at_same_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "video"
        (folder / "Title (720p).mp4").write_text("old")
        source = self._source(storage)
        real_link, real_copy = os.link, shutil.copyfileobj
        attempts: list[tuple[str, str]] = []
        copies: list[int] = []

        def cross_device(src: Any, dst: Any, **kwargs: Any) -> None:
            attempts.append((str(src), str(dst)))
            if not _is_temp(src):
                raise OSError(errno.EXDEV, "cross-device link")
            real_link(src, dst, **kwargs)

        def counted_copy(original: Any, copied: Any) -> None:
            copies.append(1)
            # A racer takes the first two names while the copy is in flight.
            (folder / "Title (720p) (2).mp4").write_text("racer")
            real_copy(original, copied)

        monkeypatch.setattr(os, "link", cross_device)
        monkeypatch.setattr(shutil, "copyfileobj", counted_copy)
        result = asyncio.run(storage.place_final(
            source, media_kind="video", title="Title", video_short_edge=720,
            staging_key="job",
        ))
        assert result.final_path.name == "Title (720p) (3).mp4"
        assert result.final_path.read_text() == "new"
        assert len(copies) == 1
        # One EXDEV on the source, then the single hidden copy walks the names.
        assert attempts[0][0] == "media.mp4" and _is_temp(attempts[0][1])
        assert [dst for _src, dst in attempts[1:]] == [
            "Title (720p).mp4", "Title (720p) (2).mp4", "Title (720p) (3).mp4",
        ]
        assert len({src for src, _dst in attempts[1:]}) == 1
        placed = result.final_path.stat()
        assert result.identity == (placed.st_dev, placed.st_ino)
        assert (folder / "Title (720p).mp4").read_text() == "old"
        assert (folder / "Title (720p) (2).mp4").read_text() == "racer"
        assert not source.exists()
        assert not list(folder.glob("*.part"))

    @pytest.mark.parametrize("cross_filesystem", [False, True])
    def test_cancel_between_attempts_publishes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cross_filesystem: bool
    ) -> None:
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "video"
        (folder / "Title.mp4").write_text("old")
        source = self._source(storage)
        cancelled = threading.Event()
        guard_entries: list[bool] = []
        real_link = os.link

        class RecordingGuard:
            def __enter__(self) -> None:
                guard_entries.append(cancelled.is_set())

            def __exit__(self, *_: object) -> None:
                return None

        def link_then_cancel(src: Any, dst: Any, **kwargs: Any) -> None:
            if cross_filesystem and not _is_temp(src):
                raise OSError(errno.EXDEV, "cross-device link")
            try:
                real_link(src, dst, **kwargs)
            except FileExistsError:
                # Cancellation arrives right after the first occupied name.
                cancelled.set()
                raise

        monkeypatch.setattr(os, "link", link_then_cancel)
        with pytest.raises(PlacementCancelled):
            asyncio.run(storage.place_final(
                source, media_kind="video", title="Title", staging_key="job",
                cancel_requested=cancelled.is_set, publish_guard=RecordingGuard(),
            ))
        # Every attempt re-enters the guard; the second sees the cancel.
        assert guard_entries[-1] is True and guard_entries[-2] is False
        assert source.read_text() == "new"
        assert sorted(p.name for p in folder.iterdir()) == ["Title.mp4"]
        assert (folder / "Title.mp4").read_text() == "old"

    @pytest.mark.parametrize("media_kind,value,error", [
        ("video", 0, ValueError), ("video", -720, ValueError),
        ("video", True, TypeError), ("video", 720.0, TypeError), ("video", "720", TypeError),
        ("audio", 720, ValueError),
    ])
    def test_invalid_short_edge_rejected_without_mutation(
        self, tmp_path: Path, media_kind: str, value: Any, error: type[Exception]
    ) -> None:
        storage = self._storage(tmp_path)
        source = self._source(storage)
        with pytest.raises(error):
            asyncio.run(storage.place_final(
                source, media_kind=media_kind, title="Title", video_short_edge=value,
            ))
        assert source.read_text() == "new"
        assert not list((tmp_path / "library" / "video").iterdir())
        assert not list((tmp_path / "library" / "audio").iterdir())

    # --- review must-fix regressions: ownership, parent, published inode ---

    def test_source_outside_own_staging_is_rejected_without_mutation(
        self, tmp_path: Path
    ) -> None:
        storage = self._storage(tmp_path)
        foreign = storage.job_staging_dir("other") / "media.mp4"
        foreign.write_text("foreign")
        with pytest.raises(StoragePathError):
            asyncio.run(storage.place_final(
                foreign, media_kind="video", title="Title", staging_key="job",
            ))
        assert foreign.read_text() == "foreign"
        assert not list((tmp_path / "library" / "video").iterdir())

    @pytest.mark.parametrize("cross_filesystem", [False, True])
    def test_source_swap_after_first_conflict_never_publishes_or_deletes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cross_filesystem: bool
    ) -> None:
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "video"
        (folder / "Title.mp4").write_text("old")
        source = self._source(storage)
        real_link = os.link

        def swap_on_conflict(src: Any, dst: Any, **kwargs: Any) -> None:
            if cross_filesystem and not _is_temp(src):
                raise OSError(errno.EXDEV, "cross-device link")
            try:
                real_link(src, dst, **kwargs)
            except FileExistsError:
                source.rename(source.with_suffix(".saved"))
                source.write_text("replacement")
                raise

        monkeypatch.setattr(os, "link", swap_on_conflict)
        with pytest.raises(MediaFileTargetChangedError):
            asyncio.run(storage.place_final(
                source, media_kind="video", title="Title", staging_key="job",
            ))
        assert source.read_text() == "replacement"  # the new source: untouched
        assert source.with_suffix(".saved").read_text() == "new"
        assert sorted(p.name for p in folder.iterdir()) == ["Title.mp4"]  # temp cleaned

    def test_source_swap_before_hidden_link_never_publishes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "video"
        source = self._source(storage)
        real_link = os.link

        def swap_then_link(src: Any, dst: Any, **kwargs: Any) -> None:
            if not _is_temp(src):  # the staging leaf is swapped right before
                source.rename(source.with_suffix(".saved"))
                source.write_text("replacement")
            real_link(src, dst, **kwargs)

        monkeypatch.setattr(os, "link", swap_then_link)
        with pytest.raises(MediaFileTargetChangedError):
            asyncio.run(storage.place_final(
                source, media_kind="video", title="Title", staging_key="job",
            ))
        assert source.read_text() == "replacement"
        assert not list(folder.iterdir())

    def test_parent_swap_after_first_conflict_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "video"
        (folder / "Title.mp4").write_text("old")
        outside = tmp_path / "outside"
        outside.mkdir()
        source = self._source(storage)
        real_link = os.link

        def swap_parent_on_conflict(src: Any, dst: Any, **kwargs: Any) -> None:
            try:
                real_link(src, dst, **kwargs)
            except FileExistsError:
                folder.rename(tmp_path / "library" / "moved")
                folder.symlink_to(outside, target_is_directory=True)
                raise

        monkeypatch.setattr(os, "link", swap_parent_on_conflict)
        with pytest.raises(StoragePathError):
            asyncio.run(storage.place_final(
                source, media_kind="video", title="Title", staging_key="job",
            ))
        assert not list(outside.iterdir())  # no write or cleanup outside the root
        moved = tmp_path / "library" / "moved"
        assert [p.name for p in moved.iterdir() if not p.name.startswith(".")] == [
            "Title.mp4"]
        assert source.read_text() == "new"

    def test_symlinked_media_directory_is_rejected(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "video"
        real = tmp_path / "library" / "real-video"
        folder.rmdir()
        real.mkdir()
        folder.symlink_to(real, target_is_directory=True)  # even inside the root
        source = self._source(storage)
        with pytest.raises(StoragePathError):
            asyncio.run(storage.place_final(
                source, media_kind="video", title="Title", staging_key="job",
            ))
        assert not list(real.iterdir())
        assert source.read_text() == "new"

    @pytest.mark.parametrize("cross_filesystem", [False, True])
    def test_target_swap_right_after_link_fails_closed_and_keeps_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cross_filesystem: bool
    ) -> None:
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "video"
        source = self._source(storage)
        real_link = os.link

        def link_then_replace(src: Any, dst: Any, **kwargs: Any) -> None:
            if cross_filesystem and not _is_temp(src):
                raise OSError(errno.EXDEV, "cross-device link")
            real_link(src, dst, **kwargs)
            if not _is_temp(dst):
                (folder / dst).unlink()
                (folder / dst).write_text("impostor")

        monkeypatch.setattr(os, "link", link_then_replace)
        with pytest.raises(MediaFileTargetChangedError):
            asyncio.run(storage.place_final(
                source, media_kind="video", title="Title", staging_key="job",
            ))
        assert source.read_text() == "new"  # never deleted early
        assert (folder / "Title.mp4").read_text() == "impostor"  # never auto-deleted

    @pytest.mark.parametrize("change", ["vanished", "replaced"])
    @pytest.mark.parametrize("cross_filesystem", [False, True])
    def test_target_change_at_gate_exit_with_same_root_keeps_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        change: str, cross_filesystem: bool,
    ) -> None:
        """Root stays; the visible target changes only when the gate is released."""
        storage = self._storage(tmp_path)
        folder = tmp_path / "library" / "video"
        source = self._source(storage)
        real_link = os.link
        published: list[str] = []

        def link(src: Any, dst: Any, **kwargs: Any) -> None:
            if cross_filesystem and not _is_temp(src):
                raise OSError(errno.EXDEV, "cross-device link")
            real_link(src, dst, **kwargs)
            if not _is_temp(dst):
                published.append(str(dst))

        class Guard:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_: object) -> None:
                if published:  # in-gate checks passed; now change the target
                    target = folder / published[0]
                    target.unlink()
                    if change == "replaced":
                        target.write_text("impostor")

        monkeypatch.setattr(os, "link", link)
        with pytest.raises(MediaFileTargetChangedError):
            asyncio.run(storage.place_final(
                source, media_kind="video", title="Title", staging_key="job",
                publish_guard=Guard(),
            ))
        assert published == ["Title.mp4"]
        assert source.read_text() == "new"  # staged source kept
        if change == "replaced":
            assert (folder / "Title.mp4").read_text() == "impostor"  # never deleted
            assert sorted(p.name for p in folder.iterdir()) == ["Title.mp4"]
        else:
            assert not list(folder.iterdir())  # nothing recreated; temp cleaned

    @pytest.mark.parametrize("moment,swap,startup", [
        ("copy", "moved", False),
        ("copy", "replaced", True),
        ("eexist", "moved", True),
        ("eexist", "replaced", False),
        ("after_link", "moved", True),
        ("gate_exit", "replaced", False),
    ])
    def test_root_swap_during_placement_fails_closed_and_keeps_staging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        moment: str, swap: str, startup: bool,
    ) -> None:
        """The held root fd never publishes/unlinks after the root changed.

        The App Library root is moved (or moved and replaced by a real
        directory) while the EXDEV copy runs, between ``EEXIST`` attempts,
        right after the visible link, or when the gate is released. The
        startup snapshot or the placement-start fallback capture is replayed,
        never re-captured.
        """
        storage = self._storage(tmp_path)
        if startup:
            storage.initialize_delete_roots()
        library = tmp_path / "library"
        old = tmp_path / "old-library"
        if moment == "eexist":
            (library / "video" / "Title.mp4").write_text("old")
        source = self._source(storage)
        real_link, real_copy = os.link, shutil.copyfileobj
        published: list[str] = []

        def swap_root() -> None:
            library.rename(old)
            if swap == "replaced":
                (library / "video").mkdir(parents=True)

        def copy(original: Any, copied: Any) -> None:
            if moment == "copy":
                swap_root()  # while the cross-filesystem copy is running
            real_copy(original, copied)

        def link(src: Any, dst: Any, **kwargs: Any) -> None:
            if not _is_temp(src):
                raise OSError(errno.EXDEV, "cross-device link")
            try:
                real_link(src, dst, **kwargs)
            except FileExistsError:
                if moment == "eexist":
                    swap_root()  # between two visible-name attempts
                raise
            published.append(str(dst))
            if moment == "after_link":
                swap_root()

        class Guard:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_: object) -> None:
                if moment == "gate_exit" and published and not old.exists():
                    swap_root()

        monkeypatch.setattr(os, "link", link)
        monkeypatch.setattr(shutil, "copyfileobj", copy)
        fd_dir = Path("/proc/self/fd")
        open_fds = len(list(fd_dir.iterdir())) if fd_dir.is_dir() else None
        with pytest.raises((StoragePathError, StorageTargetError)):
            asyncio.run(storage.place_final(
                source, media_kind="video", title="Title", staging_key="job",
                publish_guard=Guard(),
            ))
        assert source.read_text() == "new"  # staged bytes kept, never unlinked
        if open_fds is not None:
            assert len(list(fd_dir.iterdir())) == open_fds  # no leaked descriptor
        if swap == "replaced":
            assert not list((library / "video").iterdir())  # new root untouched
        else:
            assert not library.exists()
        visible = sorted(p.name for p in (old / "video").iterdir() if not _is_temp(p.name))
        if moment in {"after_link", "gate_exit"}:
            # Published before the change: kept, never rolled back.
            assert published == ["Title.mp4"] and visible == ["Title.mp4"]
            assert (old / "video" / "Title.mp4").read_text() == "new"
        else:
            # No visible name was ever published after the change.
            assert published == []
            assert visible == (["Title.mp4"] if moment == "eexist" else [])
            if moment == "eexist":
                assert (old / "video" / "Title.mp4").read_text() == "old"
        # Our own temp is still removed fd-relative in the held directory.
        assert not list((old / "video").glob("*.part"))

    @staticmethod
    def _synchronize_first_link(
        monkeypatch: pytest.MonkeyPatch, target: Path, participants: int
    ) -> None:
        barrier = threading.Barrier(participants)
        real_link = os.link

        def synchronized_link(src: Any, dst: Any, **kwargs: Any) -> None:
            if str(dst) == target.name:
                barrier.wait(timeout=2)
            real_link(src, dst, **kwargs)

        monkeypatch.setattr(os, "link", synchronized_link)


def _is_temp(name: Any) -> bool:
    """Hidden ``.<name>.<rand>.part`` temps; anything else is a source/target."""
    return str(name).startswith(".")


def _exdev_for_source(real_link: Callable[..., None]) -> Callable[..., None]:
    def link(src: Any, dst: Any, **kwargs: Any) -> None:
        if not _is_temp(src):  # the staged source leaf itself
            raise OSError(errno.EXDEV, "cross-device link")
        real_link(src, dst, **kwargs)
    return link
