"""M6-Q3-Storage first step: startup staging-root snapshot and the read-only
Q3 check (ARCHITECTURE.md §59 Q3 storage boundary, PRODUCT_SPEC.md §17).

No deletion exists yet: these tests only prove the startup capture, the
per-call identity comparison, fail-closed rejection, path-free logging, no
descriptor leak and that neither protected tree is mutated.
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.config import Settings
from app.services.storage_service import (
    StagingRootVerificationError,
    StoragePathError,
    StorageService,
    StorageTargetError,
)
from tests.conftest import FakeAnalyzer, build_application

LOGGER = "app.services.storage_service"


def _populate(staging: Path, library: Path) -> None:
    (staging / "jobkey").mkdir(parents=True, exist_ok=True)
    (staging / "jobkey" / "partial.bin").write_bytes(b"partial")
    (library / "video").mkdir(parents=True, exist_ok=True)
    (library / "video" / "Clip.mp4").write_bytes(b"media")
    # A symlink inside staging to a Library file: never followed or changed.
    (staging / "jobkey" / "link.mp4").symlink_to(library / "video" / "Clip.mp4")


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


def _guard(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """Forbid mutation primitives and re-resolution; track open descriptors."""
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Q3 staging root check must not mutate")

    def forbidden_resolve(*args: Any, **kwargs: Any) -> Path:
        raise AssertionError("check must not re-resolve startup paths")

    for name in (
        "unlink", "remove", "rmdir", "rename", "replace", "mkdir",
        "makedirs", "symlink", "link", "chmod",
    ):
        monkeypatch.setattr(os, name, forbidden)
    monkeypatch.setattr(shutil, "rmtree", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden_resolve)

    active: set[int] = set()
    real_open, real_close = os.open, os.close

    def tracked_open(*args: Any, **kwargs: Any) -> int:
        fd = real_open(*args, **kwargs)
        active.add(fd)
        return fd

    def tracked_close(fd: int) -> None:
        real_close(fd)
        active.discard(fd)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    return active


def _reject(storage: StorageService, reason: str) -> StagingRootVerificationError:
    with pytest.raises(StagingRootVerificationError) as excinfo:
        storage.verify_staging_root_for_clear_history()
    assert excinfo.value.reason == reason
    return excinfo.value


def _assert_path_free(text: str, tmp_path: Path) -> None:
    for raw in {str(tmp_path), str(tmp_path.resolve()), tmp_path.name}:
        assert raw not in text


class TestStartupCapture:
    def test_lifespan_captures_staging_root_before_migrations(
        self, settings: Settings, analyzer: FakeAnalyzer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        order: list[str] = []
        captured: list[StorageService] = []
        real_delete = StorageService.initialize_delete_roots
        real_staging = StorageService.initialize_staging_root
        real_migrations = app_main.run_migrations

        def delete_roots(self: StorageService) -> None:
            order.append("delete_roots")
            real_delete(self)

        def staging_root(self: StorageService) -> None:
            order.append("staging_root")
            captured.append(self)
            real_staging(self)

        def migrations(url: str) -> None:
            order.append("migrations")
            real_migrations(url)

        monkeypatch.setattr(StorageService, "initialize_delete_roots", delete_roots)
        monkeypatch.setattr(StorageService, "initialize_staging_root", staging_root)
        monkeypatch.setattr(app_main, "run_migrations", migrations)

        with TestClient(build_application(settings, analyzer)) as client:
            assert client.get("/api/v1/health").status_code == 200
            assert order == ["delete_roots", "staging_root", "migrations"]
            (storage,) = captured
            snapshot = storage._staging_snapshot
            assert snapshot is not None
            root_stat = settings.staging_dir.stat()
            assert snapshot.identity == (root_stat.st_dev, root_stat.st_ino)
            assert snapshot.resolved == settings.staging_dir.resolve()
            # Existing Library delete-root capture is unchanged.
            assert set(storage._delete_roots) == {"app_library"}
            storage.verify_staging_root_for_clear_history()

    def test_lifespan_fails_closed_when_staging_capture_fails(
        self, settings: Settings, analyzer: FakeAnalyzer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        migrations: list[str] = []
        real_capture = StorageService._capture_root_descriptor

        def capture(
            self: StorageService, root: Path, resolved: Path
        ) -> Any:
            if root == settings.staging_dir:
                raise StoragePathError("simulated staging capture failure")
            return real_capture(self, root, resolved)

        monkeypatch.setattr(StorageService, "_capture_root_descriptor", capture)
        monkeypatch.setattr(app_main, "run_migrations", migrations.append)

        with pytest.raises(StoragePathError):
            with TestClient(build_application(settings, analyzer)):
                pass
        assert migrations == []
        assert not (settings.state_dir / "downloader.db").exists()

    def test_capture_is_mandatory_and_leaves_no_snapshot_on_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        storage = StorageService(tmp_path / "staging", tmp_path / "library")
        with pytest.raises(StorageTargetError):  # staging root missing
            storage.initialize_staging_root()
        assert storage._staging_snapshot is None
        assert not (tmp_path / "staging").exists()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _reject(storage, "snapshot_missing")

        with pytest.raises(StoragePathError):
            StorageService(Path("/"), tmp_path / "library").initialize_staging_root()


class TestClearHistoryRootCheck:
    @staticmethod
    def _started(tmp_path: Path, staging: Path) -> StorageService:
        library = tmp_path / "library"
        storage = StorageService(staging, library)
        storage.ensure_layout()
        _populate(staging, library)
        storage.initialize_delete_roots()
        storage.initialize_staging_root()
        return storage

    def test_same_root_passes_every_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging = tmp_path / "staging"
        storage = self._started(tmp_path, staging)
        before = (_tree(staging), _tree(tmp_path / "library"))

        active = _guard(monkeypatch)
        for _ in range(3):
            storage.verify_staging_root_for_clear_history()
            assert not active
        monkeypatch.undo()
        assert (_tree(staging), _tree(tmp_path / "library")) == before

    def test_snapshot_missing_rejected_without_mutation(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging, library = tmp_path / "staging", tmp_path / "library"
        storage = StorageService(staging, library)  # never initialized
        storage.ensure_layout()
        _populate(staging, library)
        before = (_tree(staging), _tree(library))

        active = _guard(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            error = _reject(storage, "snapshot_missing")
        assert not active
        monkeypatch.undo()
        assert (_tree(staging), _tree(library)) == before
        assert "snapshot_missing" in caplog.text
        _assert_path_free(caplog.text + str(error), tmp_path)

    def test_replaced_staging_inode_rejected_before_any_operation(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        staging = tmp_path / "staging"
        storage = self._started(tmp_path, staging)
        # Same path, different directory inode (startup root moved aside).
        staging.rename(tmp_path / "staging-old")
        staging.mkdir()
        (staging / "other").mkdir()
        (staging / "other" / "keep.bin").write_bytes(b"keep")
        trees = [staging, tmp_path / "staging-old", tmp_path / "library"]
        before = [_tree(t) for t in trees]

        active = _guard(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            error = _reject(storage, "identity_changed")
        assert not active
        monkeypatch.undo()
        assert [_tree(t) for t in trees] == before
        assert "identity_changed" in caplog.text
        _assert_path_free(caplog.text + str(error), tmp_path)

    @pytest.mark.parametrize("change", ["same_target", "retargeted"])
    @pytest.mark.parametrize("alias_target", ["absolute", "relative"])
    def test_startup_alias_change_rejected(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch, change: str, alias_target: str,
    ) -> None:
        physical = tmp_path / "physical"
        physical.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(
            physical if alias_target == "absolute" else Path("physical"),
            target_is_directory=True,
        )
        staging = alias / "staging"
        storage = self._started(tmp_path, staging)
        storage.verify_staging_root_for_clear_history()  # startup alias replays

        alias.rename(tmp_path / "saved-alias")
        if change == "same_target":
            # Even a new alias reaching the SAME root inode is not trusted.
            alias.symlink_to(physical, target_is_directory=True)
        else:
            other = tmp_path / "other"
            (other / "staging").mkdir(parents=True)
            alias.symlink_to(other, target_is_directory=True)
        trees = [physical / "staging", tmp_path / "library"]
        before = [_tree(t) for t in trees]

        active = _guard(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            error = _reject(storage, "replay_failed")
        assert not active
        monkeypatch.undo()
        assert [_tree(t) for t in trees] == before
        assert "replay_failed" in caplog.text
        _assert_path_free(caplog.text + str(error), tmp_path)
