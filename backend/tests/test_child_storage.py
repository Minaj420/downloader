import asyncio
import errno
import os
import shutil
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from app.services.storage_service import (
    MediaFileMissingError,
    MediaFileNotRegularError,
    MediaFileTargetChangedError,
    PlacementCancelled,
    StoragePathError,
    StorageService,
)


@pytest.fixture
def storage(tmp_path: Path) -> StorageService:
    service = StorageService(tmp_path / "staging", tmp_path / "library")
    service.ensure_layout()
    service.initialize_delete_roots()
    return service


def place(storage: StorageService, source: Path, key: str = "child", **kwargs: Any) -> Any:
    return asyncio.run(storage.place_child_final(
        key, source, media_kind="video", title="Title", **kwargs,
    ))


def output(storage: StorageService) -> Path:
    source = storage.job_staging_dir("child") / "output.mp4"
    source.write_bytes(b"child content")
    return source


def assert_no_temp(storage: StorageService) -> None:
    assert not list((storage.library_root / "video").glob("*.part"))


def test_child_final_file_has_0644_permissions(storage: StorageService) -> None:
    result = place(storage, output(storage))
    assert stat.S_IMODE(result.final_path.stat().st_mode) == 0o644


def test_valid_nested_output_and_catalog_identity(storage: StorageService) -> None:
    source = storage.job_staging_dir("child") / "nested" / "output.mp4"
    source.parent.mkdir()
    source.write_bytes(b"child content")
    result = place(storage, source)
    assert result.final_path.read_bytes() == b"child content"
    assert result.relative_path == "video/Title.mp4"
    assert result.size_bytes == 13
    assert not result.skipped_existing
    assert not source.exists()
    assert storage.verify_media_file(
        "app_library", result.relative_path, result.identity,
    ) == result.final_path
    assert_no_temp(storage)


@pytest.mark.parametrize("key", ["", ".", "..", "../child", "a/b", "a\\b", "/child"])
def test_invalid_keys_do_not_mutate(storage: StorageService, key: str) -> None:
    source = output(storage)
    with pytest.raises(ValueError):
        place(storage, source, key)
    assert source.read_bytes() == b"child content"
    assert not list((storage.library_root / "video").iterdir())


@pytest.mark.parametrize("kind", ["outside", "sibling", "traversal", "root"])
def test_unowned_paths_do_not_mutate(
    storage: StorageService, tmp_path: Path, kind: str,
) -> None:
    owned = output(storage)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    sibling = storage.job_staging_dir("sibling") / "output.mp4"
    sibling.write_bytes(b"sibling")
    source = {
        "outside": outside,
        "sibling": sibling,
        "traversal": owned.parent / ".." / "sibling" / "output.mp4",
        "root": owned.parent,
    }[kind]
    with pytest.raises(StoragePathError):
        place(storage, source)
    assert outside.read_bytes() == b"outside"
    assert sibling.read_bytes() == b"sibling"
    assert owned.read_bytes() == b"child content"
    assert not list((storage.library_root / "video").iterdir())


@pytest.mark.parametrize("kind", ["leaf", "parent", "child"])
def test_symlinks_do_not_mutate(
    storage: StorageService, tmp_path: Path, kind: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "output.mp4").write_bytes(b"outside")
    child = storage.job_staging_dir("child")
    if kind == "child":
        child.rmdir()
        child.symlink_to(outside, target_is_directory=True)
        source = child / "output.mp4"
    elif kind == "parent":
        (child / "nested").symlink_to(outside, target_is_directory=True)
        source = child / "nested" / "output.mp4"
    else:
        source = child / "output.mp4"
        source.symlink_to(outside / "output.mp4")
    with pytest.raises(StoragePathError):
        place(storage, source)
    assert (outside / "output.mp4").read_bytes() == b"outside"
    assert not list((storage.library_root / "video").iterdir())


@pytest.mark.parametrize("kind", ["directory", "fifo", "missing"])
def test_nonregular_or_missing_rejected(
    storage: StorageService, kind: str,
) -> None:
    source = storage.job_staging_dir("child") / "output.mp4"
    if kind == "directory":
        source.mkdir()
    elif kind == "fifo":
        os.mkfifo(source)
    with pytest.raises(
        MediaFileMissingError if kind == "missing" else MediaFileNotRegularError
    ):
        place(storage, source)
    assert source.exists() == (kind != "missing")
    assert not list((storage.library_root / "video").iterdir())


@pytest.mark.parametrize("swap", ["parent", "leaf", "replacement"])
def test_swap_after_pin_never_reads_outside_or_unlinks_replacement(
    storage: StorageService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str,
) -> None:
    source = output(storage)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / source.name).write_bytes(b"outside secret")
    real_copy = shutil.copyfileobj
    copied_bytes: list[bytes] = []

    def swapped_copy(original: Any, copied: Any) -> None:
        if swap == "parent":
            source.parent.rename(tmp_path / "moved")
            source.parent.symlink_to(outside, target_is_directory=True)
        else:
            source.rename(source.with_suffix(".saved"))
            if swap == "leaf":
                source.symlink_to(outside / source.name)
            else:
                source.write_bytes(b"replacement")
        copied_bytes.append(original.read())
        original.seek(0)
        real_copy(original, copied)

    monkeypatch.setattr(shutil, "copyfileobj", swapped_copy)
    if swap == "parent":
        result = place(storage, source)
        assert result.final_path.read_bytes() == b"child content"
        assert not (tmp_path / "moved" / source.name).exists()
    else:
        with pytest.raises(
            MediaFileNotRegularError if swap == "leaf" else MediaFileTargetChangedError
        ):
            place(storage, source)
        assert not list((storage.library_root / "video").iterdir())
        assert source.is_symlink() if swap == "leaf" else source.read_bytes() == b"replacement"
    assert copied_bytes == [b"child content"]
    assert (outside / source.name).read_bytes() == b"outside secret"
    assert_no_temp(storage)


def test_swap_after_publish_prevents_replacement_unlink(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = output(storage)
    real_link = os.link

    def publish_then_swap(src: Path, dst: Path) -> None:
        real_link(src, dst)
        source.rename(source.with_suffix(".saved"))
        source.write_bytes(b"replacement")

    monkeypatch.setattr(os, "link", publish_then_swap)
    with pytest.raises(MediaFileTargetChangedError):
        place(storage, source)
    assert source.read_bytes() == b"replacement"
    assert (storage.library_root / "video" / "Title.mp4").read_bytes() == b"child content"
    assert_no_temp(storage)


@pytest.mark.parametrize("racing", [False, True])
def test_skip_existing_retains_source_and_identity(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch, racing: bool,
) -> None:
    source = output(storage)
    target = storage.final_target("video", "Title.mp4")
    if racing:
        real_link = os.link

        def raced_link(src: Path, dst: Path) -> None:
            dst.write_bytes(b"existing")
            real_link(src, dst)

        monkeypatch.setattr(os, "link", raced_link)
    else:
        target.write_bytes(b"existing")
    result = place(storage, source)
    assert result.skipped_existing
    assert target.read_bytes() == b"existing"
    assert source.read_bytes() == b"child content"
    assert result.identity == (target.stat().st_dev, target.stat().st_ino)
    assert_no_temp(storage)


def test_child_copy_does_not_link_source_across_filesystems(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = output(storage)
    real_link = os.link
    links: list[Path] = []

    def cross_device(src: Path, dst: Path) -> None:
        if src == source:
            raise OSError(errno.EXDEV, "cross device")
        links.append(src)
        assert src.parent == dst.parent
        real_link(src, dst)

    monkeypatch.setattr(os, "link", cross_device)
    assert place(storage, source).final_path.read_bytes() == b"child content"
    assert len(links) == 1
    assert not source.exists()
    assert_no_temp(storage)


@pytest.mark.parametrize("during_copy", [False, True])
def test_cancel_before_publication_cleans_temp_and_retains_source(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch, during_copy: bool,
) -> None:
    source = output(storage)
    cancelled = not during_copy
    real_copy = shutil.copyfileobj

    def cancel_copy(original: Any, copied: Any) -> None:
        nonlocal cancelled
        real_copy(original, copied)
        cancelled = True

    monkeypatch.setattr(shutil, "copyfileobj", cancel_copy)
    with pytest.raises(PlacementCancelled):
        place(storage, source, cancel_requested=lambda: cancelled, publish_guard=threading.Lock())
    assert source.read_bytes() == b"child content"
    assert not list((storage.library_root / "video").iterdir())


@pytest.mark.parametrize("cancel_publish", [False, True])
def test_repeated_waiter_cancel_drains_and_closes_source_descriptors(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch, cancel_publish: bool,
) -> None:
    source = output(storage)
    started, release = threading.Event(), threading.Event()
    real_copy = shutil.copyfileobj
    real_prepare = storage._prepare_child_output
    descriptors: list[int] = []

    def tracked_prepare(key: str, path: Path) -> Any:
        prepared = real_prepare(key, path)
        descriptors.extend([prepared._parent_fd, prepared._pinned_fd])
        return prepared

    def blocked_copy(original: Any, copied: Any) -> None:
        started.set()
        assert release.wait(3)
        real_copy(original, copied)

    monkeypatch.setattr(storage, "_prepare_child_output", tracked_prepare)
    monkeypatch.setattr(shutil, "copyfileobj", blocked_copy)

    async def run() -> None:
        task = asyncio.create_task(storage.place_child_final(
            "child", source, media_kind="video", title="Title",
            cancel_requested=lambda: cancel_publish and release.is_set(),
            publish_guard=threading.Lock(),
        ))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        if cancel_publish:
            with pytest.raises(PlacementCancelled):
                await asyncio.wait_for(task, 3)
            assert not list((storage.library_root / "video").iterdir())
        else:
            result = await asyncio.wait_for(task, 3)
            assert result.final_path.read_bytes() == b"child content"

    asyncio.run(run())
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert source.exists() == cancel_publish
    assert_no_temp(storage)


@pytest.mark.parametrize("kind", ["missing", "fifo", "symlink-parent"])
def test_rejected_output_closes_all_walk_descriptors(
    storage: StorageService, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    source = storage.job_staging_dir("child") / "output.mp4"
    if kind == "fifo":
        os.mkfifo(source)
    elif kind == "symlink-parent":
        outside = tmp_path / "outside"
        outside.mkdir()
        source.parent.rmdir()
        source.parent.symlink_to(outside, target_is_directory=True)
    active: set[int] = set()
    real_open, real_close = os.open, os.close

    def tracked_open(path: Any, flags: int, dir_fd: int | None = None) -> int:
        assert flags & os.O_NOFOLLOW
        if path == source.name:
            assert flags & os.O_NONBLOCK
        fd = real_open(path, flags, dir_fd=dir_fd)
        active.add(fd)
        return fd

    def tracked_close(fd: int) -> None:
        real_close(fd)
        active.remove(fd)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)
    with pytest.raises((MediaFileMissingError, MediaFileNotRegularError, StoragePathError)):
        place(storage, source)
    assert not active


def place_ordered(
    storage: StorageService, source: Path, order: Any, *,
    title: str = "Title", media_kind: str = "video",
) -> Any:
    return asyncio.run(storage.place_child_final(
        "child", source, media_kind=media_kind, title=title, source_order=order,
    ))


def test_legacy_without_order_keeps_flat_title(storage: StorageService) -> None:
    result = place_ordered(storage, output(storage), None)
    assert result.relative_path == "video/Title.mp4"


@pytest.mark.parametrize("order,name", [
    (1, "01 - Title.mp4"), (7, "07 - Title.mp4"),
    (100, "100 - Title.mp4"), (12345, "12345 - Title.mp4"),
])
def test_explicit_order_prefixes_video_filename(
    storage: StorageService, order: int, name: str,
) -> None:
    source = output(storage)
    result = place_ordered(storage, source, order)
    assert result.relative_path == f"video/{name}"
    assert result.final_path.read_bytes() == b"child content"
    assert not result.skipped_existing and not source.exists()
    assert_no_temp(storage)


@pytest.mark.parametrize("order,media_kind,error", [
    (True, "video", TypeError), (False, "video", TypeError),
    (1.0, "video", TypeError), ("1", "video", TypeError),
    (0, "video", ValueError), (-3, "video", ValueError),
    (1, "audio", ValueError),
])
def test_invalid_order_rejected_without_mutation(
    storage: StorageService, order: Any, media_kind: str, error: type[Exception],
) -> None:
    source = output(storage)
    with pytest.raises(error):
        place_ordered(storage, source, order, media_kind=media_kind)
    assert source.read_bytes() == b"child content"
    assert not list((storage.library_root / "video").iterdir())
    assert not list((storage.library_root / "audio").iterdir())


def test_long_unicode_title_keeps_prefix_and_extension_within_budget(
    storage: StorageService,
) -> None:
    result = place_ordered(storage, output(storage), 7, title="Şarkı ğüé " * 40)
    name = result.final_path.name
    assert name.startswith("07 - Şarkı") and name.endswith(".mp4")
    assert len(name.encode()) <= 150
    assert result.final_path.parent == storage.library_root / "video"


def test_control_only_title_falls_back_after_prefix(storage: StorageService) -> None:
    result = place_ordered(storage, output(storage), 7, title="\x00\x01\x1f")
    assert result.relative_path == "video/07 - download.mp4"


@pytest.mark.parametrize("title", ["../../etc/passwd", "a/b\\c", "/abs", "..", "."])
def test_path_shaped_titles_stay_flat_in_video_root(
    storage: StorageService, tmp_path: Path, title: str,
) -> None:
    result = place_ordered(storage, output(storage), 3, title=title)
    assert result.final_path.parent == storage.library_root / "video"
    assert result.final_path.name.startswith("03 - ")
    assert "/" not in result.final_path.name and "\\" not in result.final_path.name
    assert sorted(p.name for p in tmp_path.iterdir()) == ["library", "staging"]


def test_same_title_different_orders_are_separate_files(storage: StorageService) -> None:
    first = place_ordered(storage, output(storage), 2)
    second = place_ordered(storage, output(storage), 9)
    assert (first.relative_path, second.relative_path) == (
        "video/02 - Title.mp4", "video/09 - Title.mp4")
    assert not first.skipped_existing and not second.skipped_existing


def test_existing_prefixed_target_is_skipped_unchanged(storage: StorageService) -> None:
    target = storage.final_target("video", "07 - Title.mp4")
    target.write_bytes(b"existing")
    source = output(storage)
    result = place_ordered(storage, source, 7)
    assert result.skipped_existing and result.final_path == target
    assert target.read_bytes() == b"existing"
    assert source.read_bytes() == b"child content"
    assert result.identity == (target.stat().st_dev, target.stat().st_ino)
    assert_no_temp(storage)


def test_copy_failure_closes_descriptors_and_removes_temp(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = output(storage)
    descriptors: list[int] = []

    def fail_copy(original: Any, copied: Any) -> None:
        descriptors.extend([original.fileno(), copied.fileno()])
        copied.write(b"partial")
        raise OSError("controlled copy failure")

    monkeypatch.setattr(shutil, "copyfileobj", fail_copy)
    with pytest.raises(OSError, match="controlled copy failure"):
        place(storage, source)
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert source.read_bytes() == b"child content"
    assert not list((storage.library_root / "video").iterdir())
