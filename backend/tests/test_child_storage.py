import asyncio
import errno
import os
import shutil
import stat
import threading
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from app.services.storage_service import (
    MediaFileMissingError,
    MediaFileNotRegularError,
    MediaFileTargetChangedError,
    PlacementCancelled,
    StagedEditResult,
    StoragePathError,
    StorageService,
)
from app.utils.filenames import conflict_media_filenames


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


@pytest.mark.parametrize("kind", [
    "regular", "nested", "symlink_leaf", "symlink_parent", "symlink_child", "fifo",
    "directory", "missing", "sibling", "outside", "traversal",
])
def test_staged_regular_file_precheck_is_read_only_and_fail_closed(
    storage: StorageService, tmp_path: Path, kind: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "output.mp4").write_bytes(b"outside")
    sibling = storage.job_staging_dir("sibling") / "output.mp4"
    sibling.write_bytes(b"sibling")
    child = storage.job_staging_dir("child")
    source = child / "output.mp4"
    if kind in {"regular", "nested"}:
        if kind == "nested":
            (child / "nested").mkdir()
            source = child / "nested" / "output.mp4"
        source.write_bytes(b"child content")
    elif kind == "symlink_leaf":
        source.symlink_to(outside / "output.mp4")
    elif kind == "symlink_parent":
        (child / "nested").symlink_to(outside, target_is_directory=True)
        source = child / "nested" / "output.mp4"
    elif kind == "symlink_child":
        child.rmdir()
        child.symlink_to(outside, target_is_directory=True)
    elif kind == "fifo":
        os.mkfifo(source)  # must not block the precheck
    elif kind == "directory":
        source.mkdir()
    elif kind == "sibling":
        source = sibling
    elif kind == "outside":
        source = outside / "output.mp4"
    elif kind == "traversal":
        source = child / ".." / "sibling" / "output.mp4"
    before = sorted(p.name for p in storage.library_root.rglob("*"))
    assert storage.is_staged_regular_file("child", source) is (kind in {"regular", "nested"})
    assert (outside / "output.mp4").read_bytes() == b"outside"
    assert sibling.read_bytes() == b"sibling"
    assert sorted(p.name for p in storage.library_root.rglob("*")) == before


@pytest.mark.parametrize("kind", ["resolved", "alias", "symlink_leaf", "outside"])
def test_staged_regular_file_precheck_under_aliased_staging_root(
    tmp_path: Path, kind: str,
) -> None:
    (tmp_path / "real-staging").mkdir()
    alias = tmp_path / "staging-alias"
    alias.symlink_to(tmp_path / "real-staging", target_is_directory=True)
    storage = StorageService(alias, tmp_path / "library")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "output.mp4").write_bytes(b"outside")
    source = storage.job_staging_dir("child") / "output.mp4"  # resolved spelling
    if kind == "symlink_leaf":
        source.symlink_to(outside / "output.mp4")
    else:
        source.write_bytes(b"child content")
    if kind == "alias":
        source = alias / "child" / "output.mp4"
    elif kind == "outside":
        source = outside / "output.mp4"
    assert storage.is_staged_regular_file("child", source) is (kind in {"resolved", "alias"})
    assert (outside / "output.mp4").read_bytes() == b"outside"


def test_aliased_root_placement_copies_matched_file(tmp_path: Path) -> None:
    (tmp_path / "real-staging").mkdir()
    alias = tmp_path / "staging-alias"
    alias.symlink_to(tmp_path / "real-staging", target_is_directory=True)
    storage = StorageService(alias, tmp_path / "library")
    storage.ensure_layout()
    storage.initialize_delete_roots()
    source = storage.job_staging_dir("child") / "output.mp4"  # resolved spelling
    source.write_bytes(b"child content")

    result = place(storage, source)

    assert result.final_path.read_bytes() == b"child content"
    assert not source.exists()


def test_alias_swap_after_match_fails_closed_without_touching_either_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The alias is retargeted right after the root is resolved for path
    # matching. Placement must verify that same resolved root and refuse,
    # never copying and unlinking the same-named file under the new target.
    real_a, real_b = tmp_path / "real-a", tmp_path / "real-b"
    real_a.mkdir()
    real_b.mkdir()
    alias = tmp_path / "staging-alias"
    alias.symlink_to(real_a, target_is_directory=True)
    storage = StorageService(alias, tmp_path / "library")
    storage.ensure_layout()
    storage.initialize_delete_roots()
    source = storage.job_staging_dir("child") / "output.mp4"
    source.write_bytes(b"matched")
    decoy = real_b / "child" / "output.mp4"
    decoy.parent.mkdir()
    decoy.write_bytes(b"decoy")
    original_resolve = Path.resolve
    swapped = threading.Event()

    def resolve_then_swap(self: Path, strict: bool = False) -> Path:
        resolved = original_resolve(self, strict=strict)
        if self == alias and not swapped.is_set():
            swapped.set()
            alias.unlink()
            alias.symlink_to(real_b, target_is_directory=True)
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_then_swap)
    with pytest.raises(StoragePathError):
        place(storage, source)
    monkeypatch.undo()

    assert swapped.is_set()
    assert source.read_bytes() == b"matched"
    assert decoy.read_bytes() == b"decoy"
    assert not list((storage.library_root / "video").iterdir())


@pytest.mark.parametrize("key", ["", ".", "..", "../child", "a/b", "a\\b", "/child"])
def test_staged_regular_file_precheck_rejects_invalid_keys(
    storage: StorageService, key: str,
) -> None:
    assert storage.is_staged_regular_file(key, output(storage)) is False


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

    def publish_then_swap(src: Any, dst: Any, **kwargs: Any) -> None:
        real_link(src, dst, **kwargs)
        source.rename(source.with_suffix(".saved"))
        source.write_bytes(b"replacement")

    monkeypatch.setattr(os, "link", publish_then_swap)
    with pytest.raises(MediaFileTargetChangedError):
        place(storage, source)
    assert source.read_bytes() == b"replacement"
    assert (storage.library_root / "video" / "Title.mp4").read_bytes() == b"child content"
    assert_no_temp(storage)


@pytest.mark.parametrize("racing", [False, True])
def test_existing_target_publishes_next_number_and_keeps_existing(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch, racing: bool,
) -> None:
    source = output(storage)
    target = storage.final_target("video", "Title.mp4")
    if racing:
        real_link = os.link

        def raced_link(src: Any, dst: Any, **kwargs: Any) -> None:
            if str(dst) == target.name and not target.exists():
                target.write_bytes(b"existing")  # a racer wins the first name
            real_link(src, dst, **kwargs)

        monkeypatch.setattr(os, "link", raced_link)
    else:
        target.write_bytes(b"existing")
    result = place(storage, source)
    assert result.relative_path == "video/Title (2).mp4"
    assert result.final_path.read_bytes() == b"child content"
    assert target.read_bytes() == b"existing"
    assert not source.exists()
    placed = result.final_path.stat()
    assert result.identity == (placed.st_dev, placed.st_ino)
    assert storage.verify_media_file(
        "app_library", result.relative_path, result.identity,
    ) == result.final_path
    assert_no_temp(storage)


def test_child_copy_does_not_link_source_across_filesystems(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = output(storage)
    real_link = os.link
    links: list[str] = []

    def cross_device(src: Any, dst: Any, **kwargs: Any) -> None:
        if not str(src).startswith("."):
            raise OSError(errno.EXDEV, "cross device")
        links.append(str(src))
        # Temp and target share the one verified directory descriptor.
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
        real_link(src, dst, **kwargs)

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
        # asyncio closes its own untracked pidfd while creating the loop.
        active.discard(fd)

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
    assert not source.exists()
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
    assert first.final_path.read_bytes() == second.final_path.read_bytes() == b"child content"


def test_existing_prefixed_target_gets_numbered_sibling(storage: StorageService) -> None:
    target = storage.final_target("video", "07 - Title.mp4")
    target.write_bytes(b"existing")
    source = output(storage)
    result = place_ordered(storage, source, 7)
    assert result.relative_path == "video/07 - Title (2).mp4"
    assert result.final_path.read_bytes() == b"child content"
    assert target.read_bytes() == b"existing"
    assert not source.exists()
    placed = result.final_path.stat()
    assert result.identity == (placed.st_dev, placed.st_ino)
    assert_no_temp(storage)


def place_named(
    storage: StorageService, source: Path, *, order: int | None = None,
    short_edge: Any = None, media_kind: str = "video", title: str = "Title",
    **kwargs: Any,
) -> Any:
    return asyncio.run(storage.place_child_final(
        "child", source, media_kind=media_kind, title=title, source_order=order,
        video_short_edge=short_edge, **kwargs,
    ))


def audio_output(storage: StorageService) -> Path:
    source = storage.job_staging_dir("child") / "output.m4a"
    source.write_bytes(b"child audio")
    return source


@pytest.mark.parametrize("order,short_edge,name", [
    (7, 720, "07 - Title (720p).mp4"),
    (12, 1080, "12 - Title (1080p).mp4"),
    (None, 720, "Title (720p).mp4"),
    (7, None, "07 - Title.mp4"),
])
def test_child_video_name_carries_order_and_measured_short_edge(
    storage: StorageService, order: int | None, short_edge: int | None, name: str,
) -> None:
    source = output(storage)
    result = place_named(storage, source, order=order, short_edge=short_edge)
    assert result.relative_path == f"video/{name}"
    assert result.final_path.read_bytes() == b"child content"
    assert not source.exists()
    assert_no_temp(storage)


@pytest.mark.parametrize("media_kind,order,short_edge,existing,expected", [
    ("video", 7, 720, ["07 - Title (720p).mp4"], "07 - Title (720p) (2).mp4"),
    ("video", 7, 720, ["07 - Title.mp4"], "07 - Title (720p).mp4"),
    ("video", 7, 720, ["07 - Title (1080p).mp4"], "07 - Title (720p).mp4"),
    ("audio", None, None, ["Title.m4a", "Title (2).m4a"], "Title (3).m4a"),
])
def test_child_conflict_publishes_next_number_and_keeps_existing(
    storage: StorageService, media_kind: str, order: int | None,
    short_edge: int | None, existing: list[str], expected: str,
) -> None:
    folder = storage.library_root / media_kind
    for name in existing:
        (folder / name).write_bytes(b"old")
    source = audio_output(storage) if media_kind == "audio" else output(storage)
    content = source.read_bytes()
    result = place_named(
        storage, source, order=order, short_edge=short_edge, media_kind=media_kind,
    )
    assert result.relative_path == f"{media_kind}/{expected}"
    assert result.final_path.read_bytes() == content
    assert all((folder / name).read_bytes() == b"old" for name in existing)
    assert not list(folder.glob("*.part"))


@pytest.mark.parametrize("kind", ["symlink", "dangling", "outside", "directory"])
def test_child_occupied_leaf_is_skipped_never_followed(
    storage: StorageService, tmp_path: Path, kind: str,
) -> None:
    folder = storage.library_root / "video"
    occupied = folder / "07 - Title (720p).mp4"
    inside = folder / "inside.mp4"
    inside.write_bytes(b"inside")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    if kind == "symlink":
        occupied.symlink_to(inside)
    elif kind == "dangling":
        occupied.symlink_to(folder / "missing.mp4")
    elif kind == "outside":
        occupied.symlink_to(outside)
    else:
        occupied.mkdir()
    result = place_named(storage, output(storage), order=7, short_edge=720)
    assert result.final_path.name == "07 - Title (720p) (2).mp4"
    assert result.final_path.parent == folder.resolve()
    assert not result.final_path.is_symlink()
    assert result.final_path.read_bytes() == b"child content"
    assert inside.read_bytes() == b"inside"
    assert outside.read_bytes() == b"outside"
    assert not (folder / "missing.mp4").exists()
    assert occupied.is_dir() if kind == "directory" else occupied.is_symlink()
    assert_no_temp(storage)


def test_child_temp_copy_is_made_once_and_rechecked_per_attempt(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = storage.library_root / "video"
    (folder / "07 - Title (720p).mp4").write_bytes(b"old")
    (folder / "07 - Title (720p) (2).mp4").write_bytes(b"old")
    source = output(storage)
    real_copy, real_link = shutil.copyfileobj, os.link
    real_check = StorageService._check_child_entry
    events: list[str] = []

    def counted_copy(original: Any, copied: Any) -> None:
        events.append("copy")
        real_copy(original, copied)

    def recorded_check(prepared: Any) -> None:
        events.append("check")
        real_check(prepared)

    def recorded_link(src: Any, dst: Any, **kwargs: Any) -> None:
        events.append(f"link:{str(src).startswith('.')}:{dst}")
        real_link(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copyfileobj", counted_copy)
    monkeypatch.setattr(StorageService, "_check_child_entry", staticmethod(recorded_check))
    monkeypatch.setattr(os, "link", recorded_link)
    result = place_named(storage, source, order=7, short_edge=720)
    assert result.final_path.name == "07 - Title (720p) (3).mp4"
    assert events == [
        "copy",
        "check", "link:True:07 - Title (720p).mp4",
        "check", "link:True:07 - Title (720p) (2).mp4",
        "check", "link:True:07 - Title (720p) (3).mp4",
    ]
    assert not source.exists()
    assert_no_temp(storage)


def test_child_source_swap_between_attempts_fails_closed(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (storage.library_root / "video" / "Title.mp4").write_bytes(b"old")
    source = output(storage)
    real_link = os.link

    def swap_on_conflict(src: Any, dst: Any, **kwargs: Any) -> None:
        try:
            real_link(src, dst, **kwargs)
        except FileExistsError:
            source.rename(source.with_suffix(".saved"))
            source.write_bytes(b"replacement")
            raise

    monkeypatch.setattr(os, "link", swap_on_conflict)
    with pytest.raises(MediaFileTargetChangedError):
        place(storage, source)
    assert source.read_bytes() == b"replacement"
    assert sorted(p.name for p in (storage.library_root / "video").iterdir()) == ["Title.mp4"]
    assert_no_temp(storage)


def test_child_cancel_between_attempts_publishes_nothing(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (storage.library_root / "video" / "Title.mp4").write_bytes(b"old")
    source = output(storage)
    cancelled = threading.Event()
    real_link = os.link

    def link_then_cancel(src: Any, dst: Any, **kwargs: Any) -> None:
        try:
            real_link(src, dst, **kwargs)
        except FileExistsError:
            cancelled.set()
            raise

    monkeypatch.setattr(os, "link", link_then_cancel)
    with pytest.raises(PlacementCancelled):
        place(storage, source, cancel_requested=cancelled.is_set,
              publish_guard=threading.Lock())
    assert source.read_bytes() == b"child content"
    assert sorted(p.name for p in (storage.library_root / "video").iterdir()) == ["Title.mp4"]


def test_child_parent_swap_after_first_conflict_fails_closed(
    storage: StorageService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = storage.library_root / "video"
    (folder / "Title.mp4").write_bytes(b"old")
    outside = tmp_path / "outside"
    outside.mkdir()
    source = output(storage)
    real_link = os.link

    def swap_parent_on_conflict(src: Any, dst: Any, **kwargs: Any) -> None:
        try:
            real_link(src, dst, **kwargs)
        except FileExistsError:
            folder.rename(storage.library_root / "moved")
            folder.symlink_to(outside, target_is_directory=True)
            raise

    monkeypatch.setattr(os, "link", swap_parent_on_conflict)
    with pytest.raises(StoragePathError):
        place(storage, source)
    assert not list(outside.iterdir())  # no write or cleanup outside the root
    assert source.read_bytes() == b"child content"


def test_child_target_swap_right_after_link_fails_closed(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = storage.library_root / "video"
    source = output(storage)
    real_link = os.link

    def link_then_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        real_link(src, dst, **kwargs)
        (folder / dst).unlink()
        (folder / dst).write_bytes(b"impostor")

    monkeypatch.setattr(os, "link", link_then_replace)
    with pytest.raises(MediaFileTargetChangedError):
        place(storage, source)
    assert source.read_bytes() == b"child content"
    assert (folder / "Title.mp4").read_bytes() == b"impostor"
    assert_no_temp(storage)


def test_child_counter_exhaustion_keeps_source_and_existing(
    storage: StorageService,
) -> None:
    folder = storage.library_root / "video"
    names = conflict_media_filenames("Title", "mp4", prefix="07 - ", qualifier=" (720p)")
    for name in names:
        (folder / name).write_bytes(b"old")
    source = output(storage)
    with pytest.raises(FileExistsError):
        place_named(storage, source, order=7, short_edge=720)
    assert source.read_bytes() == b"child content"
    assert sorted(p.name for p in folder.iterdir()) == sorted(names)
    assert_no_temp(storage)


@pytest.mark.parametrize("media_kind,value,error", [
    ("video", 0, ValueError), ("video", -1, ValueError), ("video", True, TypeError),
    ("video", 720.0, TypeError), ("audio", 720, ValueError),
])
def test_child_invalid_short_edge_rejected_without_mutation(
    storage: StorageService, media_kind: str, value: Any, error: type[Exception],
) -> None:
    source = output(storage)
    with pytest.raises(error):
        place_named(storage, source, short_edge=value, media_kind=media_kind)
    assert source.read_bytes() == b"child content"
    assert not list((storage.library_root / "video").iterdir())
    assert not list((storage.library_root / "audio").iterdir())


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


# --- M6.5-T4 child placement bound to a pinned staged selection ------------------------


def staged_selection(storage: StorageService, *, keep: bool) -> StagedEditResult:
    """A pinned staged edit result: the edited copy (``keep``) or the original."""
    def edit(handle: BinaryIO) -> bool:
        if keep:
            handle.write(b"TAGGED")
        return keep

    return asyncio.run(storage.stage_edited_copy("child", output(storage), edit))


def staging_leftovers(storage: StorageService) -> list[str]:
    return sorted(p.name for p in storage.job_staging_dir("child").iterdir())


@pytest.mark.parametrize("keep", [True, False], ids=["edited", "untouched"])
def test_bound_child_placement_publishes_the_pinned_selection(
    storage: StorageService, keep: bool,
) -> None:
    selection = staged_selection(storage, keep=keep)
    try:
        result = place(storage, selection.path, source_identity=selection.identity)
    finally:
        selection.discard()
    assert result.relative_path == "video/Title.mp4"
    assert result.final_path.read_bytes() == (b"TAGGEDcontent" if keep else b"child content")
    # Child placement still publishes an independent copy of the pinned inode.
    assert result.identity != selection.identity
    placed = result.final_path.stat()
    assert result.identity == (placed.st_dev, placed.st_ino)
    assert result.size_bytes == placed.st_size
    # Only the published source is unlinked; an edit keeps the prepared output.
    assert staging_leftovers(storage) == (["output.mp4"] if keep else [])
    assert_no_temp(storage)


@pytest.mark.parametrize("keep", [True, False], ids=["edited", "untouched"])
@pytest.mark.parametrize("change", ["replaced", "mismatch"])
def test_bound_child_placement_rejects_other_inode_before_copy(
    storage: StorageService, monkeypatch: pytest.MonkeyPatch, keep: bool, change: str,
) -> None:
    copies: list[str] = []
    real_copy = storage._copy_source_to_temp

    def recorded_copy(*args: Any, **kwargs: Any) -> tuple[int, int]:
        copies.append("copy")
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(storage, "_copy_source_to_temp", recorded_copy)
    selection = staged_selection(storage, keep=keep)
    expected = selection.path.read_bytes()
    try:
        identity = selection.identity
        if change == "replaced":
            # The pinned inode stays alive, so the replacement gets a new number.
            selection.path.unlink()
            selection.path.write_bytes(b"impostor")
            expected = b"impostor"
        else:
            identity = (identity[0], identity[1] + 1)
        with pytest.raises(MediaFileTargetChangedError):
            place(storage, selection.path, source_identity=identity)
    finally:
        selection.discard()
    assert copies == []  # rejected before any copy or publish
    assert not list((storage.library_root / "video").iterdir())
    # The entry at the source name is never unlinked by the rejected placement.
    if change == "replaced" or not keep:
        assert selection.path.read_bytes() == expected
    assert (storage.job_staging_dir("child") / "output.mp4").exists()
