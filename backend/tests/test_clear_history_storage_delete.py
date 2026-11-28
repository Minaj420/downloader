"""M6-Q3-Storage: Q3-only staging removal primitive (ARCHITECTURE.md §59 Q3
storage boundary, PRODUCT_SPEC.md §17 Clear History storage threat model).

Everything runs under ``tmp_path``. The mount table is stubbed (synthetic
``/proc/self/mountinfo`` content) except in the single Linux-native test,
which is skipped elsewhere; no Compose/container result is assumed here.
"""

import logging
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.services.storage_service import (
    ClearHistoryStorageError,
    StagingRootVerificationError,
    StorageService,
)

LOGGER = "app.services.storage_service"
KEY = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"


def _escape(path: str) -> str:
    return (
        path.replace("\\", "\\134")
        .replace(" ", "\\040")
        .replace("\t", "\\011")
        .replace("\n", "\\012")
    )


MountSpec = Path | str | tuple[Path | str, str, str]


def _line(mount_id: int, parent: int, device: str, root: str, point: str) -> str:
    return (
        f"{mount_id} {parent} {device} {_escape(root)} {_escape(point)} "
        f"rw,relatime shared:{mount_id} - ext4 /dev/x rw"
    )


def _raw(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def _mountinfo(*specs: MountSpec) -> bytes:
    """Consistent synthetic table: ``/`` first, each parent its visible mount.

    A plain path is a same-filesystem bind of itself (device ``0:1``); a
    ``(point, major:minor, root)`` tuple names an explicit bind source.
    """
    entries: list[tuple[str, str, str]] = [("/", "0:1", "/")]
    for spec in specs:
        if isinstance(spec, tuple):
            entries.append((str(spec[0]), spec[1], spec[2]))
        else:
            entries.append((str(spec), "0:1", str(spec)))
    placed: list[tuple[int, Path]] = []
    lines = []
    for mount_id, (point, device, root) in enumerate(entries, start=20):
        path = Path(point)
        parent, depth = 1, -1
        for other_id, other in placed:
            if path.is_relative_to(other) and len(other.parts) >= depth:
                parent, depth = other_id, len(other.parts)
        placed.append((mount_id, path))
        lines.append(_line(mount_id, parent, device, root, point))
    return _raw(*lines)


def _stub_mountinfo(
    monkeypatch: pytest.MonkeyPatch, data: bytes | BaseException
) -> None:
    def read() -> bytes:
        if isinstance(data, BaseException):
            raise data
        return data

    monkeypatch.setattr(StorageService, "_read_mountinfo", staticmethod(read))


def _tree(root: Path) -> dict[str, tuple[str, bytes | str]]:
    """No-follow snapshot of every entry below ``root`` (type + content)."""
    entries: dict[str, tuple[str, bytes | str]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            path = base / name
            key = str(path.relative_to(root))
            if path.is_symlink():
                entries[key] = ("link", os.readlink(path))
            elif path.is_dir():
                entries[key] = ("dir", "")
            else:
                entries[key] = ("file", path.read_bytes())
    return entries


def _populate(tmp_path: Path, staging: Path, library: Path) -> None:
    (staging / KEY / "sub").mkdir(parents=True, exist_ok=True)
    (staging / KEY / "partial.bin").write_bytes(b"partial")
    (staging / KEY / "sub" / "nested.bin").write_bytes(b"nested")
    (staging / OTHER).mkdir(parents=True, exist_ok=True)
    (staging / OTHER / "keep.bin").write_bytes(b"other job")
    (library / "video").mkdir(parents=True, exist_ok=True)
    (library / "video" / "Clip.mp4").write_bytes(b"library media")
    (tmp_path / "outside").mkdir(exist_ok=True)
    (tmp_path / "outside" / "secret.bin").write_bytes(b"outside")


def _start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    staging: Path | None = None,
    library: Path | None = None,
    plex_music: Path | None = None,
    before_start: Callable[[Path, Path], None] | None = None,
    mounts: tuple[MountSpec, ...] | None = None,
) -> StorageService:
    staging = staging if staging is not None else tmp_path / "staging"
    library = library if library is not None else tmp_path / "library"
    storage = StorageService(staging, library, plex_music_root=plex_music)
    storage.ensure_layout()
    _populate(tmp_path, staging, library)
    if before_start is not None:
        before_start(staging, library)
    storage.initialize_delete_roots()
    storage.initialize_staging_root()
    if mounts is None:
        mounts = (staging.resolve(),)
    _stub_mountinfo(monkeypatch, _mountinfo(*mounts))
    return storage


def _protected(tmp_path: Path) -> list[Path]:
    return [tmp_path / "staging" / OTHER, tmp_path / "library", tmp_path / "outside"]


def _refuse(
    storage: StorageService, reason: str, key: str = KEY
) -> ClearHistoryStorageError:
    with pytest.raises(ClearHistoryStorageError) as excinfo:
        storage.remove_staging_for_clear_history(key)
    assert excinfo.value.reason == reason
    return excinfo.value


def _assert_path_free(text: str, tmp_path: Path) -> None:
    for raw in {str(tmp_path), str(tmp_path.resolve()), tmp_path.name, KEY}:
        assert raw not in text


StatHook = Callable[[os.DirEntry[str], bool], os.stat_result]


class _SpyEntry:
    def __init__(self, inner: os.DirEntry[str], hook: StatHook) -> None:
        self._inner, self._hook = inner, hook
        self.path, self.name = inner.path, inner.name

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        return self._hook(self._inner, follow_symlinks)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _SpyEntries:
    def __init__(self, inner: Any, hook: StatHook) -> None:
        self._inner, self._hook = inner, hook

    def __enter__(self) -> "_SpyEntries":
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.close()

    def __iter__(self) -> Any:
        for entry in self._inner:
            yield _SpyEntry(entry, self._hook)


def _spy_scandir(
    monkeypatch: pytest.MonkeyPatch, hook: StatHook, opened: list[Path]
) -> None:
    real_scandir = os.scandir

    def scandir(path: Any) -> Any:
        if isinstance(path, int):  # rmtree's own descriptor walk
            return real_scandir(path)
        opened.append(Path(os.fspath(path)))
        return _SpyEntries(real_scandir(path), hook)

    monkeypatch.setattr(os, "scandir", scandir)


def _fake_rmtree(
    effect: Callable[[Any], None],
) -> Callable[..., None]:
    def fake(path: Any, *args: Any, **kwargs: Any) -> None:
        effect(path)

    fake.avoids_symlink_attacks = True  # type: ignore[attr-defined]
    return fake


class TestRemoval:
    def test_same_root_removes_only_intended_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        before = [_tree(p) for p in _protected(tmp_path)]
        calls: list[int] = []
        real_verify = StorageService.verify_staging_root_for_clear_history

        def spy(self: StorageService) -> None:
            calls.append(1)
            real_verify(self)

        monkeypatch.setattr(StorageService, "verify_staging_root_for_clear_history", spy)
        assert storage.remove_staging_for_clear_history(KEY) is True
        assert calls == [1]
        assert not os.path.lexists(tmp_path / "staging" / KEY)
        assert (tmp_path / "staging").is_dir()
        assert [_tree(p) for p in _protected(tmp_path)] == before

    def test_missing_key_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        before = _tree(tmp_path)
        missing = "a" * 32
        assert storage.remove_staging_for_clear_history(missing) is False
        assert _tree(tmp_path) == before
        assert storage.remove_staging_for_clear_history(KEY) is True
        assert storage.remove_staging_for_clear_history(KEY) is False

    def test_nested_symlinks_and_hardlink_never_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def links(staging: Path, library: Path) -> None:
            job = staging / KEY / "sub"
            (job / "to_outside").symlink_to(tmp_path / "outside", target_is_directory=True)
            (job / "to_other").symlink_to(staging / OTHER, target_is_directory=True)
            (job / "to_library").symlink_to(library / "video" / "Clip.mp4")
            (job / "to_library_dir").symlink_to(library, target_is_directory=True)
            (job / "dangling").symlink_to(tmp_path / "nowhere")
            os.link(library / "video" / "Clip.mp4", job / "hard.mp4")

        storage = _start(tmp_path, monkeypatch, before_start=links)
        clip = tmp_path / "library" / "video" / "Clip.mp4"
        assert clip.stat().st_nlink == 2
        before = [_tree(p) for p in _protected(tmp_path)]

        assert storage.remove_staging_for_clear_history(KEY) is True
        assert not os.path.lexists(tmp_path / "staging" / KEY)
        assert [_tree(p) for p in _protected(tmp_path)] == before
        assert clip.read_bytes() == b"library media"
        assert clip.stat().st_nlink == 1


class TestRefusedBeforeAnyMutation:
    @pytest.mark.parametrize(
        "key",
        ["", ".", "..", "a/b", "../x", "/abs", f"/{KEY}", "a\\b", "a\x00b", "\x00",
         f"{KEY}/..", "./x"],
    )
    def test_invalid_key_rejected_before_any_filesystem_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        before = _tree(tmp_path)

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("no filesystem call for an invalid key")

        with monkeypatch.context() as patch:
            for name in (
                "lstat", "stat", "open", "scandir", "walk", "readlink", "listdir",
                "unlink", "rmdir",
            ):
                patch.setattr(os, name, forbidden)
            patch.setattr(shutil, "rmtree", forbidden)
            patch.setattr(StorageService, "_read_mountinfo", staticmethod(forbidden))
            patch.setattr(
                StorageService, "verify_staging_root_for_clear_history", forbidden
            )
            _refuse(storage, "invalid_key", key)
        assert _tree(tmp_path) == before

    def test_non_string_key_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        _refuse(storage, "invalid_key", 123)  # type: ignore[arg-type]

    def test_changed_root_removes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        staging = tmp_path / "staging"
        staging.rename(tmp_path / "staging-old")
        (staging / KEY).mkdir(parents=True)
        (staging / KEY / "new.bin").write_bytes(b"new")
        before = _tree(tmp_path)
        with pytest.raises(StagingRootVerificationError) as excinfo:
            storage.remove_staging_for_clear_history(KEY)
        assert excinfo.value.reason == "identity_changed"
        assert _tree(tmp_path) == before

    def test_snapshot_missing_removes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging, library = tmp_path / "staging", tmp_path / "library"
        storage = StorageService(staging, library)
        storage.ensure_layout()
        _populate(tmp_path, staging, library)
        storage.initialize_delete_roots()
        _stub_mountinfo(monkeypatch, _mountinfo(staging))
        before = _tree(tmp_path)
        with pytest.raises(StagingRootVerificationError):
            storage.remove_staging_for_clear_history(KEY)
        assert _tree(tmp_path) == before

    def test_delete_roots_not_captured_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging, library = tmp_path / "staging", tmp_path / "library"
        storage = StorageService(staging, library)
        storage.ensure_layout()
        _populate(tmp_path, staging, library)
        storage.initialize_staging_root()
        _stub_mountinfo(monkeypatch, _mountinfo(staging.resolve()))
        before = _tree(tmp_path)
        _refuse(storage, "roots_unverified")
        assert _tree(tmp_path) == before

    @pytest.mark.parametrize("value", [False, None, "yes", 1])
    def test_rmtree_without_symlink_attack_protection_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        before = _tree(tmp_path)
        monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", value)
        _refuse(storage, "rmtree_unsafe")
        assert _tree(tmp_path) == before

    @pytest.mark.parametrize("kind", ["to_other", "to_outside", "dangling", "file"])
    def test_top_level_symlink_or_non_directory_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
    ) -> None:
        def replace_key(staging: Path, library: Path) -> None:
            shutil.rmtree(staging / KEY)
            target = staging / KEY
            if kind == "to_other":
                target.symlink_to(staging / OTHER, target_is_directory=True)
            elif kind == "to_outside":
                target.symlink_to(tmp_path / "outside", target_is_directory=True)
            elif kind == "dangling":
                target.symlink_to(tmp_path / "nowhere")
            else:
                target.write_bytes(b"not a dir")

        storage = _start(tmp_path, monkeypatch, before_start=replace_key)
        before = _tree(tmp_path)
        _refuse(storage, "target_not_directory")
        assert _tree(tmp_path) == before

    def test_errors_and_logs_are_path_free(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        storage = _start(tmp_path, monkeypatch, mounts=(tmp_path / "staging" / KEY,))
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            error = _refuse(storage, "sub_mount")
        assert "sub_mount" in caplog.text
        _assert_path_free(caplog.text + str(error), tmp_path)


class TestStaticRoots:
    @pytest.mark.parametrize(
        "layout", ["same", "library_in_staging", "staging_in_library", "library_alias"],
    )
    def test_same_or_nested_roots_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str,
    ) -> None:
        staging = tmp_path / "staging"
        if layout == "same":
            library = staging
        elif layout == "library_in_staging":
            library = staging / "lib"
        elif layout == "staging_in_library":
            staging = tmp_path / "library" / "staging"
            library = tmp_path / "library"
        else:
            (staging / "lib").mkdir(parents=True)
            library = tmp_path / "libalias"
            library.symlink_to(staging / "lib", target_is_directory=True)
        storage = _start(tmp_path, monkeypatch, staging=staging, library=library)
        before = _tree(tmp_path)
        with pytest.raises(ClearHistoryStorageError) as excinfo:
            storage.remove_staging_for_clear_history(KEY)
        assert excinfo.value.reason in {"root_overlap", "alias_through_staging"}
        assert _tree(tmp_path) == before

    @pytest.mark.parametrize("relative", [False, True])
    def test_bridge_alias_through_staging_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool,
    ) -> None:
        """Library's access chain crosses staging; its real target is outside."""
        staging = tmp_path / "staging"
        real = tmp_path / "real-library"
        real.mkdir()
        (staging / KEY).mkdir(parents=True)
        (staging / KEY / "bridge").symlink_to(real, target_is_directory=True)
        library = tmp_path / "libalias"
        library.symlink_to(
            Path("staging") / KEY / "bridge" if relative else staging / KEY / "bridge",
            target_is_directory=True,
        )
        storage = _start(tmp_path, monkeypatch, staging=staging, library=library)
        assert storage._delete_roots["app_library"].resolved == real.resolve()
        before = _tree(tmp_path)
        _refuse(storage, "alias_through_staging")
        assert _tree(tmp_path) == before

    def test_plex_root_inside_staging_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plex = tmp_path / "staging" / OTHER / "plex"

        def make_plex(staging: Path, library: Path) -> None:
            plex.mkdir()

        storage = _start(tmp_path, monkeypatch, plex_music=plex, before_start=make_plex)
        assert "plex_music" in storage._delete_roots
        before = _tree(tmp_path)
        _refuse(storage, "root_overlap")
        assert _tree(tmp_path) == before

    def test_missing_plex_accepted_but_later_present_plex_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plex = tmp_path / "plex" / "music"
        storage = _start(tmp_path, monkeypatch, plex_music=plex)
        assert "plex_music" not in storage._delete_roots
        assert storage.remove_staging_for_clear_history(KEY) is True

        (tmp_path / "staging" / KEY).mkdir()
        (tmp_path / "staging" / KEY / "again.bin").write_bytes(b"again")
        plex.mkdir(parents=True)  # becomes reachable after startup
        before = _tree(tmp_path)
        _refuse(storage, "optional_root_unverifiable")
        assert _tree(tmp_path) == before

    def test_missing_plex_dangling_alias_through_staging_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plex = tmp_path / "plexlink"
        plex.symlink_to(tmp_path / "staging" / KEY / "missing" / "plex")
        storage = _start(tmp_path, monkeypatch, plex_music=plex)
        assert "plex_music" not in storage._delete_roots
        before = _tree(tmp_path)
        _refuse(storage, "alias_through_staging")
        assert _tree(tmp_path) == before

    @pytest.mark.parametrize("kind", ["file_component", "alias_loop"])
    def test_invalid_optional_root_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
    ) -> None:
        if kind == "file_component":
            (tmp_path / "afile").write_bytes(b"x")
            plex = tmp_path / "afile" / "plex"
        else:
            (tmp_path / "loop-a").symlink_to(tmp_path / "loop-b")
            (tmp_path / "loop-b").symlink_to(tmp_path / "loop-a")
            plex = tmp_path / "loop-a" / "plex"
        storage = _start(tmp_path, monkeypatch, plex_music=plex)
        assert "plex_music" not in storage._delete_roots
        before = _tree(tmp_path)
        _refuse(storage, "optional_root_unverifiable")
        assert _tree(tmp_path) == before


class TestMountBoundary:
    @pytest.mark.parametrize(
        "where",
        ["equal", "dir", "file", "deep_file", "escaped", "deleted", "deleted_equal"],
    )
    def test_mount_identity_at_or_below_target_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str,
    ) -> None:
        target = (tmp_path / "staging").resolve() / KEY
        (tmp_path / "staging" / KEY / "with space\tand tab").mkdir(parents=True)
        point = {
            "equal": str(target),
            "dir": str(target / "sub"),
            "file": str(target / "partial.bin"),
            "deep_file": str(target / "sub" / "nested.bin"),
            "escaped": str(target / "with space\tand tab"),
            "deleted": str(target / "sub") + " (deleted)",
            "deleted_equal": str(target) + " (deleted)",
        }[where]
        storage = _start(
            tmp_path, monkeypatch, mounts=((tmp_path / "staging").resolve(), point)
        )
        before = _tree(tmp_path)
        _refuse(storage, "sub_mount")
        assert _tree(tmp_path) == before

    def test_differently_spelled_mount_point_matches_by_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The mount point text is outside the target; its identity is not."""
        alias = tmp_path / "aliasdir"
        alias.symlink_to(tmp_path / "staging" / KEY, target_is_directory=True)
        storage = _start(
            tmp_path, monkeypatch,
            mounts=((tmp_path / "staging").resolve(), alias / "sub"),
        )
        before = _tree(tmp_path)
        _refuse(storage, "sub_mount")
        assert _tree(tmp_path) == before

    @pytest.mark.parametrize("bound", ["sub", "partial.bin"])
    def test_synthetic_mount_metadata_bind_elsewhere_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str,
    ) -> None:
        """Same-device dir/file bind reported at an unrelated spelling."""
        job = (tmp_path / "staging").resolve() / KEY
        # Unrelated spelling: the leaf does not exist, its parent does.
        point = str((tmp_path / "outside").resolve() / "elsewhere")
        storage = _start(
            tmp_path, monkeypatch, mounts=((tmp_path / "staging").resolve(), point)
        )
        real_lstat = os.lstat

        def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if os.fspath(path) == point:
                return real_lstat(job / bound)
            return real_lstat(path, *args, **kwargs)

        before = _tree(tmp_path)
        with monkeypatch.context() as patch:
            patch.setattr(os, "lstat", lstat)
            _refuse(storage, "sub_mount")
        assert _tree(tmp_path) == before

    def test_mount_point_text_alone_does_not_refuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Synthetic metadata: a point spelled inside the target whose identity
        is an unrelated directory is not a sub-mount of the tree."""
        job = (tmp_path / "staging").resolve() / KEY
        point = str(job / "sub")
        storage = _start(
            tmp_path, monkeypatch, mounts=((tmp_path / "staging").resolve(), point)
        )
        real_lstat = os.lstat
        unrelated = (tmp_path / "outside").resolve()

        def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if os.fspath(path) == point:
                return real_lstat(unrelated)
            return real_lstat(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "lstat", lstat)
            assert storage.remove_staging_for_clear_history(KEY) is True
        assert (tmp_path / "outside" / "secret.bin").read_bytes() == b"outside"

    def test_nested_symlink_to_mounted_directory_not_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def links(staging: Path, library: Path) -> None:
            (staging / KEY / "sub" / "to_mount").symlink_to(
                tmp_path / "outside", target_is_directory=True
            )
            (staging / KEY / "sub" / "to_file").symlink_to(
                tmp_path / "outside" / "secret.bin"
            )

        outside = (tmp_path / "outside").resolve()
        storage = _start(
            tmp_path, monkeypatch, before_start=links,
            mounts=((tmp_path / "staging").resolve(), outside, outside / "secret.bin"),
        )
        before = [_tree(p) for p in _protected(tmp_path)]
        assert storage.remove_staging_for_clear_history(KEY) is True
        assert [_tree(p) for p in _protected(tmp_path)] == before

    @pytest.mark.parametrize("error", ["missing", "denied", "io"])
    def test_unreadable_mount_identity_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, error: str,
    ) -> None:
        outside = (tmp_path / "outside").resolve()
        point = str(tmp_path / "no-such-mount") if error == "missing" else str(outside)
        storage = _start(
            tmp_path, monkeypatch, mounts=((tmp_path / "staging").resolve(), point)
        )
        real_lstat = os.lstat

        def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if os.fspath(path) == point and error != "missing":
                raise PermissionError(13, "denied") if error == "denied" else OSError(5, "io")
            return real_lstat(path, *args, **kwargs)

        before = _tree(tmp_path)
        with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger=LOGGER):
            patch.setattr(os, "lstat", lstat)
            error_obj = _refuse(storage, "mount_unverifiable")
        assert _tree(tmp_path) == before
        assert "mount_unverifiable" in caplog.text
        _assert_path_free(caplog.text + str(error_obj), tmp_path)

    def test_detached_deleted_mount_point_may_be_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = (tmp_path / "staging").resolve()
        storage = _start(
            tmp_path, monkeypatch,
            mounts=(root, str(tmp_path / "gone-mount") + " (deleted)"),
        )
        assert storage.remove_staging_for_clear_history(KEY) is True

    def test_root_and_unrelated_segment_mounts_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = (tmp_path / "staging").resolve()
        unrelated = (
            root / f"{KEY}x", root / f"{KEY} (deleted)x", Path(str(root / KEY)[:-1]),
        )
        for path in unrelated:
            path.mkdir(parents=True)
        storage = _start(
            tmp_path, monkeypatch,
            mounts=(root.parent, root, *unrelated, root / OTHER),
        )
        before = [_tree(p) for p in _protected(tmp_path)]
        assert storage.remove_staging_for_clear_history(KEY) is True
        assert not os.path.lexists(tmp_path / "staging" / KEY)
        assert [_tree(p) for p in _protected(tmp_path)] == before

    def test_mountinfo_unavailable_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        _stub_mountinfo(monkeypatch, FileNotFoundError(2, "no proc"))
        before = _tree(tmp_path)
        _refuse(storage, "mountinfo_unavailable")
        _stub_mountinfo(monkeypatch, PermissionError(13, "denied"))
        _refuse(storage, "mountinfo_unavailable")
        assert _tree(tmp_path) == before

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"\n",
            b"garbage\n",
            b"20 1 0:20 / / rw shared:1 ext4 /dev/x rw\n",  # no separator
            b"20 1 0:20 / / rw - ext4 /dev/x\n",  # too few trailing fields
            b"x 1 0:20 / / rw - ext4 /dev/x rw\n",
            b"20 1 020 / / rw - ext4 /dev/x rw\n",
            b"20 1 0:20 / relative rw - ext4 /dev/x rw\n",
            b"20 1 0:20 / /a\\04 rw - ext4 /dev/x rw\n",
            b"20 1 0:20 / /a\\09x rw - ext4 /dev/x rw\n",
            b"20 1 0:20 / /a\\400 rw - ext4 /dev/x rw\n",
            b"20 1 0:20 / /a\\000b rw - ext4 /dev/x rw\n",
            b"20 1 0:20 / / rw - ext4 /dev/x rw\n\n",
            # Shifted, truncated or padded lines (exact field counts).
            b"20 1 0:20 / rw - ext4 /dev/x rw\n",  # root field missing
            b"20 1 0:20 / / rw - ext4 /dev/x rw extra\n",
            b"20 1 0:20  / rw - ext4 /dev/x rw\n",  # empty field
            b"20 1 0:20 / / rw - ext4  rw\n",  # empty source
            b"20 1 0:20 / / rw bogus - ext4 /dev/x rw\n",  # unknown optional
            b"20 1 0:20 / / relatime - ext4 /dev/x rw\n",  # options shifted
            b"20 1 0:20 / / rw - ext4 /dev/x relatime\n",
            b"20 1 0:20 / rw,relatime / - ext4 /dev/x rw\n",
            b"20 1 0:20 / / rw - /dev/x rw\n",  # fstype missing
            b"20 1 0:20 / / rw - ext4 /dev/x rw\n20 1 0:21 / /b rw - ext4 /dev/y rw\n",
            b"20 1 0:20 / / rw - ext4 /dev/x rw\n21 20 0:21 / /b rw\n",
        ],
    )
    def test_malformed_mountinfo_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        _stub_mountinfo(monkeypatch, raw)
        before = _tree(tmp_path)
        _refuse(storage, "mountinfo_malformed")
        assert _tree(tmp_path) == before

    def test_known_optional_fields_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        outside = _escape(str((tmp_path / "outside").resolve()))
        _stub_mountinfo(monkeypatch, _raw(
            "20 1 8:1 / / rw,relatime shared:1 master:2 propagate_from:3 - ext4 /dev/x rw",
            f"21 20 8:1 / {outside} ro unbindable - xfs /dev/y ro,noatime",
        ))
        assert storage.remove_staging_for_clear_history(KEY) is True

    def test_device_boundary_refused_at_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        job = (tmp_path / "staging").resolve() / KEY
        real_lstat = os.lstat

        def lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            result = real_lstat(path, *args, **kwargs)
            if Path(os.fspath(path)) == job:
                fields = list(result[:10])
                fields[2] = result.st_dev + 1
                return os.stat_result(fields)
            return result

        before = _tree(tmp_path)
        with monkeypatch.context() as patch:
            patch.setattr(os, "lstat", lstat)
            _refuse(storage, "device_boundary")
        assert _tree(tmp_path) == before

    def test_device_boundary_refused_for_nested_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        foreign = (tmp_path / "staging").resolve() / KEY / "sub" / "nested.bin"

        def hook(entry: os.DirEntry[str], follow: bool) -> os.stat_result:
            result = entry.stat(follow_symlinks=follow)
            if Path(entry.path) == foreign:
                fields = list(result[:10])
                fields[2] = result.st_dev + 1
                return os.stat_result(fields)
            return result

        before = _tree(tmp_path)
        with monkeypatch.context() as patch:
            _spy_scandir(patch, hook, [])
            _refuse(storage, "device_boundary")
        assert _tree(tmp_path) == before

    def test_preflight_never_follows_symlinks_even_for_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def links(staging: Path, library: Path) -> None:
            job = staging / KEY / "sub"
            (job / "out_dir").symlink_to(tmp_path / "outside", target_is_directory=True)
            (job / "out_file").symlink_to(tmp_path / "outside" / "secret.bin")
            (job / "lib_dir").symlink_to(library, target_is_directory=True)

        storage = _start(tmp_path, monkeypatch, before_start=links)
        outside = (tmp_path / "outside").resolve()
        library = (tmp_path / "library").resolve()
        job = (tmp_path / "staging").resolve() / KEY
        follows: list[bool] = []
        opened: list[Path] = []
        stat_calls: list[Path] = []

        def hook(entry: os.DirEntry[str], follow: bool) -> os.stat_result:
            follows.append(follow)
            return entry.stat(follow_symlinks=follow)

        real_stat, real_lstat = os.stat, os.lstat

        def record(real: Callable[..., os.stat_result]) -> Callable[..., os.stat_result]:
            def wrapper(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
                if not isinstance(path, int):
                    stat_calls.append(Path(os.fspath(path)))
                return real(path, *args, **kwargs)
            return wrapper

        before = [_tree(p) for p in _protected(tmp_path)]
        with monkeypatch.context() as patch:
            _spy_scandir(patch, hook, opened)
            patch.setattr(os, "stat", record(real_stat))
            patch.setattr(os, "lstat", record(real_lstat))
            assert storage.remove_staging_for_clear_history(KEY) is True
        assert follows and not any(follows)
        assert set(opened) == {job, job / "sub"}
        for raw_path in (*opened, *stat_calls):
            # Static root checks may stat the Library root and its ``..``.
            path = Path(os.path.normpath(raw_path))
            assert not path.is_relative_to(outside)
            assert not path.is_relative_to(library) or path == library
        assert [_tree(p) for p in _protected(tmp_path)] == before


class TestBindSources:
    """Synthetic mount tables mapping canonical paths to bind sources."""

    @staticmethod
    def _paths(tmp_path: Path) -> tuple[Path, Path]:
        return (tmp_path / "staging").resolve(), (tmp_path / "library").resolve()

    @pytest.mark.parametrize(
        "library_source",
        [("8:1", "/srv/x/library"), ("8:2", "/srv/x/staging"), ("9:0", "/")],
    )
    def test_disjoint_sibling_binds_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        library_source: tuple[str, str],
    ) -> None:
        staging, library = self._paths(tmp_path)
        storage = _start(tmp_path, monkeypatch, mounts=(
            (staging, "8:1", "/srv/x/staging"), (library, *library_source),
            (library / "video", "8:1", "/srv/x/other"),
        ))
        before = [_tree(p) for p in _protected(tmp_path)]
        assert storage.remove_staging_for_clear_history(KEY) is True
        assert [_tree(p) for p in _protected(tmp_path)] == before

    @pytest.mark.parametrize(
        "library_root",
        ["/srv/x/staging", f"/srv/x/staging/{KEY}", f"/srv/x/staging/{OTHER}",
         f"/srv/x/staging/{KEY}/sub", "/srv/x", "/"],
    )
    def test_library_bind_source_related_to_staging_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library_root: str,
    ) -> None:
        staging, library = self._paths(tmp_path)
        storage = _start(tmp_path, monkeypatch, mounts=(
            (staging, "8:1", "/srv/x/staging"), (library, "8:1", library_root),
        ))
        before = _tree(tmp_path)
        _refuse(storage, "bind_overlap")
        assert _tree(tmp_path) == before

    @pytest.mark.parametrize("nested_root", [f"/srv/x/staging/{KEY}", "/srv/x", "/"])
    def test_nested_bind_below_library_related_to_staging_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested_root: str,
    ) -> None:
        staging, library = self._paths(tmp_path)
        storage = _start(tmp_path, monkeypatch, mounts=(
            (staging, "8:1", "/srv/x/staging"), (library, "8:1", "/srv/x/library"),
            (library / "video" / "Clip.mp4", "8:1", nested_root),
        ))
        before = _tree(tmp_path)
        _refuse(storage, "bind_overlap")
        assert _tree(tmp_path) == before

    def test_media_chain_through_bind_of_staging_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The alias lives in a bind of staging; the Library root is elsewhere."""
        real = tmp_path / "real-library"
        real.mkdir()
        mnt = tmp_path / "mnt"
        mnt.mkdir()
        (mnt / "alias").symlink_to(real, target_is_directory=True)
        staging, _ = self._paths(tmp_path)
        storage = _start(tmp_path, monkeypatch, library=mnt / "alias", mounts=(
            (staging, "8:1", "/srv/x/staging"),
            (mnt.resolve(), "8:1", f"/srv/x/staging/{KEY}"),
            (real.resolve(), "8:1", "/srv/x/library"),
        ))
        before = _tree(tmp_path)
        _refuse(storage, "bind_overlap")
        assert _tree(tmp_path) == before

    def test_missing_plex_below_bind_of_staging_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plexmnt = tmp_path / "plexmnt"
        plexmnt.mkdir()
        staging, library = self._paths(tmp_path)
        storage = _start(
            tmp_path, monkeypatch, plex_music=plexmnt / "music", mounts=(
                (staging, "8:1", "/srv/x/staging"), (library, "8:1", "/srv/x/library"),
                (plexmnt.resolve(), "8:1", f"/srv/x/staging/{KEY}"),
            ),
        )
        assert "plex_music" not in storage._delete_roots
        before = _tree(tmp_path)
        _refuse(storage, "bind_overlap")
        assert _tree(tmp_path) == before

    @pytest.mark.parametrize(
        "case",
        ["stacked", "shadowed", "orphan", "deleted_ancestor", "relative_root",
         "no_root_mount", "nested_relative_root"],
    )
    def test_ambiguous_mapping_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
    ) -> None:
        staging, library = self._paths(tmp_path)
        s, p, lib = str(staging), str(staging.parent), str(library)
        root = _line(20, 1, "0:1", "/", "/")
        lines = {
            "stacked": [root, _line(21, 20, "8:1", "/a", s), _line(22, 21, "8:1", "/b", s)],
            "shadowed": [root, _line(21, 20, "8:1", "/a", s), _line(22, 20, "8:2", "/", p)],
            "orphan": [root, _line(21, 999, "8:1", "/a", s)],
            "deleted_ancestor": [root, _line(21, 20, "8:1", "/a", p + " (deleted)")],
            "relative_root": [root, _line(21, 20, "8:1", "net:[1]", s)],
            "no_root_mount": [_line(21, 1, "8:1", "/a", s)],
            "nested_relative_root": [
                root, _line(21, 20, "8:1", "net:[1]", lib + "/video"),
            ],
        }[case]
        storage = _start(tmp_path, monkeypatch)
        _stub_mountinfo(monkeypatch, _raw(*lines))
        before = _tree(tmp_path)
        _refuse(storage, "mount_ambiguous")
        assert _tree(tmp_path) == before


class TestDoubleLeadingSlash:
    def test_chain_paths_collapse_double_slash_anchor(self) -> None:
        from app.services.storage_service import _RootStep

        steps = (
            _RootStep("data"),
            _RootStep("lnk", (1, 2), "//data/staging/k"),
            _RootStep("//"), _RootStep("data"), _RootStep("staging"), _RootStep("k"),
        )
        assert StorageService._chain_paths(steps) == [
            Path("/data"), Path("/data/lnk"), Path("/"), Path("/data"),
            Path("/data/staging"), Path("/data/staging/k"),
        ]

    def test_double_slash_bridge_alias_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging = (tmp_path / "staging").resolve()
        real = tmp_path / "real-library"
        real.mkdir()
        (staging / KEY).mkdir(parents=True)
        (staging / KEY / "bridge").symlink_to(real, target_is_directory=True)
        library = tmp_path / "libalias"
        library.symlink_to("/" + str(staging / KEY / "bridge"), target_is_directory=True)
        storage = _start(tmp_path, monkeypatch, staging=staging, library=library)
        assert any(
            step.component == "//" for step in storage._delete_roots["app_library"].steps
        )
        before = _tree(tmp_path)
        _refuse(storage, "alias_through_staging")
        assert _tree(tmp_path) == before

    def test_double_slash_dangling_plex_alias_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging = (tmp_path / "staging").resolve()
        plex = tmp_path / "plexlink"
        plex.symlink_to("/" + str(staging / KEY / "missing" / "plex"))
        storage = _start(tmp_path, monkeypatch, plex_music=plex)
        assert "plex_music" not in storage._delete_roots
        before = _tree(tmp_path)
        _refuse(storage, "alias_through_staging")
        assert _tree(tmp_path) == before

    def test_double_slash_configured_missing_plex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plex = Path("/" + str((tmp_path / "plex" / "music").resolve()))
        storage = _start(tmp_path, monkeypatch, plex_music=plex)
        assert "plex_music" not in storage._delete_roots
        assert storage.remove_staging_for_clear_history(KEY) is True
        (tmp_path / "staging" / KEY).mkdir()
        (tmp_path / "plex" / "music").mkdir(parents=True)
        _refuse(storage, "optional_root_unverifiable")


class TestRemovalFailures:
    @pytest.mark.parametrize(
        "error", [FileNotFoundError(2, "vanished"), PermissionError(13, "denied"),
                  OSError(5, "io")],
    )
    def test_rmtree_error_is_typed_and_never_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)

        def fail(path: Any) -> None:
            raise error

        monkeypatch.setattr(shutil, "rmtree", _fake_rmtree(fail))
        _refuse(storage, "delete_failed")
        assert (tmp_path / "staging" / KEY / "partial.bin").exists()

    def test_partial_removal_then_vanished_sub_entry_is_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        before = [_tree(p) for p in _protected(tmp_path)]

        def partial(path: Any) -> None:
            os.unlink(Path(path) / "partial.bin")
            raise FileNotFoundError(2, "sub entry vanished")

        monkeypatch.setattr(shutil, "rmtree", _fake_rmtree(partial))
        _refuse(storage, "delete_failed")
        assert not (tmp_path / "staging" / KEY / "partial.bin").exists()
        assert (tmp_path / "staging" / KEY / "sub" / "nested.bin").exists()
        assert [_tree(p) for p in _protected(tmp_path)] == before

    def test_rmtree_called_once_without_ignore_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
        real_rmtree = shutil.rmtree

        def spy(path: Any, *args: Any, **kwargs: Any) -> None:
            calls.append((path, args, kwargs))
            real_rmtree(path)

        spy.avoids_symlink_attacks = True  # type: ignore[attr-defined]
        monkeypatch.setattr(shutil, "rmtree", spy)
        assert storage.remove_staging_for_clear_history(KEY) is True
        assert calls == [((tmp_path / "staging").resolve() / KEY, (), {})]

    def test_inspection_error_is_typed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        nested = (tmp_path / "staging").resolve() / KEY / "sub" / "nested.bin"
        sub = nested.parent
        real_scandir = os.scandir

        def hook(entry: os.DirEntry[str], follow: bool) -> os.stat_result:
            if Path(entry.path) == nested:
                raise PermissionError(13, "denied")
            return entry.stat(follow_symlinks=follow)

        def unreadable(path: Any) -> Any:
            if not isinstance(path, int) and Path(os.fspath(path)) == sub:
                raise PermissionError(13, "denied")
            return real_scandir(path)

        before = _tree(tmp_path)
        with monkeypatch.context() as patch:
            _spy_scandir(patch, hook, [])
            _refuse(storage, "inspect_failed")
        with monkeypatch.context() as patch:
            patch.setattr(os, "scandir", unreadable)
            _refuse(storage, "inspect_failed")
        assert _tree(tmp_path) == before


def _identity(path: Path) -> tuple[int, int]:
    info = os.lstat(path)
    return (info.st_dev, info.st_ino)


def _steps(path: Path) -> tuple[Any, ...]:
    from app.services.storage_service import _RootStep

    return tuple(_RootStep(part) for part in path.parts)


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return os.path.exists(directory / "caseprobe")
    finally:
        probe.rmdir()


class TestIdentityOverlap:
    """Overlap decisions by ``(st_dev, st_ino)``, independent of spelling.

    The deterministic fixture records a captured media root under an
    alternate spelling (through an alias of a staging directory) whose text
    is outside staging while the real directory is inside it.
    """

    @staticmethod
    def _alias(tmp_path: Path, name: str, to: Path) -> Path:
        alias = tmp_path / name
        alias.symlink_to(to, target_is_directory=True)
        return alias

    def _forge(
        self, storage: StorageService, storage_type: str, spelled: Path,
        steps: tuple[Any, ...] | None = None,
    ) -> None:
        from app.services.storage_service import _DeleteRoot

        info = os.stat(spelled)  # the directory the spelling designates
        storage._delete_roots[storage_type] = _DeleteRoot(
            spelled, steps if steps is not None else _steps(spelled),
            (info.st_dev, info.st_ino),
        )

    @pytest.mark.parametrize(
        "where", ["inside_job", "inside_other_job", "same", "ancestor"],
    )
    @pytest.mark.parametrize("storage_type", ["app_library", "plex_music"])
    def test_alternate_spelling_overlap_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        where: str, storage_type: str,
    ) -> None:
        storage = _start(tmp_path, monkeypatch, plex_music=tmp_path / "plex-missing")
        staging = (tmp_path / "staging").resolve()
        (staging / KEY / "media").mkdir()
        (staging / OTHER / "media").mkdir()
        alt_staging = self._alias(tmp_path, "alt-staging", staging)
        alt_tmp = self._alias(tmp_path, "alt-tmp", tmp_path.resolve())
        spelled = {
            "inside_job": alt_staging / KEY / "media",
            "inside_other_job": alt_staging / OTHER / "media",
            "same": alt_staging,
            "ancestor": alt_tmp,
        }[where]
        assert not spelled.is_relative_to(staging)
        self._forge(storage, storage_type, spelled)
        before = _tree(tmp_path)
        _refuse(storage, "root_overlap")
        assert _tree(tmp_path) == before

    def test_alternate_spelling_bridge_chain_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.services.storage_service import _RootStep

        storage = _start(tmp_path, monkeypatch)
        staging = (tmp_path / "staging").resolve()
        real = tmp_path / "real-library"
        real.mkdir()
        real = real.resolve()
        bridge = staging / KEY / "bridge"
        bridge.symlink_to(real, target_is_directory=True)
        alt_job = self._alias(tmp_path, "alt-job", staging / KEY)
        steps = (
            *_steps(alt_job)[1:],
            _RootStep("bridge", _identity(bridge), str(real)),
            *_steps(real),
        )
        self._forge(storage, "app_library", real, steps)
        before = _tree(tmp_path)
        _refuse(storage, "alias_through_staging")
        assert _tree(tmp_path) == before

    def test_media_root_identity_inside_job_refused_by_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even if ancestry and mount mapping could not show it, the no-follow
        tree scan matches the media root identity inside the job."""
        storage = _start(tmp_path, monkeypatch)
        media = (tmp_path / "staging").resolve() / KEY / "sub" / "lib"
        media.mkdir()
        real_lineage = StorageService._real_lineage

        def blind(
            self: StorageService, path: Path, reason: str, *, missing_ok: bool = False,
            directory: bool = False,
        ) -> set[tuple[int, int]] | None:
            found = real_lineage(
                self, path, reason, missing_ok=missing_ok, directory=directory
            )
            staging_id = self._staging_snapshot.identity  # type: ignore[union-attr]
            return None if found is None else found - {staging_id}

        self._forge(storage, "app_library", media)
        before = _tree(tmp_path)
        with monkeypatch.context() as patch:
            patch.setattr(StorageService, "_real_lineage", blind)
            patch.setattr(
                StorageService, "_check_bind_sources", lambda *args, **kwargs: None
            )
            _refuse(storage, "root_overlap")
        assert _tree(tmp_path) == before

    def test_changed_media_root_inode_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _start(tmp_path, monkeypatch)
        library = tmp_path / "library"
        library.rename(tmp_path / "library-old")
        library.mkdir()
        before = _tree(tmp_path)
        _refuse(storage, "roots_unverified")
        assert _tree(tmp_path) == before

    def test_alternate_spelling_mount_mapping_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A mount point identity spelled differently from the path we map."""
        alt = self._alias(tmp_path, "alt-parent", tmp_path.resolve())
        storage = _start(
            tmp_path, monkeypatch, mounts=((alt / "staging", "8:1", "/srv/x/staging"),)
        )
        before = _tree(tmp_path)
        _refuse(storage, "mount_ambiguous")
        assert _tree(tmp_path) == before

    def test_case_insensitive_real_nested_media_root_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Real startup capture: Library configured with different letter case."""
        if not _case_insensitive(tmp_path):
            pytest.skip("filesystem is case-sensitive")
        staging = tmp_path / "staging"
        (staging / OTHER / "media").mkdir(parents=True)
        library = tmp_path / "STAGING" / OTHER / "media"
        storage = _start(tmp_path, monkeypatch, staging=staging, library=library)
        assert not storage._delete_roots["app_library"].resolved.is_relative_to(
            staging.resolve()
        )
        before = _tree(tmp_path)
        _refuse(storage, "root_overlap")
        assert _tree(tmp_path) == before


@pytest.mark.skipif(
    sys.platform != "linux" or not os.path.exists("/proc/self/mountinfo"),
    reason="native mountinfo is Linux-only",
)
def test_linux_native_mountinfo_and_real_delete(tmp_path: Path) -> None:
    staging, library = tmp_path / "staging", tmp_path / "library"
    storage = StorageService(staging, library)
    storage.ensure_layout()
    _populate(tmp_path, staging, library)
    (staging / KEY / "sub" / "to_library").symlink_to(library / "video" / "Clip.mp4")
    storage.initialize_delete_roots()
    storage.initialize_staging_root()
    before = [_tree(p) for p in _protected(tmp_path)]
    assert storage.remove_staging_for_clear_history(KEY) is True
    assert not os.path.lexists(staging / KEY)
    assert [_tree(p) for p in _protected(tmp_path)] == before
