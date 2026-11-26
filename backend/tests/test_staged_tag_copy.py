"""M6.5-T3: storage-owned staged copy for text tagging (PRODUCT_SPEC §14).

Storage copies a pinned staged file into an exclusive no-follow candidate in
the same job staging, lets the caller edit only that copy and returns the
chosen inode (edited copy or rechecked original) still pinned, so the later
placement walk is bound to that exact inode.
"""

import asyncio
import os
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from mutagen.flac import FLAC

from app.integrations.mutagen_tags import (
    TagError,
    TagErrorKind,
    TagOutcome,
    write_text_tags,
)
from app.services.job_runner import _text_tag_editor
from app.services.storage_service import (
    MediaFileTargetChangedError,
    StagedEditResult,
    StoragePathError,
    StorageService,
)
from app.services.text_tags import TextTags

KEY = "job"
TAGS = TextTags(title="Şarkı", artists=("A", "B"), album="Albüm", track_number=3)


def tiny_flac() -> bytes:
    """A real, Mutagen-parsable FLAC: STREAMINFO only, no audio frames."""
    # 44.1 kHz, 2 channels, 16 bits, 0 samples; packed into 64 bits.
    packed = (44_100 << 44) | (1 << 41) | (15 << 36)
    info = struct.pack(">HH", 4096, 4096) + bytes(6) + struct.pack(">Q", packed) + bytes(16)
    assert len(info) == 34
    return b"fLaC" + bytes([0x80, 0, 0, len(info)]) + info


def make_storage(tmp_path: Path) -> StorageService:
    storage = StorageService(tmp_path / "staging", tmp_path / "library")
    storage.ensure_layout()
    storage.initialize_delete_roots()
    return storage


def staged(storage: StorageService, data: bytes, name: str = "media.flac") -> Path:
    path = storage.job_staging_dir(KEY) / name
    path.write_bytes(data)
    return path


def raw_editor(suffix: str = ".flac") -> Any:
    """Writer without the runner adapter: a ``TagError`` escapes to storage."""

    def edit(handle: BinaryIO) -> bool:
        return write_text_tags(handle, suffix=suffix, tags=TAGS) is TagOutcome.WRITTEN

    return edit


def leftovers(storage: StorageService) -> list[str]:
    return sorted(p.name for p in storage.job_staging_dir(KEY).iterdir())


def identity_of(path: Path) -> tuple[int, int]:
    entry = os.stat(path, follow_symlinks=False)
    return (entry.st_dev, entry.st_ino)


def run_copy(storage: StorageService, source: Path, edit: Any) -> StagedEditResult:
    return asyncio.run(storage.stage_edited_copy(KEY, source, edit))


def place(storage: StorageService, result: StagedEditResult) -> Any:
    return asyncio.run(storage.place_final(
        result.path, media_kind="audio", title="Song", staging_key=KEY,
        source_identity=result.identity,
    ))


def replace(path: Path, data: bytes = b"impostor") -> None:
    path.unlink()
    path.write_bytes(data)


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@contextmanager
def no_fd_leak() -> Iterator[None]:
    if not os.path.isdir("/proc/self/fd"):
        yield
        return
    before = open_fds()
    yield
    assert open_fds() == before


def edited_name(storage: StorageService) -> str:
    [name] = [n for n in leftovers(storage) if n.startswith(".edit-")]
    return name


# --- edited copy -------------------------------------------------------------


def test_verified_copy_is_tagged_pinned_and_source_untouched(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, tiny_flac())
    result = run_copy(storage, source, raw_editor())
    try:
        assert result.edited
        # Same staging directory, same suffix, a distinct hidden name.
        assert result.path.parent == source.parent and result.path.suffix == ".flac"
        assert result.path.name != source.name and result.path.name.startswith(".")
        assert identity_of(result.path) == result.identity
        assert os.stat(result.path).st_nlink == 1  # genuine copy, never a hard link
        # The result keeps its own descriptor on that inode.
        pinned = os.fstat(result._pin._pinned_fd)
        assert (pinned.st_dev, pinned.st_ino) == result.identity
        assert source.read_bytes() == tiny_flac()
        tagged = FLAC(result.path)
        assert tagged["TITLE"] == ["Şarkı"] and tagged["ARTIST"] == ["A", "B"]
        assert tagged["TRACKNUMBER"] == ["3"]
    finally:
        result.close()


def test_copy_publishes_through_bound_placement(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, tiny_flac())
    with no_fd_leak():
        result = run_copy(storage, source, raw_editor())
        placement = place(storage, result)
        result.discard()  # no unlink: placement already moved it
    published = tmp_path / "library" / "audio" / "Song.flac"
    assert placement.final_path == published and placement.identity == result.identity
    assert identity_of(published) == result.identity
    assert FLAC(published)["TITLE"] == ["Şarkı"]
    assert placement.size_bytes == published.stat().st_size
    assert leftovers(storage) == ["media.flac"]
    assert source.read_bytes() == tiny_flac()


def test_editor_gets_separate_writable_inode(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, b"prepared bytes")
    source_ino = source.stat().st_ino
    seen: list[tuple[int, int, bytes]] = []

    def edit(handle: BinaryIO) -> bool:
        entry = os.fstat(handle.fileno())
        seen.append((entry.st_ino, entry.st_nlink, handle.read()))
        handle.seek(0)
        handle.write(b"EDITED")
        return True

    result = run_copy(storage, source, edit)
    result.close()
    [(inode, links, content)] = seen
    assert inode != source_ino and links == 1 and content == b"prepared bytes"
    assert result.path.read_bytes() == b"EDITEDed bytes"
    assert source.read_bytes() == b"prepared bytes"


def test_replaced_copy_keeps_original_inode_pinned_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, tiny_flac())
    with no_fd_leak():
        result = run_copy(storage, source, raw_editor())
        replace(result.path)
        # The pinned inode is still alive, so the replacement cannot reuse it.
        pinned = os.fstat(result._pin._pinned_fd)
        assert (pinned.st_dev, pinned.st_ino) == result.identity and pinned.st_nlink == 0
        assert identity_of(result.path) != result.identity
        with pytest.raises(MediaFileTargetChangedError):
            place(storage, result)
        result.discard()
    assert list((tmp_path / "library" / "audio").iterdir()) == []
    assert result.path.read_bytes() == b"impostor"  # unowned: never unlinked
    assert source.read_bytes() == tiny_flac()


def test_discard_removes_own_unpublished_copy(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, tiny_flac())
    with no_fd_leak():
        result = run_copy(storage, source, raw_editor())
        result.discard()
        result.discard()  # idempotent
    assert leftovers(storage) == ["media.flac"]


# --- nothing kept: the original stays pinned ------------------------------------


@pytest.mark.parametrize("data, suffix, failed", [
    (b"OggS\x00\x02 not m4a", ".m4a", False),  # writer: UNSUPPORTED, silent
    (b"fLaC", ".flac", True),  # writer: typed TagError captured by the adapter
], ids=["unsupported", "tag_error"])
def test_nothing_kept_returns_rechecked_pinned_original(
    tmp_path: Path, data: bytes, suffix: str, failed: bool,
) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, data, name=f"media{suffix}")
    failures: list[TagError] = []
    with no_fd_leak():
        result = run_copy(storage, source, _text_tag_editor(suffix, TAGS, failures))
        assert (result.edited, result.path) == (False, source)
        assert result.identity == identity_of(source)
        assert leftovers(storage) == [source.name]  # own copy removed
        assert (len(failures) == 1) is failed
        placement = place(storage, result)
        result.discard()
    published = tmp_path / "library" / "audio" / f"Song{suffix}"
    assert placement.final_path == published and published.read_bytes() == data
    assert placement.identity == result.identity == identity_of(published)
    assert placement.size_bytes == len(data)


def test_discard_of_original_never_unlinks_it(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, b"OggS\x00\x02 not m4a", name="media.m4a")
    with no_fd_leak():
        result = run_copy(storage, source, _text_tag_editor(".m4a", TAGS, []))
        result.discard()
    assert source.read_bytes() == b"OggS\x00\x02 not m4a"


@pytest.mark.parametrize("data, suffix", [
    (b"OggS\x00\x02 not m4a", ".m4a"), (b"fLaC", ".flac"),
], ids=["unsupported", "tag_error"])
def test_original_swapped_before_placement_publishes_nothing(
    tmp_path: Path, data: bytes, suffix: str,
) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, data, name=f"media{suffix}")
    with no_fd_leak():
        result = run_copy(storage, source, _text_tag_editor(suffix, TAGS, []))
        assert not result.edited
        replace(source)
        assert identity_of(source) != result.identity  # original inode still pinned
        with pytest.raises(MediaFileTargetChangedError):
            place(storage, result)
        result.discard()
    assert list((tmp_path / "library" / "audio").iterdir()) == []
    assert source.read_bytes() == b"impostor"  # unowned replacement kept


@pytest.mark.parametrize("data", [tiny_flac(), b"fLaC"], ids=["written", "tag_error"])
def test_source_swap_during_edit_fails_closed(tmp_path: Path, data: bytes) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, data)
    failures: list[TagError] = []
    real = _text_tag_editor(".flac", TAGS, failures)

    def edit(handle: BinaryIO) -> bool:
        replace(source)
        return real(handle)

    with no_fd_leak(), pytest.raises(MediaFileTargetChangedError):
        run_copy(storage, source, edit)
    assert (len(failures) == 1) is (data == b"fLaC")  # typed failure still rechecked
    assert leftovers(storage) == ["media.flac"]  # own copy removed
    assert source.read_bytes() == b"impostor"  # the swapped entry is not ours


def test_copy_swap_during_edit_never_unlinks_unowned_entry(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, tiny_flac())
    swapped: list[Path] = []

    def edit(handle: BinaryIO) -> bool:
        path = source.parent / edited_name(storage)
        replace(path)
        swapped.append(path)
        return True

    with no_fd_leak(), pytest.raises(MediaFileTargetChangedError):
        run_copy(storage, source, edit)
    [path] = swapped
    assert path.read_bytes() == b"impostor"
    assert source.read_bytes() == tiny_flac()


# --- genuine errors ----------------------------------------------------------------


def test_uncaptured_editor_error_propagates_and_removes_copy(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, b"fLaC")
    with no_fd_leak(), pytest.raises(TagError) as caught:
        run_copy(storage, source, raw_editor())
    assert caught.value.kind is TagErrorKind.PARSE
    assert leftovers(storage) == ["media.flac"]
    assert source.read_bytes() == b"fLaC"


def test_os_error_from_editor_propagates_and_removes_copy(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, tiny_flac())
    error = OSError(28, "No space left on device")

    def edit(handle: BinaryIO) -> bool:
        raise error

    with no_fd_leak(), pytest.raises(OSError) as caught:
        run_copy(storage, source, edit)
    assert caught.value is error
    assert leftovers(storage) == ["media.flac"]


@pytest.mark.parametrize("data", [b"fLaC", b"OggS\x00\x02 not flac"],
                         ids=["tag_error", "unsupported"])
def test_cleanup_error_replaces_outcome_and_closes_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes,
) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, data)
    real_unlink = os.unlink
    failures: list[TagError] = []

    def refuse(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path).startswith(".edit-"):
            raise PermissionError(13, "denied")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse)
    with no_fd_leak(), pytest.raises(PermissionError) as caught:
        run_copy(storage, source, _text_tag_editor(".flac", TAGS, failures))
    assert not isinstance(caught.value, TagError)
    assert source.read_bytes() == data


def test_bound_identity_requires_staging_key(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, b"x")
    with pytest.raises(ValueError, match="staging_key"):
        asyncio.run(storage.place_final(
            source, media_kind="audio", title="Song", source_identity=(1, 2),
        ))
    assert source.read_bytes() == b"x"


@pytest.mark.parametrize("kind", ["symlink", "outside"])
def test_unsafe_source_is_rejected_before_editing(tmp_path: Path, kind: str) -> None:
    storage = make_storage(tmp_path)
    outside = tmp_path / "outside.flac"
    outside.write_bytes(tiny_flac())
    called: list[bool] = []

    def edit(handle: BinaryIO) -> bool:
        called.append(True)
        return True

    if kind == "symlink":
        source = storage.job_staging_dir(KEY) / "media.flac"
        source.symlink_to(outside)
    else:
        source = outside
    with no_fd_leak(), pytest.raises(StoragePathError):
        run_copy(storage, source, edit)
    assert called == [] and outside.read_bytes() == tiny_flac()


# --- caller cancellation -----------------------------------------------------------


@pytest.mark.parametrize("data", [tiny_flac(), b"fLaC"], ids=["edited", "original"])
def test_repeated_cancellation_drains_worker_then_propagates(
    tmp_path: Path, data: bytes,
) -> None:
    storage = make_storage(tmp_path)
    source = staged(storage, data)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real = _text_tag_editor(".flac", TAGS, [])

    def edit(handle: BinaryIO) -> bool:
        entered.set()
        release.wait(5)
        try:
            return real(handle)
        finally:
            finished.set()

    async def scenario() -> None:
        task = asyncio.ensure_future(storage.stage_edited_copy(KEY, source, edit))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()  # still draining the blocking worker
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    with no_fd_leak():
        asyncio.run(scenario())
    # Whichever result emerged was discarded: no copy, original untouched.
    assert leftovers(storage) == ["media.flac"]
    assert source.read_bytes() == data
