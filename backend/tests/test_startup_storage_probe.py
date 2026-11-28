"""M7-StartupProbe: nonfatal startup write check (PRODUCT_SPEC.md §23,
ARCHITECTURE.md §47).

The probe reuses only the startup root captures, writes and removes its own
hidden file descriptor-relative under the pinned root, never touches foreign
entries and reports only fixed, path-free reason tokens. Faults are injected
deterministically; nothing here needs privileges.
"""

import errno
import logging
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
import app.services.storage_service as storage_module
from app.config import Settings
from app.services.storage_service import (
    StartupProbeResult,
    StoragePathError,
    StorageService,
)
from tests.conftest import FakeAnalyzer, build_application

LOGGER = "app.services.storage_service"
PREFIX = ".downloader-probe-"
PAYLOAD = storage_module._PROBE_PAYLOAD
ATTEMPTS = storage_module._PROBE_NAME_ATTEMPTS
REQUIRED_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


def _ok(*targets: str) -> tuple[StartupProbeResult, ...]:
    return tuple(StartupProbeResult(target, True, None) for target in targets)


def _identity(path: Path) -> tuple[int, int]:
    info = os.stat(path)
    return (info.st_dev, info.st_ino)


def _entries(root: Path) -> dict[str, tuple[str, bytes | str]]:
    """No-follow snapshot of the direct entries of ``root``."""
    found: dict[str, tuple[str, bytes | str]] = {}
    for name in os.listdir(root):
        path = root / name
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            found[name] = ("link", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            found[name] = ("dir", ",".join(sorted(os.listdir(path))))
        else:
            found[name] = ("file", path.read_bytes())
    return found


def _assert_path_free(text: str, tmp_path: Path) -> None:
    for raw in {str(tmp_path), str(tmp_path.resolve()), tmp_path.name}:
        assert raw not in text


def _started(
    tmp_path: Path, music: Path | None = None, video: Path | None = None
) -> StorageService:
    storage = StorageService(
        tmp_path / "staging", tmp_path / "library",
        plex_music_root=music, plex_video_root=video,
    )
    storage.ensure_layout()
    storage.initialize_delete_roots()
    storage.initialize_staging_root()
    return storage


@dataclass(slots=True)
class _Created:
    name: str
    flags: int
    mode: int
    fd: int
    dir_identity: tuple[int, int]


class _Spy:
    """Tracks descriptors/creations/unlinks and forbids other mutations."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.active: set[int] = set()
        self.created: list[_Created] = []
        # Open probe descriptors only: fd numbers are reused after close.
        self.live: dict[int, _Created] = {}
        self.unlinked: list[tuple[str, tuple[int, int]]] = []
        self.real_open = os.open
        self.real_close = os.close
        self.real_fstat = os.fstat
        self.real_unlink = os.unlink
        self.real_symlink = os.symlink
        self.real_mkdir = os.mkdir
        self.real_write = os.write

        def tracked_open(
            path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> int:
            fd = self.real_open(path, flags, mode, dir_fd=dir_fd)
            self.active.add(fd)
            if flags & os.O_CREAT:
                assert dir_fd is not None
                info = self.real_fstat(dir_fd)
                created = _Created(str(path), flags, mode, fd, (info.st_dev, info.st_ino))
                self.created.append(created)
                self.live[fd] = created
            return fd

        def tracked_close(fd: int) -> None:
            self.real_close(fd)
            self.active.discard(fd)
            self.live.pop(fd, None)

        def tracked_unlink(path: Any, *, dir_fd: int | None = None) -> None:
            assert dir_fd is not None, "probe unlink must be descriptor-relative"
            info = self.real_fstat(dir_fd)
            self.unlinked.append((str(path), (info.st_dev, info.st_ino)))
            self.real_unlink(path, dir_fd=dir_fd)

        def forbidden(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("startup probe must not perform this operation")

        monkeypatch.setattr(os, "open", tracked_open)
        monkeypatch.setattr(os, "close", tracked_close)
        monkeypatch.setattr(os, "unlink", tracked_unlink)
        for name in (
            "remove", "rmdir", "rename", "replace", "mkdir", "makedirs",
            "symlink", "link", "chmod",
        ):
            monkeypatch.setattr(os, name, forbidden)
        monkeypatch.setattr(shutil, "rmtree", forbidden)
        monkeypatch.setattr(Path, "resolve", forbidden)
        monkeypatch.setattr(Path, "mkdir", forbidden)

    def probe_fds(self) -> set[int]:
        return set(self.live)

    def by_fd(self, fd: int) -> _Created:
        return self.live[fd]


def _patch_write(
    monkeypatch: pytest.MonkeyPatch, spy: _Spy,
    action: Callable[[int, bytes], int],
) -> None:
    """Replace ``os.write`` for probe descriptors only."""

    def write(fd: int, data: bytes) -> int:
        if fd in spy.probe_fds():
            return action(fd, data)
        return spy.real_write(fd, data)

    monkeypatch.setattr(os, "write", write)


class TestProbeCoverage:
    def test_every_configured_root_written_and_cleaned_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        music, video = tmp_path / "plex-music", tmp_path / "plex-video"
        music.mkdir()
        video.mkdir()
        storage = _started(tmp_path, music, video)
        roots = [tmp_path / "staging", tmp_path / "library", music, video]
        before = [_entries(root) for root in roots]

        spy = _Spy(monkeypatch)
        written: dict[str, bytes] = {}
        private: dict[str, bool] = {}

        def record(fd: int, data: bytes) -> int:
            name = spy.by_fd(fd).name
            private[name] = stat.S_IMODE(spy.real_fstat(fd).st_mode) & 0o077 == 0
            count = spy.real_write(fd, data)
            written[name] = written.get(name, b"") + data[:count]
            return count

        _patch_write(monkeypatch, spy, record)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            results = storage.probe_startup_writability()

        assert results == _ok("staging", "app_library", "plex_music", "plex_video")
        assert [c.dir_identity for c in spy.created] == [_identity(r) for r in roots]
        names = [c.name for c in spy.created]
        assert len(set(names)) == len(names)
        for created in spy.created:
            assert created.name.startswith(PREFIX)
            token = created.name[len(PREFIX):]
            assert len(token) == 32 and all(ch in "0123456789abcdef" for ch in token)
            assert created.flags & REQUIRED_FLAGS == REQUIRED_FLAGS
            assert created.mode == 0o600
            assert written[created.name] == PAYLOAD != b""
            assert private[created.name]
        assert spy.unlinked == [(c.name, c.dir_identity) for c in spy.created]
        assert not spy.active
        assert caplog.records == []  # success is silent
        monkeypatch.undo()
        assert [_entries(root) for root in roots] == before

    def test_names_are_fresh_per_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _started(tmp_path)
        spy = _Spy(monkeypatch)
        assert storage.probe_startup_writability() == _ok("staging", "app_library")
        assert storage.probe_startup_writability() == _ok("staging", "app_library")
        names = [c.name for c in spy.created]
        assert len(names) == 4 and len(set(names)) == 4

    def test_unconfigured_optional_roots_are_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = _started(tmp_path)
        spy = _Spy(monkeypatch)
        assert storage.probe_startup_writability() == _ok("staging", "app_library")
        assert len(spy.created) == 2

    @pytest.mark.parametrize("kind", ["missing", "regular_file", "filesystem_root"])
    def test_configured_missing_or_unsafe_optional_root_fails_without_mkdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, kind: str,
    ) -> None:
        music = tmp_path / "plex-music"
        if kind == "regular_file":
            music.write_bytes(b"not a directory")
        elif kind == "filesystem_root":
            music = Path("/")
        storage = _started(tmp_path, music=music)
        assert "plex_music" not in storage._delete_roots

        spy = _Spy(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            results = storage.probe_startup_writability()
        assert results == (
            *_ok("staging", "app_library"),
            StartupProbeResult("plex_music", False, "root_unavailable"),
        )
        assert [c.dir_identity for c in spy.created] == [
            _identity(tmp_path / "staging"), _identity(tmp_path / "library"),
        ]
        assert not spy.active
        monkeypatch.undo()
        if kind == "missing":
            assert not music.exists()
        elif kind == "regular_file":
            assert music.read_bytes() == b"not a directory"
        assert "plex_music: root_unavailable" in caplog.text
        _assert_path_free(caplog.text + repr(results), tmp_path)


class TestRootValidation:
    @pytest.mark.parametrize("target", ["staging", "app_library", "plex_video"])
    def test_replaced_root_is_denied_and_never_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, target: str,
    ) -> None:
        video = tmp_path / "plex-video"
        video.mkdir()
        storage = _started(tmp_path, video=video)
        root = {
            "staging": tmp_path / "staging",
            "app_library": tmp_path / "library",
            "plex_video": video,
        }[target]
        old = tmp_path / f"{root.name}-old"
        root.rename(old)
        root.mkdir()
        (root / "keep.bin").write_bytes(b"keep")
        before = (_entries(root), _entries(old))

        spy = _Spy(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            results = storage.probe_startup_writability()
        expected = {r.target: r for r in _ok("staging", "app_library", "plex_video")}
        expected[target] = StartupProbeResult(target, False, "root_changed")
        assert results == tuple(expected.values())
        forbidden_dirs = {_identity(root), _identity(old)}
        assert not any(c.dir_identity in forbidden_dirs for c in spy.created)
        assert len(spy.created) == 2
        assert not spy.active
        monkeypatch.undo()
        assert (_entries(root), _entries(old)) == before
        _assert_path_free(caplog.text + repr(results), tmp_path)

    def test_vanished_root_reports_unavailable_without_recreating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        music = tmp_path / "plex-music"
        music.mkdir()
        storage = _started(tmp_path, music=music)
        music.rename(tmp_path / "moved")

        spy = _Spy(monkeypatch)
        results = storage.probe_startup_writability()
        assert results[-1] == StartupProbeResult("plex_music", False, "root_unavailable")
        assert results[:-1] == _ok("staging", "app_library")
        assert not spy.active
        monkeypatch.undo()
        assert not music.exists()
        assert _entries(tmp_path / "moved") == {}

    def test_missing_staging_snapshot_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        storage.ensure_layout()
        storage.initialize_delete_roots()  # staging snapshot never captured
        spy = _Spy(monkeypatch)
        results = storage.probe_startup_writability()
        assert results == (
            StartupProbeResult("staging", False, "root_unavailable"),
            *_ok("app_library"),
        )
        assert [c.dir_identity for c in spy.created] == [_identity(tmp_path / "library")]


class TestSyscallFailures:
    def test_create_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        storage = _started(tmp_path)
        roots = [tmp_path / "staging", tmp_path / "library"]
        before = [_entries(r) for r in roots]
        spy = _Spy(monkeypatch)
        tracked = os.open

        def refuse(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if flags & os.O_CREAT:
                raise PermissionError(errno.EACCES, "denied", str(path))
            return tracked(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", refuse)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            results = storage.probe_startup_writability()
        assert results == (
            StartupProbeResult("staging", False, "create_failed"),
            StartupProbeResult("app_library", False, "create_failed"),
        )
        assert spy.unlinked == [] and not spy.active
        monkeypatch.undo()
        assert [_entries(r) for r in roots] == before
        assert "denied" not in caplog.text and "PermissionError" not in caplog.text
        _assert_path_free(caplog.text + repr(results), tmp_path)

    @pytest.mark.parametrize("fault", ["oserror", "short_write", "close_error"])
    def test_write_failure_still_removes_own_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
    ) -> None:
        storage = _started(tmp_path)
        roots = [tmp_path / "staging", tmp_path / "library"]
        before = [_entries(r) for r in roots]
        spy = _Spy(monkeypatch)
        if fault == "oserror":
            def fail(fd: int, data: bytes) -> int:
                raise OSError(errno.EIO, "io error")
            _patch_write(monkeypatch, spy, fail)
        elif fault == "short_write":
            _patch_write(
                monkeypatch, spy, lambda fd, data: spy.real_write(fd, data[:1])
            )
        else:
            tracked_close = os.close

            def close(fd: int) -> None:
                probe = fd in spy.probe_fds()
                tracked_close(fd)
                if probe:
                    raise OSError(errno.EIO, "deferred io error")

            monkeypatch.setattr(os, "close", close)

        results = storage.probe_startup_writability()
        assert results == (
            StartupProbeResult("staging", False, "write_failed"),
            StartupProbeResult("app_library", False, "write_failed"),
        )
        assert spy.unlinked == [(c.name, c.dir_identity) for c in spy.created]
        assert not spy.active
        monkeypatch.undo()
        assert [_entries(r) for r in roots] == before

    @pytest.mark.parametrize("fault", ["unlink_error", "stat_error", "fstat_error"])
    def test_cleanup_failure_is_reported_and_nothing_else_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
    ) -> None:
        storage = _started(tmp_path)
        roots = [tmp_path / "staging", tmp_path / "library"]
        before = [_entries(r) for r in roots]
        spy = _Spy(monkeypatch)
        if fault == "unlink_error":
            def refuse_unlink(path: Any, *, dir_fd: int | None = None) -> None:
                raise PermissionError(errno.EACCES, "denied")
            monkeypatch.setattr(os, "unlink", refuse_unlink)
        elif fault == "stat_error":
            real_stat = os.stat

            def failing_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
                if str(path).startswith(PREFIX):
                    raise OSError(errno.EIO, "io error")
                return real_stat(path, *args, **kwargs)
            monkeypatch.setattr(os, "stat", failing_stat)
        else:
            def failing_fstat(fd: int) -> os.stat_result:
                if fd in spy.probe_fds():
                    raise OSError(errno.EIO, "io error")
                return spy.real_fstat(fd)
            monkeypatch.setattr(os, "fstat", failing_fstat)

        results = storage.probe_startup_writability()
        assert results == (
            StartupProbeResult("staging", False, "cleanup_failed"),
            StartupProbeResult("app_library", False, "cleanup_failed"),
        )
        assert spy.unlinked == []
        assert not spy.active
        monkeypatch.undo()
        content = b"" if fault == "fstat_error" else PAYLOAD
        for root, old, created in zip(roots, before, spy.created, strict=True):
            # Only our own reported probe remains; every other entry is intact.
            assert _entries(root) == {**old, created.name: ("file", content)}

    @pytest.mark.parametrize("kind", ["symlink", "regular", "directory", "vanished"])
    def test_replaced_or_vanished_probe_is_never_unlinked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture, kind: str,
    ) -> None:
        storage = _started(tmp_path)
        staging, library = tmp_path / "staging", tmp_path / "library"
        sentinel = tmp_path / "sentinel.txt"
        sentinel.write_bytes(b"sentinel content")
        before_library = _entries(library)
        spy = _Spy(monkeypatch)
        staging_identity = _identity(staging)

        def swap(fd: int, data: bytes) -> int:
            count = spy.real_write(fd, data)
            created = spy.by_fd(fd)
            if created.dir_identity == staging_identity:
                target = staging / created.name
                spy.real_unlink(target)
                if kind == "symlink":
                    spy.real_symlink(sentinel, target)
                elif kind == "regular":
                    target.write_bytes(b"foreign content")
                elif kind == "directory":
                    spy.real_mkdir(target)
                    (target / "child.bin").write_bytes(b"child")
            return count

        _patch_write(monkeypatch, spy, swap)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            results = storage.probe_startup_writability()
        assert results == (
            StartupProbeResult("staging", False, "cleanup_unverified"),
            *_ok("app_library"),
        )
        staging_probe, library_probe = spy.created
        assert spy.unlinked == [(library_probe.name, library_probe.dir_identity)]
        assert not spy.active
        monkeypatch.undo()
        found = _entries(staging)
        replacement: dict[str, tuple[str, bytes | str]] = {
            "symlink": ("link", str(sentinel)),
            "regular": ("file", b"foreign content"),
            "directory": ("dir", "child.bin"),
        }
        expected = (
            {} if kind == "vanished" else {staging_probe.name: replacement[kind]}
        )
        assert found == expected
        if kind == "directory":
            assert (staging / staging_probe.name / "child.bin").read_bytes() == b"child"
        assert sentinel.read_bytes() == b"sentinel content"
        assert _entries(library) == before_library
        assert "staging: cleanup_unverified" in caplog.text
        _assert_path_free(caplog.text + repr(results), tmp_path)

    @pytest.mark.parametrize("existing", ["symlink", "regular"])
    @pytest.mark.parametrize("mode", ["bounded", "fresh_name"])
    def test_name_collision_preserves_existing_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        existing: str, mode: str,
    ) -> None:
        storage = _started(tmp_path)
        staging, library = tmp_path / "staging", tmp_path / "library"
        sentinel = tmp_path / "sentinel.txt"
        sentinel.write_bytes(b"sentinel content")
        taken = "a" * 32
        occupied = staging / f"{PREFIX}{taken}"
        if existing == "symlink":
            occupied.symlink_to(sentinel)
        else:
            occupied.write_bytes(b"existing content")
        before = (_entries(staging), _entries(library))

        tokens = iter(
            [taken] * ATTEMPTS + ["b" * 32] if mode == "bounded"
            else [taken, "c" * 32, "d" * 32]
        )
        monkeypatch.setattr(
            storage_module.secrets, "token_hex", lambda nbytes=None: next(tokens)
        )
        spy = _Spy(monkeypatch)
        tracked = os.open
        attempts: list[str] = []

        def counting(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if flags & os.O_CREAT:
                attempts.append(str(path))
            return tracked(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", counting)
        results = storage.probe_startup_writability()
        if mode == "bounded":
            assert results == (
                StartupProbeResult("staging", False, "name_collision"),
                *_ok("app_library"),
            )
            assert attempts == [occupied.name] * ATTEMPTS + [f"{PREFIX}{'b' * 32}"]
        else:
            assert results == _ok("staging", "app_library")
            assert attempts == [occupied.name, f"{PREFIX}{'c' * 32}", f"{PREFIX}{'d' * 32}"]
        assert occupied.name not in [name for name, _ in spy.unlinked]
        assert not spy.active
        monkeypatch.undo()
        assert (_entries(staging), _entries(library)) == before
        assert sentinel.read_bytes() == b"sentinel content"

    def test_unexpected_error_is_contained_per_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        storage = _started(tmp_path)
        roots = [tmp_path / "staging", tmp_path / "library"]
        before = [_entries(r) for r in roots]
        spy = _Spy(monkeypatch)
        staging_identity = _identity(tmp_path / "staging")

        def boom(fd: int, data: bytes) -> int:
            if spy.by_fd(fd).dir_identity == staging_identity:
                raise RuntimeError(f"boom {tmp_path}")
            return spy.real_write(fd, data)

        _patch_write(monkeypatch, spy, boom)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            results = storage.probe_startup_writability()
        assert results == (
            StartupProbeResult("staging", False, "probe_failed"),
            *_ok("app_library"),
        )
        # The own, ownership-verified probe is still removed on the error.
        assert spy.unlinked == [(c.name, c.dir_identity) for c in spy.created]
        assert not spy.active
        monkeypatch.undo()
        assert [_entries(r) for r in roots] == before
        assert "boom" not in caplog.text and "RuntimeError" not in caplog.text
        _assert_path_free(caplog.text + repr(results), tmp_path)

    @pytest.mark.parametrize(
        ("fault", "reason"),
        [
            ("oserror", "write_failed"),
            ("short_write", "write_failed"),
            ("runtime_error", "probe_failed"),
        ],
    )
    def test_cleanup_failure_does_not_mask_write_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        fault: str, reason: str,
    ) -> None:
        storage = _started(tmp_path)
        roots = [tmp_path / "staging", tmp_path / "library"]
        before = [_entries(r) for r in roots]
        spy = _Spy(monkeypatch)
        attempts: list[str] = []

        def refuse_unlink(path: Any, *, dir_fd: int | None = None) -> None:
            attempts.append(str(path))
            raise PermissionError(errno.EACCES, "denied")

        def fail(fd: int, data: bytes) -> int:
            if fault == "oserror":
                raise OSError(errno.EIO, "io error")
            if fault == "runtime_error":
                raise RuntimeError("unexpected")
            return spy.real_write(fd, data[:1])

        _patch_write(monkeypatch, spy, fail)
        monkeypatch.setattr(os, "unlink", refuse_unlink)
        results = storage.probe_startup_writability()
        assert results == (
            StartupProbeResult("staging", False, reason),
            StartupProbeResult("app_library", False, reason),
        )
        # Cleanup was attempted on our own verified probe only.
        assert attempts == [c.name for c in spy.created]
        assert not spy.active
        monkeypatch.undo()
        content = b"d" if fault == "short_write" else b""
        for root, old, created in zip(roots, before, spy.created, strict=True):
            assert _entries(root) == {**old, created.name: ("file", content)}


class TestStartupWiring:
    def test_probe_runs_after_mandatory_captures_before_migrations(
        self, settings: Settings, analyzer: FakeAnalyzer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        order: list[str] = []
        real_delete = StorageService.initialize_delete_roots
        real_staging = StorageService.initialize_staging_root
        real_probe = StorageService.probe_startup_writability
        real_migrations = app_main.run_migrations

        def delete_roots(self: StorageService) -> None:
            order.append("delete_roots")
            real_delete(self)

        def staging_root(self: StorageService) -> None:
            order.append("staging_root")
            real_staging(self)

        def probe(self: StorageService) -> tuple[StartupProbeResult, ...]:
            order.append("probe")
            return real_probe(self)

        def migrations(url: str) -> None:
            order.append("migrations")
            real_migrations(url)

        monkeypatch.setattr(StorageService, "initialize_delete_roots", delete_roots)
        monkeypatch.setattr(StorageService, "initialize_staging_root", staging_root)
        monkeypatch.setattr(StorageService, "probe_startup_writability", probe)
        monkeypatch.setattr(app_main, "run_migrations", migrations)

        application = build_application(settings, analyzer)
        with TestClient(application) as client:
            assert client.get("/api/v1/health").status_code == 200
            assert order == ["delete_roots", "staging_root", "probe", "migrations"]
            assert application.state.startup_storage_probe == _ok(
                "staging", "app_library"
            )
        assert _entries(settings.staging_dir) == {}

    def test_missing_optional_root_not_created_and_next_startup_reprobes(
        self, settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path,
    ) -> None:
        music = tmp_path / "plex-music"
        overrides = {"plex_music_dir": music}
        first_app = build_application(settings, analyzer, overrides)
        with TestClient(first_app) as client:
            assert client.get("/api/v1/health").status_code == 200
            assert first_app.state.startup_storage_probe == (
                *_ok("staging", "app_library"),
                StartupProbeResult("plex_music", False, "root_unavailable"),
            )
        assert not music.exists()

        music.mkdir()
        second_app = build_application(settings, analyzer, overrides)
        with TestClient(second_app) as client:
            assert client.get("/api/v1/health").status_code == 200
            assert second_app.state.startup_storage_probe == _ok(
                "staging", "app_library", "plex_music"
            )
        assert _entries(music) == {}

    def test_probe_failure_is_nonfatal(
        self, settings: Settings, analyzer: FakeAnalyzer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def broken(root_fd: int) -> str | None:
            raise RuntimeError("probe bug")

        monkeypatch.setattr(StorageService, "_write_probe", staticmethod(broken))
        application = build_application(settings, analyzer)
        with TestClient(application) as client:
            assert client.get("/api/v1/health").status_code == 200
            assert application.state.startup_storage_probe == (
                StartupProbeResult("staging", False, "probe_failed"),
                StartupProbeResult("app_library", False, "probe_failed"),
            )
        assert (settings.state_dir / "downloader.db").is_file()

    @pytest.mark.parametrize("failing", ["staging", "library"])
    def test_mandatory_guard_failure_is_not_masked(
        self, settings: Settings, analyzer: FakeAnalyzer,
        monkeypatch: pytest.MonkeyPatch, failing: str,
    ) -> None:
        failing_root = settings.staging_dir if failing == "staging" else settings.library_dir
        real_capture = StorageService._capture_root_descriptor
        probes: list[str] = []
        migrations: list[str] = []

        def capture(self: StorageService, root: Path, resolved: Path) -> Any:
            if root == failing_root:
                raise StoragePathError("simulated capture failure")
            return real_capture(self, root, resolved)

        def probe(self: StorageService) -> tuple[StartupProbeResult, ...]:
            probes.append("probe")
            return ()

        monkeypatch.setattr(StorageService, "_capture_root_descriptor", capture)
        monkeypatch.setattr(StorageService, "probe_startup_writability", probe)
        monkeypatch.setattr(app_main, "run_migrations", migrations.append)

        application = build_application(settings, analyzer)
        application.state.startup_storage_probe = _ok("stale")
        with pytest.raises(StoragePathError):
            with TestClient(application):
                pass
        assert probes == [] and migrations == []
        assert application.state.startup_storage_probe is None
