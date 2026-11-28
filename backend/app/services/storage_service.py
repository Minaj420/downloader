"""Storage boundary: staging layout and finalization into the App Library.

All filesystem behaviour for downloads lives here (ARCHITECTURE.md §28–30).
An existing entry is never overwritten (PRODUCT_SPEC.md §15–16): a video
name carries the measured short edge (``Title (720p).ext``) and any name
collision publishes the next free ``(2)``, ``(3)`` … ``(999)`` candidate,
chosen only by atomic ``link`` ``EEXIST`` results, never by a prior
existence check. Publication is descriptor-relative to a verified media
directory, from a pinned source, and the published inode is verified
against the expected identity before the source is unlinked.

Catalogued media deletion (PRODUCT_SPEC.md §21 "Delete File",
ARCHITECTURE.md §37–38) also lives here: physical targets are reached
only through delete roots whose identity was captured at startup
(``initialize_delete_roots``) and re-validated per operation by a
fail-closed, descriptor-relative walk from the filesystem root — never
from a client-supplied path, and never through a cached descriptor.

Placements carry the placed file's storage identity, and
``verify_media_file`` re-checks a target under the same validated-root
semantics: that is the writer half of the concurrent-delete-safe
catalog protocol (§38, §51), used by every catalog write that commits
a ``media_files`` row.
"""

import asyncio
import errno
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from app.domain.library import MediaStorageType
from app.utils.filenames import (
    conflict_media_filenames,
    sanitize_media_filename,
    sanitize_path_segment,
)
from app.utils.paths import ensure_within_root

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PlacementResult:
    # Always the newly published file: placement never reports an
    # existing entry as its result.
    final_path: Path
    relative_path: str
    # Size and identity come from the published inode itself (pinned source
    # or temporary copy fd), never from re-reading the target path; the
    # target was verified no-follow against them right after the link.
    size_bytes: int
    # (st_dev, st_ino) of the file actually present at the target,
    # captured inside the storage boundary. The catalog write that
    # references this placement must re-verify this identity before
    # committing (ARCHITECTURE.md §38, §51).
    identity: tuple[int, int]


class PlacementCancelled(Exception):
    """Finalization was cancelled before the file became visible."""


class StorageTargetError(ValueError):
    """A catalogued storage mapping is unsupported or not configured."""


class StoragePathError(ValueError):
    """A catalogued path cannot resolve inside its configured storage root."""


class MediaFileMissingError(Exception):
    """The catalogued physical file does not exist on disk."""


class MediaFileNotRegularError(Exception):
    """The catalogued physical target is not a regular file (directory, symlink, …)."""


class StorageDeleteFailedError(Exception):
    """The filesystem refused the deletion (permissions, I/O failure, …)."""


class MediaFileTargetChangedError(Exception):
    """The final entry changed between preparation and deletion."""


class StagingRootVerificationError(Exception):
    """The staging root does not match its startup snapshot (Q3, fail closed).

    ``reason`` is one of ``snapshot_missing``, ``replay_failed`` or
    ``identity_changed``. The message is fixed and never carries a path.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"staging root verification failed: {reason}")
        self.reason = reason


class ClearHistoryStorageError(Exception):
    """The Q3 staging removal refused or failed (fail closed).

    ``reason`` is a fixed token (``invalid_key``, ``rmtree_unsafe``,
    ``roots_unverified``, ``root_overlap``, ``alias_through_staging``,
    ``optional_root_unverifiable``, ``target_not_directory``,
    ``mountinfo_unavailable``, ``mountinfo_malformed``, ``sub_mount``,
    ``mount_unverifiable``, ``mount_ambiguous``, ``bind_overlap``,
    ``device_boundary``,
    ``inspect_failed`` or ``delete_failed``). The message is fixed and
    never carries a path or raw error text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"clear history staging removal failed: {reason}")
        self.reason = reason


_PROBE_PREFIX = ".downloader-probe-"
_PROBE_PAYLOAD = b"downloader startup write probe\n"
_PROBE_NAME_ATTEMPTS = 4


@dataclass(frozen=True, slots=True)
class StartupProbeResult:
    """Path-free startup write-check outcome for one storage root.

    ``target`` is ``staging`` or a storage type value (``app_library``,
    ``plex_music``, ``plex_video``). ``reason`` is ``None`` on success,
    else one fixed token: ``root_unavailable``, ``root_changed``,
    ``name_collision``, ``create_failed``, ``write_failed``,
    ``cleanup_failed``, ``cleanup_unverified`` or ``probe_failed``. It never
    carries a path or raw error text and describes only the permissions of
    this process at startup, not a guarantee of future writes.
    """

    target: str
    ok: bool
    reason: str | None = None


_MOUNTINFO_PATH = "/proc/self/mountinfo"
_DELETED_SUFFIX = " (deleted)"
_OCTAL_DIGITS = b"01234567"
_OPTIONAL_MOUNT_FIELD = re.compile(rb"(?:shared|master|propagate_from):[0-9]+|unbindable")
_ACCESS_MODES = (b"rw", b"ro")


@dataclass(frozen=True, slots=True)
class _MountEntry:
    """One parsed ``/proc/self/mountinfo`` line (decoded, never resolved)."""

    mount_id: int
    parent_id: int
    device: str  # filesystem identity ``major:minor``
    root: str  # path inside that filesystem shown at ``point``
    point: Path
    # Spelling without the kernel's `` (deleted)`` suffix, else ``None``.
    stripped: Path | None

    def spellings(self) -> tuple[Path, ...]:
        if self.stripped is None:
            return (self.point,)
        return (self.point, self.stripped)


def _single_slash(path: str) -> Path:
    """Absolute POSIX path with the implementation-defined ``//`` collapsed."""
    return Path("/" + path.lstrip("/"))


def _unescape_mount_field(field: bytes) -> str:
    """Decode a kernel-escaped mountinfo field (``\\ooo`` octal escapes).

    Any backslash not followed by exactly three octal digits, an escaped
    value above one byte or a NUL byte is malformed (fail closed).
    """
    out = bytearray()
    i = 0
    while i < len(field):
        byte = field[i]
        if byte == 0x5C:
            code = field[i + 1:i + 4]
            if len(code) != 3 or any(c not in _OCTAL_DIGITS for c in code):
                raise ClearHistoryStorageError("mountinfo_malformed")
            value = int(code, 8)
            if value > 0xFF:
                raise ClearHistoryStorageError("mountinfo_malformed")
            out.append(value)
            i += 4
        else:
            out.append(byte)
            i += 1
    if 0 in out:
        raise ClearHistoryStorageError("mountinfo_malformed")
    return os.fsdecode(bytes(out))


def _parse_mountinfo(raw: bytes) -> list[_MountEntry]:
    """Strictly parse ``/proc/self/mountinfo`` content.

    Every line must be exactly ``id parent major:minor root mountpoint
    options [optional...] - fstype source superoptions``: six mandatory
    fields, only known optional tags, the separator, then exactly three
    non-empty fields. Mount and super options must start with ``rw``/``ro``,
    ids must be unique digits. Any shifted, truncated or otherwise
    malformed line, and empty content, raises
    ``ClearHistoryStorageError("mountinfo_malformed")``.
    """
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        raise ClearHistoryStorageError("mountinfo_malformed")
    entries: list[_MountEntry] = []
    seen: set[int] = set()
    for line in lines:
        fields = line.split(b" ")
        try:
            separator = fields.index(b"-", 6)
        except ValueError as exc:
            raise ClearHistoryStorageError("mountinfo_malformed") from exc
        major, colon, minor = fields[2].partition(b":")
        if (
            len(fields) != separator + 4
            or any(not field for field in fields)
            or not fields[0].isdigit()
            or not fields[1].isdigit()
            or not colon
            or not major.isdigit()
            or not minor.isdigit()
            or fields[5].split(b",")[0] not in _ACCESS_MODES
            or fields[separator + 3].split(b",")[0] not in _ACCESS_MODES
            or any(
                not _OPTIONAL_MOUNT_FIELD.fullmatch(optional)
                for optional in fields[6:separator]
            )
        ):
            raise ClearHistoryStorageError("mountinfo_malformed")
        mount_id = int(fields[0])
        if mount_id in seen:
            raise ClearHistoryStorageError("mountinfo_malformed")
        seen.add(mount_id)
        root = _unescape_mount_field(fields[3])
        point = _unescape_mount_field(fields[4])
        if not point.startswith("/"):
            raise ClearHistoryStorageError("mountinfo_malformed")
        stripped: Path | None = None
        if point.endswith(_DELETED_SUFFIX):
            stripped = _single_slash(point[: -len(_DELETED_SUFFIX)])
        entries.append(_MountEntry(
            mount_id, int(fields[1]), f"{int(major)}:{int(minor)}", root,
            _single_slash(point), stripped,
        ))
    return entries


def _mount_backing(path: Path, mounts: Sequence[_MountEntry]) -> tuple[str, Path]:
    """``(major:minor, path inside that filesystem)`` backing a canonical path.

    The most specific visible mount is chosen on whole path components.
    The mounts covering ``path`` must form one unambiguous chain from ``/``:
    exactly one mount per mount point, each the child of the previous one
    (a stacked, shadowed or orphaned mount breaks the chain), no
    `` (deleted)`` mount on the way, and the chosen mount must expose an
    absolute, non-deleted root. Anything else raises
    ``ClearHistoryStorageError("mount_ambiguous")``.
    """
    for mount in mounts:
        if mount.stripped is not None and path.is_relative_to(mount.stripped):
            raise ClearHistoryStorageError("mount_ambiguous")
    covering = sorted(
        (m for m in mounts if m.stripped is None and path.is_relative_to(m.point)),
        key=lambda m: len(m.point.parts),
    )
    if not covering or covering[0].point != Path("/"):
        raise ClearHistoryStorageError("mount_ambiguous")
    for outer, inner in zip(covering, covering[1:], strict=False):
        if inner.point == outer.point or inner.parent_id != outer.mount_id:
            raise ClearHistoryStorageError("mount_ambiguous")
    top = covering[-1]
    if not top.root.startswith("/") or top.root.endswith(_DELETED_SUFFIX):
        raise ClearHistoryStorageError("mount_ambiguous")
    return top.device, _single_slash(top.root) / path.relative_to(top.point)


def _component_open_error(component: str, exc: OSError) -> Exception:
    """Map a parent-component open failure to a typed, fail-closed error."""
    if exc.errno == errno.ENOENT:
        return MediaFileMissingError(component)
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        return StoragePathError(
            f"symlink or non-directory component rejected: {component!r}"
        )
    return StorageDeleteFailedError(str(exc))


def _final_open_error(final_name: str, exc: OSError) -> Exception:
    """Map a final-entry open failure to a typed, fail-closed error."""
    if exc.errno == errno.ENOENT:
        return MediaFileMissingError(final_name)
    if exc.errno == errno.ELOOP:
        return StoragePathError(f"final entry is a symlink, rejected: {final_name!r}")
    return StorageDeleteFailedError(str(exc))


def _root_walk_error(component: str, exc: OSError) -> Exception:
    """Map a delete-root ancestor-walk failure to a typed, fail-closed error."""
    if exc.errno == errno.ENOENT:
        return StorageTargetError(
            f"storage root is no longer reachable at component {component!r}"
        )
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        return StoragePathError(
            f"symlink or non-directory ancestor rejected: {component!r}"
        )
    return StorageDeleteFailedError(str(exc))


@dataclass(slots=True)
class _RootStep:
    component: str
    # Only aliases explicitly observed during startup may be replayed.
    alias_identity: tuple[int, int] | None = None
    alias_target: str | None = None


@dataclass(slots=True)
class _DeleteRoot:
    resolved: Path
    steps: tuple[_RootStep, ...]
    identity: tuple[int, int]


@dataclass(slots=True)
class ResolvedMediaFile:
    """Validated catalog location and its current entry identity, if present.

    Missing paths retain their lexical location, never a fabricated inode.
    Equality of this DTO is not the shared-reference predicate: callers
    compare locations OR existing identities with the pinned target.
    """

    resolved_path: Path
    identity: tuple[int, int] | None


@dataclass(slots=True)
class PreparedMediaFile:
    """Descriptor-anchored deletion target (ARCHITECTURE.md §38, §49).

    ``_parent_fd`` is the directory descriptor reached from a
    startup-identity-checked root descriptor by a fail-closed walk
    (``O_DIRECTORY | O_NOFOLLOW`` per component, so symlink components
    are rejected) and ``_pinned_fd`` pins the verified final entry. The
    shared-reference location and inode identity and the unlink
    therefore operate on the same verified target: the path is never
    re-resolved at delete time, and parent-name or root swaps cannot
    redirect the unlink outside the storage root. Within the *same*
    parent directory POSIX offers no atomic verify-and-unlink, so a
    same-directory entry swap in the micro-window between the identity
    recheck and the unlink is the documented residual race (it stays
    inside the root and every detectable swap fails closed).
    ``close()`` is idempotent and must run on every path, including
    cancellation.
    """

    _parent_fd: int
    _pinned_fd: int
    final_name: str
    resolved_path: Path
    _identity: tuple[int, int]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self._pinned_fd, self._parent_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    @property
    def identity(self) -> tuple[int, int]:
        """The pinned entry's ``(st_dev, st_ino)`` identity."""
        return self._identity

    def delete(self) -> Path:
        """Unlink the verified entry under the held parent descriptor.

        The final entry is re-verified as close to the unlink as POSIX
        allows — same parent descriptor, no symlink following, and the
        dev/ino identity must still match the pinned inode — so every
        detectable replacement (symlink, directory, different inode)
        fails closed instead of deleting the wrong file. The residual
        same-parent lstat→unlink micro-window is inherent to POSIX and
        documented on the class. Returns the resolved path removed.
        """
        if self._closed:
            raise StorageDeleteFailedError("prepared target already closed")
        try:
            entry_stat = os.stat(
                self.final_name, dir_fd=self._parent_fd, follow_symlinks=False
            )
        except FileNotFoundError as exc:
            raise MediaFileMissingError(str(self.resolved_path)) from exc
        except OSError as exc:
            raise StorageDeleteFailedError(str(exc)) from exc
        if not stat.S_ISREG(entry_stat.st_mode):
            raise MediaFileNotRegularError(str(self.resolved_path))
        if (entry_stat.st_dev, entry_stat.st_ino) != self._identity:
            raise MediaFileTargetChangedError(str(self.resolved_path))
        try:
            os.unlink(self.final_name, dir_fd=self._parent_fd)
        except OSError as exc:
            raise StorageDeleteFailedError(str(exc)) from exc
        return self.resolved_path


@dataclass(slots=True)
class StagedEditResult:
    """The staged file chosen for placement after an edit (PRODUCT_SPEC §14).

    ``edited`` is ``True`` for the verified edited copy (same staging
    directory and suffix as its source) and ``False`` for the rechecked
    original source when the editor kept nothing. ``_pin`` holds that inode
    and its staging directory open until ``discard``/``close``, so the inode
    cannot be freed and its number reused while ``place_final(
    source_identity=identity)`` binds a fresh walk from the staging root to it.

    ``discard`` never raises and always closes. Only an edited copy is
    unlinked, under the held directory and only while the name still holds the
    pinned inode (a no-op once placement moved it); the original source and
    any unowned replacement are never unlinked. ``close`` is idempotent.
    """

    path: Path
    edited: bool
    _pin: PreparedMediaFile

    @property
    def identity(self) -> tuple[int, int]:
        return self._pin.identity

    def close(self) -> None:
        self._pin.close()

    def discard(self) -> None:
        pin = self._pin
        try:
            if self.edited and not pin._closed:
                entry = os.stat(
                    pin.final_name, dir_fd=pin._parent_fd, follow_symlinks=False
                )
                if stat.S_ISREG(entry.st_mode) and (
                    entry.st_dev, entry.st_ino
                ) == pin.identity:
                    os.unlink(pin.final_name, dir_fd=pin._parent_fd)
        except OSError:
            pass
        finally:
            pin.close()


@dataclass(slots=True)
class _PublishDir:
    """Verified flat publish directory held as ``root_fd`` + ``fd``.

    ``root_steps``/``root_identity`` are the App Library root snapshot taken
    once — at startup, or by the fallback capture when placement began —
    and are never re-captured or re-resolved during the operation.

    ``verify`` replays ``root_steps`` with the existing no-follow,
    alias-aware walker (``replay_root``), requires the replayed root to
    still be ``root_identity`` and ``name`` below it to still be the
    directory ``fd`` was opened on. A root or media directory that was
    moved, renamed or replaced therefore fails closed instead of letting a
    held descriptor publish outside the configured location. Replay errors
    keep their typed storage errors and never leak descriptors.
    ``verify_local`` only re-checks ``name`` under the held ``root_fd``
    (used for best-effort cleanup of our own temp through ``fd``).
    The stat→operation micro-window inherent to POSIX remains (same as
    ``PreparedMediaFile``).
    """

    root_fd: int
    fd: int
    name: str
    path: Path
    identity: tuple[int, int]
    root_steps: tuple[_RootStep, ...]
    root_identity: tuple[int, int]
    replay_root: Callable[[tuple[_RootStep, ...]], int]
    _closed: bool = False

    def verify(self) -> None:
        fresh = self.replay_root(self.root_steps)
        try:
            root = os.fstat(fresh)
            if (root.st_dev, root.st_ino) != self.root_identity:
                raise StoragePathError("App Library root changed during placement")
            self._check_entry(fresh)
        finally:
            try:
                os.close(fresh)
            except OSError:
                pass

    def verify_local(self) -> None:
        self._check_entry(self.root_fd)

    def _check_entry(self, root_fd: int) -> None:
        try:
            entry = os.stat(self.name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise StoragePathError(f"publish directory vanished: {self.name!r}") from exc
        if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != self.identity:
            raise StoragePathError(f"publish directory changed: {self.name!r}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self.fd, self.root_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class StorageService:
    def __init__(
        self,
        staging_root: Path,
        library_root: Path,
        *,
        plex_music_root: Path | None = None,
        plex_video_root: Path | None = None,
    ) -> None:
        # The constructor never touches the filesystem: delete roots are
        # only trusted after initialize_delete_roots() captured their
        # identities (fail-closed before that, no cached descriptors).
        self._staging_root = staging_root
        self._library_root = library_root
        self._plex_music_root = plex_music_root
        self._plex_video_root = plex_video_root
        self._delete_roots: dict[str, _DeleteRoot] = {}
        # Startup staging-root snapshot for the Q3 Clear History check;
        # ``None`` until initialize_staging_root() captured it.
        self._staging_snapshot: _DeleteRoot | None = None

    @property
    def library_root(self) -> Path:
        return self._library_root

    def ensure_layout(self) -> None:
        self._staging_root.mkdir(parents=True, exist_ok=True)
        (self._library_root / "audio").mkdir(parents=True, exist_ok=True)
        (self._library_root / "video").mkdir(parents=True, exist_ok=True)

    def job_staging_dir(self, job_id: str) -> Path:
        job_id = self._validated_job_id(job_id)
        staging_dir = ensure_within_root(self._staging_root, self._staging_root / job_id)
        staging_dir.mkdir(parents=True, exist_ok=True)
        return staging_dir

    def remove_staging(self, job_id: str) -> None:
        job_id = self._validated_job_id(job_id)
        staging_dir = ensure_within_root(self._staging_root, self._staging_root / job_id)
        shutil.rmtree(staging_dir, ignore_errors=True)

    @staticmethod
    def _validated_job_id(job_id: str) -> str:
        """Reject path-shaped job ids before any staging filesystem call.

        Application-generated job ids are ``uuid4().hex`` values; this guard
        is defense-in-depth (ARCHITECTURE.md §49) so no input can make a
        staging operation resolve to the staging root itself or below it
        through ``/``, ``\\``, traversal segments, or absolute paths.
        """
        if (
            not job_id
            or job_id in {".", ".."}
            or "/" in job_id
            or "\\" in job_id
            or Path(job_id).name != job_id
        ):
            raise ValueError(f"invalid job id: {job_id!r}")
        return job_id

    def final_target(
        self, media_kind: str, filename: str, *, folders: tuple[str, ...] = ()
    ) -> Path:
        """Plan an App Library target ``media_kind/*folders/filename``.

        Backend-only planning: each folder is sanitized as exactly one
        segment (path-shaped names never add levels; ``.``/``..`` fall
        back) and the filename keeps its extension. No metadata or layout
        is interpreted here; callers omit folders for missing metadata
        instead of passing blanks. Nothing is written and no directory is
        created. The ``ensure_within_root`` check happens at resolve time
        only and is no defense against concurrent mutation (TOCTOU):
        before nested publication is activated, the writer must verify
        containment safely again at write time. With ``folders=()`` the
        result is the existing flat target.
        """
        if media_kind not in {"audio", "video"}:
            raise ValueError(f"unsupported media kind: {media_kind}")
        if not isinstance(folders, tuple):
            raise TypeError(f"folders must be a tuple: {folders!r}")
        segments: list[str] = []
        for folder in folders:
            if not isinstance(folder, str):
                raise TypeError(f"folder must be a str: {folder!r}")
            if not folder.strip():
                raise ValueError(f"folder must not be blank: {folder!r}")
            segments.append(sanitize_path_segment(folder))
        safe_filename = sanitize_media_filename(filename)
        candidate = self._library_root.joinpath(media_kind, *segments, safe_filename)
        return ensure_within_root(self._library_root, candidate)

    @staticmethod
    def _validate_video_short_edge(value: int | None, media_kind: str) -> None:
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"video_short_edge must be an int: {value!r}")
        if value <= 0:
            raise ValueError(f"video_short_edge must be positive: {value!r}")
        if media_kind != "video":
            raise ValueError(f"video_short_edge is only for video: {media_kind!r}")

    @staticmethod
    def _candidate_names(
        media_kind: str,
        title: str,
        extension: str,
        *,
        source_order: int | None = None,
        video_short_edge: int | None = None,
    ) -> list[str]:
        """Ordered flat no-clobber leaf names ``[NN - ]Title[ (Np)][ (n)].ext``.

        Names are single segments published only relative to the verified
        ``media_kind`` directory descriptor (``_open_publish_dir``); a leaf
        is never resolved or followed, so an existing leaf symlink (normal,
        dangling or escaping) or directory is just an occupied candidate
        (``EEXIST``).
        """
        if media_kind not in {"audio", "video"}:
            raise ValueError(f"unsupported media kind: {media_kind}")
        prefix = "" if source_order is None else f"{source_order:02d} - "
        qualifier = "" if video_short_edge is None else f" ({video_short_edge}p)"
        return conflict_media_filenames(
            title, extension, prefix=prefix, qualifier=qualifier
        )

    async def place_final(
        self,
        source: Path,
        *,
        media_kind: str,
        title: str,
        cancel_requested: Callable[[], bool] | None = None,
        publish_guard: AbstractContextManager[Any] | None = None,
        video_short_edge: int | None = None,
        staging_key: str | None = None,
        source_identity: tuple[int, int] | None = None,
    ) -> PlacementResult:
        """Move finalized media off the event loop without overwriting a target.

        ``staging_key`` is the runner's own staging key: the source must be a
        regular file inside that staging directory, pinned with the same
        no-follow descriptor walk as child placement, and re-verified before
        every publish attempt and before its unlink. The job runner always
        passes it. ``None`` only pins the given leaf (no ownership check) for
        legacy direct callers; production code must not rely on it.

        ``source_identity`` (requires ``staging_key``) binds the pin to an
        already verified inode, e.g. ``StagedEditResult.identity``: the fresh
        walk re-checks every parent from the staging root, and a different
        inode at the source name fails closed with
        ``MediaFileTargetChangedError`` before anything is published or
        unlinked. The caller must keep that inode pinned open until this
        returns, so its number cannot be reused by a replacement.

        ``video_short_edge`` (video only) is the measured short edge of the
        staged file; it names the output ``Title (720p).ext``. ``None``
        (unmeasured, or audio) adds no qualifier. A collision publishes the
        next free numbered candidate; exhaustion raises ``OSError``.

        ``publish_guard`` linearizes each atomic publish attempt against an
        external cancellation signal: the cancel side sets its event while
        holding the same guard, so either cancellation is observed before the
        file becomes visible or the publish has already happened.
        """
        if staging_key is not None:
            staging_key = self._validated_job_id(staging_key)
        elif source_identity is not None:
            raise ValueError("source_identity requires staging_key")
        self._validate_video_short_edge(video_short_edge, media_kind)
        extension = source.suffix.lstrip(".") or "bin"
        names = self._candidate_names(
            media_kind, title, extension, video_short_edge=video_short_edge
        )
        operation = asyncio.create_task(
            asyncio.to_thread(
                self._place_final_sync,
                source,
                staging_key,
                media_kind,
                names,
                cancel_requested or (lambda: False),
                publish_guard,
                source_identity,
            )
        )
        # Once finalization starts, let its atomic publish finish so callers can
        # persist a result consistent with the filesystem during shutdown.
        while True:
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None:
                    current_task.uncancel()

    async def place_child_final(
        self,
        child_staging_key: str,
        adapter_output_path: Path,
        *,
        media_kind: str,
        title: str,
        cancel_requested: Callable[[], bool] | None = None,
        publish_guard: AbstractContextManager[Any] | None = None,
        source_order: int | None = None,
        video_short_edge: int | None = None,
        source_identity: tuple[int, int] | None = None,
    ) -> PlacementResult:
        """Place only a regular output owned by the backend child's staging key.

        ``source_order`` (video only) is the persisted collection position;
        it prefixes the filename as ``NN - Title.ext`` with at least two
        digits, never truncated or compacted (PRODUCT_SPEC.md §15). With
        ``None`` the flat ``Title.ext`` target is unchanged.
        ``video_short_edge`` (video only) adds the measured ``(720p)``
        qualifier; a collision publishes the next free numbered candidate.

        ``source_identity`` binds the no-follow pin to an already verified
        inode (``StagedEditResult.identity``), as single placement does: a
        different inode at the source name fails closed with
        ``MediaFileTargetChangedError`` before any copy or publish, and is
        never unlinked. The caller keeps that inode pinned until this returns.

        Source parents and file are pinned with no-follow descriptors. Always
        copy from the pinned file (also on the same filesystem): portable POSIX
        has no descriptor-based hard-link primitive. Never reopen the adapter
        path for copying; the temporary copy is made once, descriptor-relative
        in the verified target directory, and reused for every candidate.
        Every publish attempt retains the placement cancel gate and the
        pinned-source recheck; the catalog identity/size are the temporary
        copy's own inode facts, verified no-follow at the target after link.
        The worker owns all fds and temporary files; repeated waiter
        cancellation drains it as single placement does, rather than
        orphaning cleanup.

        Parent-name swaps cannot redirect reads/unlinks. Detectable leaf swaps
        fail closed before publication and unlink. As with PreparedMediaFile,
        POSIX's same-parent stat/unlink micro-window remains; pinned inodes may
        also be modified in place by a concurrent writer. This is not a claim
        of protection against arbitrary concurrent filesystem mutation.
        """
        child_staging_key = self._validated_job_id(child_staging_key)
        if source_order is not None:
            if isinstance(source_order, bool) or not isinstance(source_order, int):
                raise TypeError(f"source_order must be an int: {source_order!r}")
            if source_order <= 0:
                raise ValueError(f"source_order must be positive: {source_order!r}")
            if media_kind != "video":
                raise ValueError(f"source_order is only for video: {media_kind!r}")
        self._validate_video_short_edge(video_short_edge, media_kind)
        operation = asyncio.create_task(asyncio.to_thread(
            self._place_child_final_sync,
            child_staging_key, adapter_output_path, media_kind, title,
            cancel_requested or (lambda: False), publish_guard,
            source_order=source_order, video_short_edge=video_short_edge,
            source_identity=source_identity,
        ))
        while True:
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None:
                    current_task.uncancel()

    def _prepare_child_output(self, key: str, output: Path) -> PreparedMediaFile:
        # Lexical containment first: resolving the untrusted output would hide
        # traversal and symlink components. Relative paths use the same cwd
        # interpretation as the adapter's existing Path output.
        if ".." in output.parts or "\\" in str(output) or "\x00" in str(output):
            raise StoragePathError("invalid child output path")
        # The root may be named as configured or resolved: job_staging_dir()
        # hands out resolved paths, so an aliased root must still match. Only
        # the root is resolved; the output stays lexical for the no-follow walk.
        # Resolve once: the descriptor capture below must verify the very root
        # that was matched, so an alias swapped in between fails closed.
        resolved_root = self._staging_root.resolve()
        absolute_output = output.absolute()
        for root in (self._staging_root.absolute(), resolved_root):
            try:
                parts = absolute_output.relative_to(root / key).parts
                break
            except ValueError:
                continue
        else:
            raise StoragePathError("output is outside child staging")
        if not parts:
            raise StoragePathError("output must name a child file")
        # Only configured root aliases (e.g. macOS /var) may be expanded;
        # child and adapter-supplied parents are strictly no-follow.
        parent_fd, _ = self._capture_root_descriptor(self._staging_root, resolved_root)
        pinned_fd: int | None = None
        try:
            for component in (key, *parts[:-1]):
                try:
                    next_fd = os.open(
                        component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                except OSError as exc:
                    raise _component_open_error(component, exc) from exc
                os.close(parent_fd)
                parent_fd = next_fd
            try:
                pinned_fd = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise _final_open_error(parts[-1], exc) from exc
            entry = os.fstat(pinned_fd)
            if not stat.S_ISREG(entry.st_mode):
                raise MediaFileNotRegularError(str(output))
            return PreparedMediaFile(
                parent_fd, pinned_fd, parts[-1], output,
                (entry.st_dev, entry.st_ino),
            )
        except BaseException:
            if pinned_fd is not None:
                os.close(pinned_fd)
            os.close(parent_fd)
            raise

    def is_staged_regular_file(self, key: str, output: Path) -> bool:
        """Read-only precheck: ``output`` is a regular file in staging ``key``.

        Reuses the descriptor walk of child placement (no-follow on every
        directory component and the leaf, ``O_NONBLOCK`` so a FIFO cannot
        block). Any rejection or error is ``False``; nothing is mutated and
        no descriptor outlives the call. This is only a precheck: a consumer
        that reopens the path (e.g. a probe subprocess) is not pinned to the
        inspected inode, so a same-directory swap in the micro-window after
        this check remains possible in the private staging directory.
        """
        try:
            prepared = self._prepare_child_output(self._validated_job_id(key), output)
        except (
            ValueError, OSError, MediaFileMissingError, MediaFileNotRegularError,
            StorageDeleteFailedError,
        ):
            return False
        prepared.close()
        return True

    async def stage_edited_copy(
        self, staging_key: str, source: Path, edit: Callable[[BinaryIO], bool],
    ) -> StagedEditResult:
        """Copy a staged file and let ``edit`` change only the copy (§14).

        ``source`` must be a regular file in ``staging_key``'s own staging,
        pinned with the child-placement no-follow walk. The copy is created
        ``O_CREAT | O_EXCL | O_NOFOLLOW`` under the pinned source directory
        with the same suffix and filled from the pinned descriptor: a genuine
        copy, never a hard link, so the source bytes are never mutated.
        ``edit`` gets the copy as a seekable ``r+b`` handle it must not close
        and returns ``True`` when the copy changed and should be kept. Any
        exception it raises is a genuine failure; storage interprets nothing.

        Afterwards, on both outcomes, the source entry must still be the pinned
        inode and the copy name still the created one; detectable swaps fail
        closed. On ``False`` or any error the copy is removed under the held
        directory, only while it still holds the created (still pinned) inode
        (an unowned entry is never unlinked; that raises
        ``MediaFileTargetChangedError``). A removal error replaces the original
        error, so it is never reported as an edit outcome.

        The result keeps the chosen inode pinned: the edited copy on ``True``,
        the rechecked original source on ``False``. The caller passes
        ``result.path``/``result.identity`` to ``place_final`` and calls
        ``discard`` once placement and cleanup are finished. Every descriptor
        not transferred to the result is closed on every exit.

        The blocking worker owns all descriptors and the copy. Repeated waiter
        cancellation waits for it to finish, discards whichever result emerged
        and then re-raises ``CancelledError``. Pinned inodes may still be
        modified in place by a concurrent writer; this is not a guarantee
        against arbitrary filesystem mutation.
        """
        staging_key = self._validated_job_id(staging_key)
        operation = asyncio.ensure_future(asyncio.to_thread(
            self._stage_edited_copy_sync, staging_key, source, edit,
        ))
        cancelled = False
        while not operation.done():
            try:
                # ``wait`` never cancels the worker task itself.
                await asyncio.wait((operation,))
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            if operation.exception() is None:
                operation.result().discard()
            raise asyncio.CancelledError
        return operation.result()

    def _stage_edited_copy_sync(
        self, key: str, source_path: Path, edit: Callable[[BinaryIO], bool],
    ) -> StagedEditResult:
        source = self._prepare_child_output(key, source_path)
        transferred = False
        try:
            copy = self._edit_pinned_copy(source, edit)
            if copy is not None:
                return StagedEditResult(copy.resolved_path, True, copy)
            # Nothing kept: the rechecked original stays pinned for placement.
            transferred = True
            return StagedEditResult(source.resolved_path, False, source)
        finally:
            if not transferred:
                source.close()

    def _edit_pinned_copy(
        self, source: PreparedMediaFile, edit: Callable[[BinaryIO], bool],
    ) -> PreparedMediaFile | None:
        """The verified edited copy, pinned; ``None`` when nothing was kept."""
        parent_fd = os.dup(source._parent_fd)
        pin_fd: int | None = None
        owned = True
        try:
            suffix = source.resolved_path.suffix
            for _ in range(16):
                name = f".edit-{secrets.token_hex(8)}{suffix}"
                try:
                    fd = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600, dir_fd=parent_fd,
                    )
                except FileExistsError:
                    continue
                break
            else:
                raise FileExistsError(errno.EEXIST, "no free temporary name")
            try:
                # A second descriptor keeps the copy's inode alive (no number
                # reuse) through every identity check, placement and discard.
                pin_fd = os.dup(fd)
                created = os.fstat(pin_fd)
            except BaseException:
                # Unverifiable ownership: the name is left, never unlinked.
                os.close(fd)
                raise
            identity = (created.st_dev, created.st_ino)
            try:
                # fdopen owns the descriptor even when copying fails.
                with os.fdopen(fd, "r+b") as handle:
                    pinned = os.fstat(source._pinned_fd)
                    with os.fdopen(os.dup(source._pinned_fd), "rb") as original:
                        original.seek(0)
                        shutil.copyfileobj(original, handle)
                    handle.flush()
                    if os.fstat(handle.fileno()).st_size != pinned.st_size:
                        raise OSError(errno.EIO, "edit copy size mismatch")
                    os.fchmod(handle.fileno(), stat.S_IMODE(pinned.st_mode))
                    handle.seek(0)
                    keep = edit(handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                # Detectable swaps of the source or the copy fail closed.
                self._check_child_entry(source)
                if self._entry_identity(name, parent_fd) != identity:
                    raise MediaFileTargetChangedError(name)
            except BaseException:
                self._remove_own_copy(parent_fd, name, identity)
                raise
            if not keep:
                self._remove_own_copy(parent_fd, name, identity)
                return None
            assert pin_fd is not None
            copy = PreparedMediaFile(
                parent_fd, pin_fd, name, source.resolved_path.parent / name, identity
            )
            owned = False  # both descriptors transferred to the pinned copy
            return copy
        finally:
            if owned:
                for held in (pin_fd, parent_fd):
                    if held is not None:
                        try:
                            os.close(held)
                        except OSError:
                            pass

    @staticmethod
    def _remove_own_copy(dir_fd: int, name: str, identity: tuple[int, int]) -> None:
        """Unlink ``name`` only while it still holds ``identity``; errors raise."""
        try:
            entry = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(entry.st_mode) or (entry.st_dev, entry.st_ino) != identity:
            raise MediaFileTargetChangedError(name)
        os.unlink(name, dir_fd=dir_fd)

    @staticmethod
    def _check_child_entry(source: PreparedMediaFile) -> None:
        entry = os.stat(
            source.final_name, dir_fd=source._parent_fd, follow_symlinks=False
        )
        if not stat.S_ISREG(entry.st_mode):
            raise MediaFileNotRegularError(str(source.resolved_path))
        if (entry.st_dev, entry.st_ino) != source.identity:
            raise MediaFileTargetChangedError(str(source.resolved_path))

    def _pin_unowned_source(self, source: Path) -> PreparedMediaFile:
        """Legacy pin for a caller-trusted path (no staging ownership check).

        The leaf is opened ``O_NOFOLLOW | O_NONBLOCK`` relative to its parent
        descriptor and must be a regular file; publication still rechecks
        and unlinks it descriptor-relative under the same identity rules.
        """
        if not source.name or source.name in {".", ".."}:
            raise StoragePathError("source must name a file")
        try:
            parent_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise _component_open_error(str(source.parent), exc) from exc
        pinned_fd: int | None = None
        try:
            try:
                pinned_fd = os.open(
                    source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise _final_open_error(source.name, exc) from exc
            entry = os.fstat(pinned_fd)
            if not stat.S_ISREG(entry.st_mode):
                raise MediaFileNotRegularError(str(source))
            return PreparedMediaFile(
                parent_fd, pinned_fd, source.name, source, (entry.st_dev, entry.st_ino)
            )
        except BaseException:
            if pinned_fd is not None:
                os.close(pinned_fd)
            os.close(parent_fd)
            raise

    def _open_publish_dir(self, media_kind: str) -> _PublishDir:
        """Open the flat ``media_kind`` directory through a verified root fd.

        The App Library root is the startup-captured delete root when
        available (identity re-checked), otherwise captured now with the
        same alias-aware no-follow walk. ``media_kind`` itself must be a
        real directory opened ``O_NOFOLLOW`` (a symlinked media directory,
        even one pointing inside the root, is rejected fail-closed). All
        temporary files, links and cleanup are relative to this descriptor;
        no target path is ever re-resolved. The root steps and identity used
        here are kept for ``_PublishDir.verify`` replays.
        """
        app_library = MediaStorageType.APP_LIBRARY.value
        captured = self._delete_roots.get(app_library)
        if captured is not None:
            root_fd, root_path = self._open_validated_root(app_library)
            root_steps, root_identity = captured.steps, captured.identity
        else:
            self._library_root.mkdir(parents=True, exist_ok=True)
            root_path = self._library_root.resolve()
            root_fd, root_steps = self._capture_root_descriptor(
                self._library_root, root_path
            )
            try:
                root_stat = os.fstat(root_fd)
            except BaseException:
                os.close(root_fd)
                raise
            root_identity = (root_stat.st_dev, root_stat.st_ino)
        try:
            try:
                os.mkdir(media_kind, 0o755, dir_fd=root_fd)
            except FileExistsError:
                pass
            try:
                fd = os.open(
                    media_kind, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise StoragePathError(
                        f"publish directory is not a real directory: {media_kind!r}"
                    ) from exc
                raise
            try:
                entry = os.fstat(fd)
            except BaseException:
                os.close(fd)
                raise
            return _PublishDir(
                root_fd, fd, media_kind, root_path / media_kind,
                (entry.st_dev, entry.st_ino), root_steps, root_identity,
                self._open_root_descriptor,
            )
        except BaseException:
            os.close(root_fd)
            raise

    @staticmethod
    def _temp_name(first: str) -> str:
        return f".{first}.{secrets.token_hex(8)}.part"

    @staticmethod
    def _entry_identity(name: str, dir_fd: int) -> tuple[int, int] | None:
        """No-follow regular-file identity of ``name``; ``None`` otherwise."""
        try:
            entry = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(entry.st_mode):
            return None
        return (entry.st_dev, entry.st_ino)

    def _link_source_to_temp(
        self, source: PreparedMediaFile, publish: _PublishDir, first: str,
    ) -> tuple[str, tuple[int, int] | None]:
        """Hard-link the pinned source name to a hidden temp; ``EXDEV`` raises.

        Returns the temp name and the identity it actually got. The caller
        must compare it with the pinned identity: a staging swap between pin
        and link is detected before anything becomes visible.
        """
        for _ in range(16):
            name = self._temp_name(first)
            try:
                os.link(
                    source.final_name, name, src_dir_fd=source._parent_fd,
                    dst_dir_fd=publish.fd, follow_symlinks=False,
                )
            except FileExistsError:
                continue
            return name, self._entry_identity(name, publish.fd)
        raise FileExistsError(errno.EEXIST, "no free temporary name")

    def _copy_source_to_temp(
        self, source: PreparedMediaFile, publish: _PublishDir, first: str,
        on_created: Callable[[str, tuple[int, int]], None],
    ) -> tuple[int, int]:
        """Copy the pinned source fd into a hidden temp; returns ``(dev, ino)``.

        The temp is created ``O_CREAT | O_EXCL | O_NOFOLLOW`` relative to the
        verified directory; ``on_created`` registers it for cleanup before
        any byte is copied. The adapter path is never reopened.
        """
        for _ in range(16):
            name = self._temp_name(first)
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600, dir_fd=publish.fd,
                )
            except FileExistsError:
                continue
            break
        else:
            raise FileExistsError(errno.EEXIST, "no free temporary name")
        # fdopen owns the descriptor even when copying fails.
        with os.fdopen(fd, "wb") as copied:
            created = os.fstat(copied.fileno())
            identity = (created.st_dev, created.st_ino)
            on_created(name, identity)
            with os.fdopen(os.dup(source._pinned_fd), "rb") as original:
                shutil.copyfileobj(original, copied)
            copied.flush()
            if os.fstat(copied.fileno()).st_size != os.fstat(source._pinned_fd).st_size:
                raise OSError("output copy size mismatch")
            os.fchmod(copied.fileno(), 0o644)
            os.fsync(copied.fileno())
        return identity

    @staticmethod
    def _cleanup_temp(
        publish: _PublishDir, name: str, identity: tuple[int, int]
    ) -> None:
        """Remove our own temp name, never through a changed directory.

        Best effort and descriptor-relative to the held directory the temp
        was created in: skipped when that directory no longer is the media
        entry under the held root descriptor or the name no longer holds the
        inode we created. The root is not replayed here, so cleanup never
        reaches a new root or a published name (no rollback unlink).
        """
        try:
            publish.verify_local()
            if StorageService._entry_identity(name, publish.fd) == identity:
                os.unlink(name, dir_fd=publish.fd)
        except (OSError, StoragePathError):
            pass

    def _publish_pinned(
        self,
        source: PreparedMediaFile,
        media_kind: str,
        names: Sequence[str],
        cancel_requested: Callable[[], bool],
        publish_guard: AbstractContextManager[Any] | None,
        *,
        allow_link: bool,
    ) -> PlacementResult:
        """Publish a pinned source at the first free name, never clobbering.

        A hidden temp holding exactly one known inode is prepared first:
        with ``allow_link`` a hard link of the pinned source name (verified
        to be the pinned inode), else — or on ``EXDEV`` — one copy from the
        pinned descriptor. That inode's ``(st_dev, st_ino)`` and size are the
        catalog facts. Each attempt holds the publish guard and re-checks
        cancellation, the publish directory (root snapshot replay), the
        pinned source entry and the temp before an atomic descriptor-relative
        ``link``; only ``EEXIST`` advances. After a link the target is
        verified no-follow against the expected inode and, after the gate,
        the root/media directory is replayed again before the source is
        unlinked; a mismatch fails
        closed (no catalog facts, source kept; the published name is left,
        never auto-deleted). Exhaustion raises ``FileExistsError``.
        """
        publish = self._open_publish_dir(media_kind)
        temp: tuple[str, tuple[int, int]] | None = None

        def register(name: str, identity: tuple[int, int]) -> None:
            nonlocal temp
            temp = (name, identity)

        try:
            self._raise_if_cancelled(cancel_requested)
            publish.verify()
            expected: tuple[int, int] | None = None
            size = os.fstat(source._pinned_fd).st_size
            if allow_link:
                try:
                    name, linked = self._link_source_to_temp(source, publish, names[0])
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                else:
                    if linked is not None:
                        register(name, linked)
                    if linked != source.identity:
                        # Staging leaf swapped between pin and link: the new
                        # inode is never published (temp removed) nor deleted.
                        raise MediaFileTargetChangedError(str(source.resolved_path))
                    expected = source.identity
            if expected is None:
                expected = self._copy_source_to_temp(source, publish, names[0], register)
            assert temp is not None
            temp_name = temp[0]
            published = self._link_temp_first_free(
                source, publish, temp_name, expected, names,
                cancel_requested, publish_guard,
            )
            # After the gate: the root/media directory must still be the
            # verified one right before the staged source is unlinked. On a
            # change the published name and the source both stay.
            publish.verify()
            # The visible target must still hold the expected inode (no-follow)
            # after the gate too; vanished/replaced keeps the staged source.
            if self._entry_identity(published, publish.fd) != expected:
                raise MediaFileTargetChangedError(str(publish.path / published))
            source.delete()  # descriptor-relative identity recheck + unlink
            return PlacementResult(
                final_path=publish.path / published,
                relative_path=f"{media_kind}/{published}",
                size_bytes=size,
                identity=expected,
            )
        finally:
            if temp is not None:
                self._cleanup_temp(publish, *temp)
            publish.close()

    def _link_temp_first_free(
        self,
        source: PreparedMediaFile,
        publish: _PublishDir,
        temp_name: str,
        expected: tuple[int, int],
        names: Sequence[str],
        cancel_requested: Callable[[], bool],
        publish_guard: AbstractContextManager[Any] | None,
    ) -> str:
        guard = publish_guard if publish_guard is not None else nullcontext()
        for name in names:
            with guard:
                self._raise_if_cancelled(cancel_requested)
                publish.verify()
                self._check_child_entry(source)
                if self._entry_identity(temp_name, publish.fd) != expected:
                    raise MediaFileTargetChangedError(temp_name)
                try:
                    os.link(
                        temp_name, name, src_dir_fd=publish.fd,
                        dst_dir_fd=publish.fd, follow_symlinks=False,
                    )
                except FileExistsError:
                    continue
                # Verify what is actually at the published name, no-follow.
                if self._entry_identity(name, publish.fd) != expected:
                    raise MediaFileTargetChangedError(str(publish.path / name))
                # The root replay happens before the source unlink, after
                # the gate (``_publish_pinned``).
                return name
        raise FileExistsError(
            errno.EEXIST, f"no free filename among {len(names)} candidates"
        )

    def _place_child_final_sync(
        self, key: str, output: Path, media_kind: str, title: str,
        cancel_requested: Callable[[], bool],
        publish_guard: AbstractContextManager[Any] | None,
        source_order: int | None = None,
        video_short_edge: int | None = None,
        source_identity: tuple[int, int] | None = None,
    ) -> PlacementResult:
        source = self._prepare_child_output(key, output)
        try:
            if source_identity is not None and source.identity != source_identity:
                # Handoff check: the verified, still pinned inode must be the
                # one copied and published; a replacement is never unlinked.
                raise MediaFileTargetChangedError(str(output))
            extension = output.suffix.lstrip(".") or "bin"
            names = self._candidate_names(
                media_kind, title, extension,
                source_order=source_order, video_short_edge=video_short_edge,
            )
            # Always copy from the pinned fd (no portable fd hard link).
            return self._publish_pinned(
                source, media_kind, names, cancel_requested, publish_guard,
                allow_link=False,
            )
        finally:
            source.close()

    def _place_final_sync(
        self,
        source_path: Path,
        staging_key: str | None,
        media_kind: str,
        names: Sequence[str],
        cancel_requested: Callable[[], bool],
        publish_guard: AbstractContextManager[Any] | None = None,
        source_identity: tuple[int, int] | None = None,
    ) -> PlacementResult:
        source = (
            self._pin_unowned_source(source_path) if staging_key is None
            else self._prepare_child_output(staging_key, source_path)
        )
        try:
            if source_identity is not None and source.identity != source_identity:
                # Handoff check: the verified copy must be the inode published.
                raise MediaFileTargetChangedError(str(source_path))
            # Same filesystem: link the verified source inode; EXDEV: one copy
            # from the pinned descriptor.
            return self._publish_pinned(
                source, media_kind, names, cancel_requested, publish_guard,
                allow_link=True,
            )
        finally:
            source.close()

    @staticmethod
    def _raise_if_cancelled(cancel_requested: Callable[[], bool]) -> None:
        if cancel_requested():
            raise PlacementCancelled

    def storage_root_for(self, storage_type: str) -> Path:
        """Map a catalog storage type to its configured root, fail-closed.

        Only App Library and *configured* Plex roots are ever reachable
        (ARCHITECTURE.md §37, §49): an unconfigured or unknown storage
        mapping raises instead of offering any filesystem fallback.
        """
        if storage_type == MediaStorageType.APP_LIBRARY.value:
            return self._library_root
        if storage_type == MediaStorageType.PLEX_MUSIC.value:
            if self._plex_music_root is None:
                raise StorageTargetError(
                    f"storage type has no configured root: {storage_type!r}"
                )
            return self._plex_music_root
        if storage_type == MediaStorageType.PLEX_VIDEO.value:
            if self._plex_video_root is None:
                raise StorageTargetError(
                    f"storage type has no configured root: {storage_type!r}"
                )
            return self._plex_video_root
        raise StorageTargetError(f"unsupported storage type: {storage_type!r}")

    def initialize_delete_roots(self) -> None:
        """Capture delete-root identities once, after ``ensure_layout``.

        The App Library root is mandatory — an unreachable or filesystem-
        root configuration raises here. Configured Plex roots are captured
        only when they exist, so a missing optional root never takes
        startup down: it simply stays unavailable and every deletion
        against it fails closed. Identities are ``(st_dev, st_ino)``
        pairs; no descriptors are cached, every operation re-walks and
        re-checks (ARCHITECTURE.md §38, §49).
        """
        roots: dict[str, _DeleteRoot] = {}
        for storage_type in (
            MediaStorageType.APP_LIBRARY.value,
            MediaStorageType.PLEX_MUSIC.value,
            MediaStorageType.PLEX_VIDEO.value,
        ):
            try:
                root = self.storage_root_for(storage_type)
            except StorageTargetError:
                continue  # unconfigured optional root stays unavailable
            try:
                resolved = root.resolve()
                fd, steps = self._capture_root_descriptor(root, resolved)
            except (StorageTargetError, StoragePathError, StorageDeleteFailedError):
                if storage_type == MediaStorageType.APP_LIBRARY.value:
                    raise
                continue  # missing/unsafe optional root stays unavailable
            try:
                root_stat = os.fstat(fd)
            except OSError as exc:
                if storage_type == MediaStorageType.APP_LIBRARY.value:
                    raise StorageDeleteFailedError(str(exc)) from exc
                continue
            finally:
                os.close(fd)
            roots[storage_type] = _DeleteRoot(
                resolved, steps, (root_stat.st_dev, root_stat.st_ino)
            )
        self._delete_roots = roots

    def initialize_staging_root(self) -> None:
        """Capture the staging root's identity once, after ``ensure_layout``.

        Same pattern as the App Library delete root: the alias-aware lexical
        walk (``_capture_root_descriptor``) plus ``(st_dev, st_ino)``. The
        staging root is mandatory: any capture failure raises its typed
        storage error so startup fails closed. No descriptor is cached and
        nothing is created, moved or removed here. Only the Q3 Clear History
        check (``verify_staging_root_for_clear_history``) uses this snapshot;
        existing staging producers and cleanup are unchanged.
        """
        self._staging_snapshot = None
        resolved = self._staging_root.resolve()
        fd, steps = self._capture_root_descriptor(self._staging_root, resolved)
        try:
            root_stat = os.fstat(fd)
        except OSError as exc:
            raise StorageDeleteFailedError(str(exc)) from exc
        finally:
            os.close(fd)
        self._staging_snapshot = _DeleteRoot(
            resolved, steps, (root_stat.st_dev, root_stat.st_ino)
        )

    def probe_startup_writability(self) -> tuple[StartupProbeResult, ...]:
        """Nonfatal startup write check (PRODUCT_SPEC.md §23, ARCHITECTURE.md §47).

        Runs after ``initialize_delete_roots`` and ``initialize_staging_root``
        and reuses only their captures: the staging root and the App Library
        plus each *configured* Plex root (unconfigured ones are omitted). A
        configured root that was not captured (missing/unsafe at startup)
        fails as ``root_unavailable``; it is never created, re-resolved or
        probed by path. A captured root is replayed with the existing
        no-follow walker and must still show its startup identity; that
        root descriptor stays pinned for the whole probe.

        Under it a backend-generated, unpredictable hidden name is created
        ``O_CREAT | O_EXCL | O_NOFOLLOW`` with mode ``0o600`` (a collision
        retries with a fresh name a bounded number of times), one small
        payload is written (a short write fails) and the probe descriptor is
        kept open until cleanup. Only that name is unlinked, relative to the
        same root descriptor and only while it is still the created regular
        inode; a vanished or replaced entry is left untouched and reported
        ``cleanup_unverified``, never as a confirmed deletion. Nothing else
        is swept. POSIX offers no atomic verify-and-unlink, so the
        stat→unlink micro-window remains (no locking or absolute race
        guarantee).

        Each failure is a path-free result plus one path-free warning;
        success is silent. No probe failure raises: an unexpected error is
        ``probe_failed`` and the remaining roots are still probed.
        """
        targets: list[tuple[str, _DeleteRoot | None]] = [
            ("staging", self._staging_snapshot)
        ]
        for storage_type in (
            MediaStorageType.APP_LIBRARY.value,
            MediaStorageType.PLEX_MUSIC.value,
            MediaStorageType.PLEX_VIDEO.value,
        ):
            try:
                self.storage_root_for(storage_type)
            except StorageTargetError:
                continue  # unconfigured optional root: omitted
            targets.append((storage_type, self._delete_roots.get(storage_type)))
        results: list[StartupProbeResult] = []
        for target, snapshot in targets:
            try:
                reason = self._probe_captured_root(snapshot)
            except Exception:
                reason = "probe_failed"
            if reason is not None:
                logger.warning("Startup storage write check failed: %s: %s", target, reason)
            results.append(StartupProbeResult(target, reason is None, reason))
        return tuple(results)

    def _probe_captured_root(self, snapshot: _DeleteRoot | None) -> str | None:
        """Probe one captured root; ``None`` on success, else a reason token."""
        if snapshot is None:
            return "root_unavailable"
        try:
            root_fd = self._open_root_descriptor(snapshot.steps)
        except (OSError, ValueError, StorageDeleteFailedError):
            return "root_unavailable"
        try:
            try:
                root = os.fstat(root_fd)
            except OSError:
                return "root_unavailable"
            if (root.st_dev, root.st_ino) != snapshot.identity:
                return "root_changed"
            return self._write_probe(root_fd)
        finally:
            try:
                os.close(root_fd)
            except OSError:
                pass

    @staticmethod
    def _write_probe(root_fd: int) -> str | None:
        for _ in range(_PROBE_NAME_ATTEMPTS):
            name = f"{_PROBE_PREFIX}{secrets.token_hex(16)}"
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600, dir_fd=root_fd,
                )
            except FileExistsError:
                continue  # never touch the existing entry; fresh name
            except OSError:
                return "create_failed"
            break
        else:
            return "name_collision"
        closed = False
        try:
            reason = StorageService._fill_and_remove_probe(root_fd, fd, name)
        finally:
            # Closed only after cleanup: the open probe descriptor keeps the
            # inode (and its number) alive through the identity check.
            try:
                os.close(fd)
                closed = True
            except OSError:
                pass
        if reason is None and not closed:
            return "write_failed"  # deferred write error reported on close
        return reason

    @staticmethod
    def _fill_and_remove_probe(root_fd: int, fd: int, name: str) -> str | None:
        try:
            created = os.fstat(fd)
        except OSError:
            return "cleanup_failed"  # ownership unverifiable: name left
        identity = (created.st_dev, created.st_ino)
        # Ownership is verified from here on: every write outcome attempts
        # the identity-checked cleanup, and a write failure is never masked
        # by a cleanup failure (an unexpected error still propagates).
        try:
            written = os.write(fd, _PROBE_PAYLOAD)
        except OSError:
            StorageService._remove_own_probe(root_fd, name, identity)
            return "write_failed"
        except BaseException:
            try:
                StorageService._remove_own_probe(root_fd, name, identity)
            except Exception:
                pass
            raise
        if written != len(_PROBE_PAYLOAD):
            StorageService._remove_own_probe(root_fd, name, identity)
            return "write_failed"
        return StorageService._remove_own_probe(root_fd, name, identity)

    @staticmethod
    def _remove_own_probe(
        root_fd: int, name: str, identity: tuple[int, int]
    ) -> str | None:
        """Unlink our probe only while it still is ``identity``; reason token."""
        try:
            entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "cleanup_unverified"
        except OSError:
            return "cleanup_failed"
        if not stat.S_ISREG(entry.st_mode) or (entry.st_dev, entry.st_ino) != identity:
            return "cleanup_unverified"  # foreign entry: never unlinked
        try:
            os.unlink(name, dir_fd=root_fd)
        except FileNotFoundError:
            return "cleanup_unverified"
        except OSError:
            return "cleanup_failed"
        return None

    def verify_staging_root_for_clear_history(self) -> None:
        """Read-only Q3 check: the staging root still is the startup root.

        Replays the startup snapshot from ``/`` with the existing no-follow,
        alias-aware walker and compares the reached root's ``(st_dev,
        st_ino)`` with the captured identity on every call; nothing is
        re-resolved or re-captured. A missing snapshot, a failed replay
        (vanished component, new symlink, changed startup alias) or a
        different identity raises ``StagingRootVerificationError`` with a
        fixed reason and logs only that reason (no path). The single root
        descriptor exists only for this check and is always closed. This
        method never mutates the filesystem.
        """
        snapshot = self._staging_snapshot
        if snapshot is None:
            self._reject_staging_root("snapshot_missing")
        try:
            fd = self._open_root_descriptor(snapshot.steps)
        except (
            StorageTargetError, StoragePathError, StorageDeleteFailedError, OSError,
        ) as exc:
            self._reject_staging_root("replay_failed", exc)
        try:
            root_stat = os.fstat(fd)
        except OSError as exc:
            self._reject_staging_root("replay_failed", exc)
        finally:
            os.close(fd)
        if (root_stat.st_dev, root_stat.st_ino) != snapshot.identity:
            self._reject_staging_root("identity_changed")

    @staticmethod
    def _reject_staging_root(
        reason: str, cause: BaseException | None = None
    ) -> NoReturn:
        """Log a path-free reason (class/errno only) and raise the typed error."""
        if cause is None:
            logger.warning("Clear History staging root check failed: %s", reason)
        else:
            logger.warning(
                "Clear History staging root check failed: %s (%s, errno=%s)",
                reason,
                type(cause).__name__,
                getattr(cause, "errno", None),
            )
        raise StagingRootVerificationError(reason) from cause

    def remove_staging_for_clear_history(self, staging_key: str) -> bool:
        """Q3-only removal of one DB-derived single/child staging tree.

        ``staging_key`` must be a trusted, DB-derived ``download_jobs.id`` or
        ``job_items.id`` (caller trust boundary); it is still validated as
        one path segment (no NUL, separator, traversal or absolute form)
        before any filesystem call. Then, fail closed and before the first
        mutation: ``shutil.rmtree`` must avoid symlink attacks, the staging
        root must still be the startup root
        (``verify_staging_root_for_clear_history``), the static Library/Plex
        roots may not be the same as or nested with staging in either
        direction and none of their configured access chains (aliases
        included, reused from the startup capture) may pass through
        staging, all decided by ``(st_dev, st_ino)`` of the roots and their
        real ancestors, never by path text; an optional root missing at
        startup must still be normally missing. Media root and access
        component identities found inside the job tree are refused too. The
        target is the canonical startup root plus the key and is never
        resolved: a missing key returns ``False`` (idempotent), a
        top-level symlink or non-directory is refused. ``/proc/self/mountinfo``
        filesystem identity and mount roots map the staging root and every
        media access path (chain, root and mounts below the root) to their
        backing locations: a media path backed inside staging, or a media
        root backed equal to, inside or around staging on the same
        filesystem, is refused, as is an ambiguous mapping. Every mount
        point's ``(st_dev, st_ino)`` is collected (unreadable fails closed);
        the target and every entry below it must match none of them and
        stay on the root's ``st_dev``. The preflight uses ``scandir`` with
        no-follow ``stat`` and descends only into real directories, so
        symlinks are never followed, not even for metadata. Only then is the
        tree removed with one ``shutil.rmtree`` (nested symlinks are
        unlinked as names, never followed) and ``True`` is returned.

        Refusals and operational errors raise ``ClearHistoryStorageError``
        (or the root check's ``StagingRootVerificationError``) with a fixed
        reason; an error while removing, including a vanished sub-entry, is
        never reported as a missing key. External filesystem or mount
        changes during the removal are out of scope (PRODUCT_SPEC.md §17).
        """
        if (
            not isinstance(staging_key, str)
            or "\x00" in staging_key
            or not staging_key
            or staging_key in {".", ".."}
            or "/" in staging_key
            or "\\" in staging_key
            or Path(staging_key).name != staging_key
        ):
            self._reject_clear_history("invalid_key")
        if getattr(shutil.rmtree, "avoids_symlink_attacks", False) is not True:
            self._reject_clear_history("rmtree_unsafe")
        self.verify_staging_root_for_clear_history()
        snapshot = self._staging_snapshot
        assert snapshot is not None  # verified above
        staging_root = snapshot.resolved
        media, media_ids = self._check_static_roots_for_clear_history(
            staging_root, snapshot.identity
        )

        target = staging_root / staging_key
        try:
            entry = os.lstat(target)
        except FileNotFoundError:
            return False
        except OSError as exc:
            self._reject_clear_history("inspect_failed", exc)
        if not stat.S_ISDIR(entry.st_mode):
            self._reject_clear_history("target_not_directory")
        root_dev = snapshot.identity[0]
        if entry.st_dev != root_dev:
            self._reject_clear_history("device_boundary")

        try:
            raw = self._read_mountinfo()
        except OSError as exc:
            self._reject_clear_history("mountinfo_unavailable", exc)
        try:
            mounts = _parse_mountinfo(raw)
        except ClearHistoryStorageError as exc:
            self._reject_clear_history(exc.reason)

        # Sub-mounts are detected by identity, never by mount point text: a
        # mount point spelled differently still matches the entry it covers.
        # The staging root itself is not checked, so its own mount is fine.
        mount_map = self._mount_point_identities(mounts)
        self._check_bind_sources(staging_root, mounts, mount_map, media)

        def forbidden(identity: tuple[int, int]) -> None:
            if identity in mount_map:
                self._reject_clear_history("sub_mount")
            if identity in media_ids:
                # A Library/Plex root or access component inside this job.
                self._reject_clear_history("root_overlap")

        forbidden((entry.st_dev, entry.st_ino))
        pending = [target]
        try:
            while pending:
                with os.scandir(pending.pop()) as entries:
                    for item in entries:
                        info = item.stat(follow_symlinks=False)
                        if info.st_dev != root_dev:
                            self._reject_clear_history("device_boundary")
                        forbidden((info.st_dev, info.st_ino))
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(Path(item.path))
        except OSError as exc:
            self._reject_clear_history("inspect_failed", exc)

        try:
            shutil.rmtree(target)
        except OSError as exc:
            self._reject_clear_history("delete_failed", exc)
        return True

    @staticmethod
    def _read_mountinfo() -> bytes:
        """Plain read of the process mount table (Linux)."""
        with open(_MOUNTINFO_PATH, "rb") as handle:
            return handle.read()

    def _mount_point_identities(
        self, mounts: Sequence[_MountEntry]
    ) -> dict[tuple[int, int], set[Path]]:
        """``(st_dev, st_ino)`` of every visible mount point (no-follow leaf).

        Maps each identity to the mount point spellings showing it. A mount
        point shows the mounted root (directory or bind-mounted file), which
        is what the no-follow tree scan sees at that entry. Any mount point
        whose identity cannot be read fails closed with
        ``mount_unverifiable``; only a `` (deleted)`` mount, whose detached
        mount point is unreachable by any path, may be absent.
        """
        identities: dict[tuple[int, int], set[Path]] = {}
        for mount in mounts:
            for spelling in mount.spellings():
                try:
                    info = os.lstat(spelling)
                except FileNotFoundError as exc:
                    if mount.stripped is not None:
                        continue
                    self._reject_clear_history("mount_unverifiable", exc)
                except OSError as exc:
                    self._reject_clear_history("mount_unverifiable", exc)
                identities.setdefault((info.st_dev, info.st_ino), set()).add(spelling)
        return identities

    def _lstat_identity(
        self, path: str, reason: str, *, missing_ok: bool = False
    ) -> tuple[int, int] | None:
        """No-follow ``(st_dev, st_ino)`` of ``path``; errors fail closed."""
        try:
            info = os.lstat(path)
        except FileNotFoundError as exc:
            if missing_ok:
                return None
            self._reject_clear_history(reason, exc)
        except OSError as exc:
            self._reject_clear_history(reason, exc)
        return (info.st_dev, info.st_ino)

    def _real_lineage(
        self, path: Path, reason: str, *, missing_ok: bool = False,
        directory: bool = False,
    ) -> set[tuple[int, int]] | None:
        """Identities of ``path`` (no-follow leaf) and all its real ancestors.

        Ancestors are reached through the kernel's ``.`` and ``..`` from the
        directory that holds the entry, never by trimming path text, so an
        alternate spelling (letter case, aliases on the way) still yields
        the real ancestor directories up to ``/``. With ``directory`` the
        walk starts at the directory ``path`` designates (``path/.``).
        ``missing_ok`` returns ``None`` for a vanished path; any other error
        fails closed.
        """
        lineage: set[tuple[int, int]] = set()
        if directory:
            current = os.path.join(str(path), ".")
        else:
            leaf = self._lstat_identity(str(path), reason, missing_ok=missing_ok)
            if leaf is None:
                return None
            lineage.add(leaf)
            current = os.path.join(str(path.parent), ".")
        for _ in range(4096):
            here = self._lstat_identity(current, reason, missing_ok=missing_ok)
            if here is None:
                return None
            lineage.add(here)
            parent = os.path.join(current, "..")
            above = self._lstat_identity(parent, reason, missing_ok=missing_ok)
            if above is None:
                return None
            if above == here:
                return lineage
            current = parent
        self._reject_clear_history(reason)

    def _check_static_roots_for_clear_history(
        self, staging_root: Path, staging_identity: tuple[int, int]
    ) -> tuple[list[tuple[list[Path], Path | None, tuple[int, int] | None]],
               set[tuple[int, int]]]:
        """Refuse Library/Plex roots that overlap or route through staging.

        Decisions use identities, never path text: a media root equal to the
        staging root or one of its real ancestors (``/`` included), a media
        root whose real ancestry contains the staging root (inside any job),
        and any access chain component (directory or alias) located in
        staging are refused, whatever their spelling. Captured roots reuse
        the startup walk steps and must still show the captured root inode.
        A configured optional root that was not captured is probed
        read-only: normally missing outside staging is accepted, anything
        reachable now, invalid or ambiguous fails closed. Returns each media
        root's access paths, canonical root and root identity (``None`` when
        missing) for the mount backing check, and the identities of every
        media root and access component for the tree preflight.
        """
        app_library = MediaStorageType.APP_LIBRARY.value
        if app_library not in self._delete_roots:
            self._reject_clear_history("roots_unverified")
        staging_lineage = self._real_lineage(staging_root, "inspect_failed")
        assert staging_lineage is not None
        media: list[tuple[list[Path], Path | None, tuple[int, int] | None]] = []
        media_ids: set[tuple[int, int]] = set()
        for storage_type in (
            app_library,
            MediaStorageType.PLEX_MUSIC.value,
            MediaStorageType.PLEX_VIDEO.value,
        ):
            try:
                configured = self.storage_root_for(storage_type)
            except StorageTargetError:
                continue  # unconfigured optional root
            captured = self._delete_roots.get(storage_type)
            if captured is None:
                visited = self._probe_uncaptured_root(configured, staging_identity)
                for path in visited:
                    identity = self._lstat_identity(
                        str(path), "optional_root_unverifiable"
                    )
                    assert identity is not None
                    media_ids.add(identity)
                media.append((visited, None, None))
                continue
            resolved = _single_slash(str(captured.resolved))
            # The canonical path must still designate the captured root inode.
            if self._lstat_identity(
                os.path.join(str(resolved), "."), "roots_unverified"
            ) != captured.identity:
                self._reject_clear_history("roots_unverified")
            root_lineage = self._real_lineage(
                resolved, "roots_unverified", directory=True
            )
            assert root_lineage is not None
            if captured.identity in staging_lineage or staging_identity in root_lineage:
                self._reject_clear_history("root_overlap")
            media_ids.add(captured.identity)
            chain = self._chain_paths(captured.steps)
            for path in chain:
                lineage = self._real_lineage(path, "roots_unverified")
                assert lineage is not None
                if staging_identity in lineage:
                    self._reject_clear_history("alias_through_staging")
                identity = self._lstat_identity(str(path), "roots_unverified")
                assert identity is not None
                media_ids.add(identity)
            media.append((chain, resolved, captured.identity))
        return media, media_ids

    @staticmethod
    def _chain_paths(steps: tuple[_RootStep, ...]) -> list[Path]:
        """Canonical directories and alias names reached by a startup walk.

        An absolute anchor component (``/`` or Linux's ``//`` from an alias
        target) restarts at the single-slash root, so every reported path is
        comparable with the canonical staging root.
        """
        current = Path("/")
        visited: list[Path] = []
        for step in steps:
            component = step.component
            if step.alias_identity is not None:
                visited.append(current / component)
                continue
            if component.startswith("/"):
                current = Path("/")
            elif component == "..":
                current = current.parent
            else:
                current = current / component
            visited.append(current)
        return visited

    def _probe_uncaptured_root(
        self, root: Path, staging_identity: tuple[int, int]
    ) -> list[Path]:
        """Read-only lexical probe of an optional root missing at startup.

        Every existing component (directory or alias) whose real ancestry
        contains the staging root is refused by identity. Returns the
        existing directories and alias names walked before the normally
        missing component.
        """
        pending = list(root.absolute().parts[1:])
        current = Path("/")
        visited: list[Path] = [current]
        aliases = 0
        while pending:
            component = pending.pop(0)
            if component.startswith("/"):
                current = Path("/")
                continue
            if component == "..":
                current = current.parent
                continue
            candidate = current / component
            try:
                entry = os.lstat(candidate)
            except FileNotFoundError:
                return visited  # normally missing optional root
            except OSError as exc:
                self._reject_clear_history("optional_root_unverifiable", exc)
            lineage = self._real_lineage(candidate, "optional_root_unverifiable")
            assert lineage is not None
            if staging_identity in lineage:
                self._reject_clear_history("alias_through_staging")
            visited.append(candidate)
            if stat.S_ISLNK(entry.st_mode):
                aliases += 1
                if aliases > 40:
                    self._reject_clear_history("optional_root_unverifiable")
                try:
                    target = os.readlink(candidate)
                except OSError as exc:
                    self._reject_clear_history("optional_root_unverifiable", exc)
                if not target or "\x00" in target:
                    self._reject_clear_history("optional_root_unverifiable")
                pending = list(Path(target).parts) + pending
                continue
            if not stat.S_ISDIR(entry.st_mode):
                self._reject_clear_history("optional_root_unverifiable")
            current = candidate
        # Reachable now although it was not captured at startup.
        self._reject_clear_history("optional_root_unverifiable")

    def _verify_mount_spelling(
        self, path: Path, mount_map: dict[tuple[int, int], set[Path]]
    ) -> None:
        """Fail closed unless ``path`` maps onto mount points by its text.

        The mount table is matched on path text, so the text must be the
        kernel's spelling: every prefix must be a real directory (the leaf
        may be an alias), its lexical parent must be its real ``..`` parent,
        and a prefix showing a mount point's identity must be spelled as
        that mount point. Otherwise equivalence cannot be established and
        ``mount_ambiguous`` is raised.
        """
        for prefix in (path, *path.parents):
            text = str(prefix)
            try:
                info = os.lstat(text)
            except OSError as exc:
                self._reject_clear_history("mount_unverifiable", exc)
            is_dir = stat.S_ISDIR(info.st_mode)
            if prefix != path and not is_dir:
                self._reject_clear_history("mount_ambiguous")
            points = mount_map.get((info.st_dev, info.st_ino))
            if points is not None and prefix not in points:
                self._reject_clear_history("mount_ambiguous")
            if is_dir and prefix != prefix.parent:
                real_parent = self._lstat_identity(
                    os.path.join(text, ".."), "mount_unverifiable"
                )
                if real_parent != self._lstat_identity(
                    str(prefix.parent), "mount_unverifiable"
                ):
                    self._reject_clear_history("mount_ambiguous")

    def _check_bind_sources(
        self,
        staging_root: Path,
        mounts: Sequence[_MountEntry],
        mount_map: dict[tuple[int, int], set[Path]],
        media: Sequence[tuple[list[Path], Path | None, tuple[int, int] | None]],
    ) -> None:
        """Refuse media paths whose mount backing relates to staging's.

        Backing locations are ``(major:minor, path inside the filesystem)``
        from ``_mount_backing``; every mapped path must first prove its
        spelling (``_verify_mount_spelling``). On the same filesystem
        identity: any media access path backed equal to or inside the
        staging backing, a media root backed around it, and any mount whose
        real ancestry contains a media root's identity (at or below it, by
        identity) with a source root equal to, inside or around it raise
        ``bind_overlap``. Separate sibling sources (e.g. Compose binds)
        pass. Unmappable entries raise ``mount_ambiguous``.
        """

        def backing_of(path: Path) -> tuple[str, Path]:
            self._verify_mount_spelling(path, mount_map)
            try:
                return _mount_backing(path, mounts)
            except ClearHistoryStorageError as exc:
                self._reject_clear_history(exc.reason)

        device, backing = backing_of(staging_root)

        def related(other_device: str, other: Path, *, around: bool) -> bool:
            if other_device != device:
                return False
            return other.is_relative_to(backing) or (
                around and backing.is_relative_to(other)
            )

        lineages: dict[int, set[tuple[int, int]] | None] = {}

        def mount_lineage(mount: _MountEntry) -> set[tuple[int, int]]:
            if mount.mount_id not in lineages:
                found: set[tuple[int, int]] = set()
                for spelling in mount.spellings():
                    lineage = self._real_lineage(
                        spelling, "mount_unverifiable",
                        missing_ok=mount.stripped is not None,
                    )
                    found |= lineage or set()
                lineages[mount.mount_id] = found
            return lineages[mount.mount_id] or set()

        for chain, root, root_identity in media:
            for path in chain:
                if related(*backing_of(path), around=False):
                    self._reject_clear_history("bind_overlap")
            if root is None or root_identity is None:
                continue
            if related(*backing_of(root), around=True):
                self._reject_clear_history("bind_overlap")
            for mount in mounts:
                if root_identity not in mount_lineage(mount):
                    continue
                if (
                    mount.stripped is not None
                    or not mount.root.startswith("/")
                    or mount.root.endswith(_DELETED_SUFFIX)
                ):
                    self._reject_clear_history("mount_ambiguous")
                if related(mount.device, _single_slash(mount.root), around=True):
                    self._reject_clear_history("bind_overlap")

    @staticmethod
    def _reject_clear_history(
        reason: str, cause: BaseException | None = None
    ) -> NoReturn:
        """Log a path-free reason (class/errno only) and raise the typed error."""
        if cause is None:
            logger.warning("Clear History staging removal refused: %s", reason)
        else:
            logger.warning(
                "Clear History staging removal refused: %s (%s, errno=%s)",
                reason,
                type(cause).__name__,
                getattr(cause, "errno", None),
            )
        raise ClearHistoryStorageError(reason) from cause

    def _capture_root_descriptor(
        self, root: Path, resolved: Path
    ) -> tuple[int, tuple[_RootStep, ...]]:
        """Snapshot the lexical walk, including startup-only trusted aliases.

        Expand readlink targets explicitly, never by opening a symlink.
        This preserves existing startup aliases (e.g. macOS /var) while
        retaining every lexical and expanded component for later checks.
        Relative roots are anchored to the startup working directory.
        """
        if resolved.parent == resolved:
            raise StoragePathError(
                f"storage root may not be the filesystem root: {root}"
            )
        pending = list(root.absolute().parts[1:])
        steps: list[_RootStep] = []
        aliases = 0
        fd: int | None = None
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            while pending:
                component = pending.pop(0)
                try:
                    entry = os.stat(component, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISLNK(entry.st_mode):
                        aliases += 1
                        if aliases > 40:
                            raise StoragePathError("too many storage root aliases")
                        target = os.readlink(component, dir_fd=fd)
                        steps.append(_RootStep(
                            component, (entry.st_dev, entry.st_ino), target
                        ))
                        pending = list(Path(target).parts) + pending
                        continue
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=None if component == "/" else fd,
                    )
                except OSError as exc:
                    raise _root_walk_error(component, exc) from exc
                steps.append(_RootStep(component))
                os.close(fd)
                fd = next_fd
            assert fd is not None
            # Also pin the canonical walk: it must name the same root,
            # and none of its components may have become a symlink.
            canonical_steps = tuple(_RootStep(p) for p in resolved.parts)
            canonical_fd = self._open_root_descriptor(canonical_steps)
            try:
                lexical_stat, canonical_stat = os.fstat(fd), os.fstat(canonical_fd)
                if (lexical_stat.st_dev, lexical_stat.st_ino) != (
                    canonical_stat.st_dev, canonical_stat.st_ino
                ):
                    raise StoragePathError("storage root changed during capture")
            finally:
                os.close(canonical_fd)
            return fd, tuple(steps) + canonical_steps
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def _open_root_descriptor(self, steps: tuple[_RootStep, ...]) -> int:
        """Replay a startup snapshot from / without resolving current paths.

        New symlinks always fail O_NOFOLLOW. Existing aliases must retain
        both inode and link text; their saved expansion is walked instead
        of following the current link. No descriptors survive a failure.
        """
        fd: int | None = None
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for step in steps:
                component = step.component
                try:
                    if step.alias_identity is not None:
                        entry = os.stat(component, dir_fd=fd, follow_symlinks=False)
                        if (
                            not stat.S_ISLNK(entry.st_mode)
                            or (entry.st_dev, entry.st_ino) != step.alias_identity
                            or os.readlink(component, dir_fd=fd) != step.alias_target
                        ):
                            raise StoragePathError("storage root alias changed")
                        continue
                    next_fd = os.open(
                        component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=None if component == "/" else fd,
                    )
                except OSError as exc:
                    raise _root_walk_error(component, exc) from exc
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise

    def _open_validated_root(self, storage_type: str) -> tuple[int, Path]:
        """Open a delete root and verify it still is the startup root.

        Fails closed when the root was never captured (unconfigured,
        missing at startup, or before ``initialize_delete_roots``), when
        it is no longer reachable, or when its descriptor identity no
        longer matches the startup capture (root or ancestor replaced).
        """
        captured = self._delete_roots.get(storage_type)
        if captured is None:
            raise StorageTargetError(
                f"storage root is not available for deletion: {storage_type!r}"
            )
        fd = self._open_root_descriptor(captured.steps)
        resolved = captured.resolved
        try:
            root_stat = os.fstat(fd)
            if (root_stat.st_dev, root_stat.st_ino) != captured.identity:
                raise StoragePathError(
                    f"storage root identity changed since startup: {resolved}"
                )
            return fd, resolved
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def resolve_media_file(self, storage_type: str, path: str) -> ResolvedMediaFile:
        """Resolve one catalogued copy's identity under a validated root.

        Used for shared-reference scans with the same validated-root
        descriptor semantics as the deletion target: the root is
        identity-checked per operation and the catalog components are
        walked with ``O_NOFOLLOW``. A symlink anywhere on the row's path
        makes the row's real location unknowable under these semantics,
        so it fails with ``StoragePathError`` (the caller maps that to
        an unverifiable reference). Missing entries cannot hide a
        symlink redirect, so the lexical location stands with no file
        identity. Existing entries also carry descriptor-relative
        ``(st_dev, st_ino)``: different spellings/mappings can reach the
        same file despite unequal path text. ``display_path`` is never used.
        """
        parts = self._validated_catalog_parts(path)
        root_fd, root_path = self._open_validated_root(storage_type)
        try:
            parent_fd: int | None = None
            try:
                for component in parts[:-1]:
                    try:
                        next_fd = os.open(
                            component,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent_fd if parent_fd is not None else root_fd,
                        )
                    except FileNotFoundError:
                        # Nothing exists below this entry, so no symlink
                        # can redirect the row's lexical identity.
                        return ResolvedMediaFile(root_path.joinpath(*parts), None)
                    except OSError as exc:
                        raise _component_open_error(component, exc) from exc
                    if parent_fd is not None:
                        os.close(parent_fd)
                    parent_fd = next_fd
                base_fd = parent_fd if parent_fd is not None else root_fd
                try:
                    entry_stat = os.stat(
                        parts[-1], dir_fd=base_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    return ResolvedMediaFile(root_path.joinpath(*parts), None)
                except OSError as exc:
                    raise StorageDeleteFailedError(str(exc)) from exc
                if stat.S_ISLNK(entry_stat.st_mode):
                    raise StoragePathError(
                        f"final entry is a symlink, unverifiable: {parts[-1]!r}"
                    )
                return ResolvedMediaFile(
                    root_path.joinpath(*parts),
                    (entry_stat.st_dev, entry_stat.st_ino),
                )
            finally:
                if parent_fd is not None:
                    try:
                        os.close(parent_fd)
                    except OSError:
                        pass
        finally:
            os.close(root_fd)

    def try_resolve_media_files(
        self, entries: Sequence[tuple[str, str]]
    ) -> list[ResolvedMediaFile | None]:
        """Best-effort bulk resolution for shared-reference scans.

        Unresolvable mappings — unsupported or unavailable storage root
        (unconfigured, missing at startup, replaced since startup),
        containment/symlink problems on the row's path, or permission/I/O
        failures (including ``StorageDeleteFailedError``) — yield ``None``
        so the caller treats them
        as unverifiable potential references instead of silently
        skipping them. Only typed storage errors and OSError are
        absorbed; programming errors (e.g. RuntimeError) propagate.
        """
        resolved: list[ResolvedMediaFile | None] = []
        for storage_type, path in entries:
            try:
                resolved.append(self.resolve_media_file(storage_type, path))
            except (ValueError, OSError, StorageDeleteFailedError):
                resolved.append(None)
        return resolved

    @staticmethod
    def _validated_catalog_parts(path: str) -> list[str]:
        """Reject absolute, empty, and traversal-shaped catalog paths.

        The remaining components are walked descriptor-relative and
        every component must be a real directory (final entry a real
        file), so a catalogued path can never name the root itself or
        climb out of it.
        """
        catalog_path = Path(path)
        if catalog_path.is_absolute():
            raise StoragePathError(f"catalog path must be relative: {path!r}")
        parts = list(catalog_path.parts)
        if not parts or any(part in (os.curdir, os.pardir, "") for part in parts):
            raise StoragePathError(
                f"catalog path must name an entry inside the root: {path!r}"
            )
        return parts

    def prepare_media_file(self, storage_type: str, path: str) -> PreparedMediaFile:
        """Prepare one catalogued copy for deletion, fail-closed.

        Continues from an identity-checked root descriptor: every parent
        component of the catalog path is walked with ``O_DIRECTORY |
        O_NOFOLLOW`` relative to the previous descriptor (symlink
        components fail with ``ELOOP``), then the final entry is opened
        and pinned with ``O_NOFOLLOW | O_NONBLOCK`` (a final symlink is
        rejected, and a FIFO cannot block before the regular-file check).
        The returned handle carries the storage-resolved
        identity used for shared-reference scans and performs the unlink
        under its held parent descriptor, so the check and the delete
        operate on the same verified target. All temporary descriptors
        close on any failure here.
        """
        parts = self._validated_catalog_parts(path)
        root_fd, root_path = self._open_validated_root(storage_type)
        parent_fd: int | None = None
        pinned_fd: int | None = None
        try:
            for component in parts[:-1]:
                try:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent_fd if parent_fd is not None else root_fd,
                    )
                except OSError as exc:
                    raise _component_open_error(component, exc) from exc
                if parent_fd is not None:
                    os.close(parent_fd)
                parent_fd = next_fd
            if parent_fd is None:
                # Single-component catalog path: the prepared handle owns
                # its own duplicate of the root descriptor.
                parent_fd = os.dup(root_fd)
            final_name = parts[-1]
            try:
                pinned_fd = os.open(
                    final_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise _final_open_error(final_name, exc) from exc
            try:
                entry_stat = os.fstat(pinned_fd)
            except OSError as exc:
                raise StorageDeleteFailedError(str(exc)) from exc
            if not stat.S_ISREG(entry_stat.st_mode):
                raise MediaFileNotRegularError(final_name)
            prepared = PreparedMediaFile(
                _parent_fd=parent_fd,
                _pinned_fd=pinned_fd,
                final_name=final_name,
                resolved_path=root_path.joinpath(*parts),
                _identity=(entry_stat.st_dev, entry_stat.st_ino),
            )
            # Ownership transferred to the prepared handle.
            parent_fd = pinned_fd = None
            return prepared
        except BaseException:
            for fd in (pinned_fd, parent_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            raise
        finally:
            os.close(root_fd)

    def verify_media_file(
        self, storage_type: str, path: str, expected_identity: tuple[int, int]
    ) -> Path:
        """Re-verify a placed copy's physical target before a catalog commit.

        The writer half of the concurrent-delete-safe catalog protocol
        (ARCHITECTURE.md §38, §51): a transaction about to commit a
        ``media_files`` row must already hold the SQLite write lock and
        re-verify through this boundary that the target still exists and
        is still the file the placement observed. A Delete File that
        committed while the writer waited on the lock is thereby caught
        here — as a missing or replaced target — instead of leaving a
        dangling catalog row. Uses the deletion target's exact
        validated-root walk semantics (fail-closed on unavailable
        roots, symlinked/escaping paths, and non-regular targets with
        the same typed errors); an identity mismatch raises
        ``MediaFileTargetChangedError``. Read-only: the handle is only
        pinned, never unlinked, and always closed.
        """
        prepared = self.prepare_media_file(storage_type, path)
        try:
            if prepared.identity != expected_identity:
                raise MediaFileTargetChangedError(str(prepared.resolved_path))
            return prepared.resolved_path
        finally:
            prepared.close()
