"""Storage boundary: staging layout and finalization into the App Library.

All filesystem behaviour for downloads lives here (ARCHITECTURE.md §28–30).
Filename conflicts default to Skip Existing (PRODUCT_SPEC.md §16): the
existing file is never overwritten.

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
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.library import MediaStorageType
from app.utils.filenames import sanitize_media_filename
from app.utils.paths import ensure_within_root


@dataclass(slots=True)
class PlacementResult:
    final_path: Path
    relative_path: str
    skipped_existing: bool
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

    def final_target(self, media_kind: str, filename: str) -> Path:
        if media_kind not in {"audio", "video"}:
            raise ValueError(f"unsupported media kind: {media_kind}")
        safe_filename = sanitize_media_filename(filename)
        candidate = self._library_root / media_kind / safe_filename
        return ensure_within_root(self._library_root, candidate)

    async def place_final(
        self,
        source: Path,
        *,
        media_kind: str,
        title: str,
        cancel_requested: Callable[[], bool] | None = None,
        publish_guard: AbstractContextManager[Any] | None = None,
    ) -> PlacementResult:
        """Move finalized media off the event loop without overwriting a target.

        ``publish_guard`` linearizes the atomic publish against an external
        cancellation signal: the cancel side sets its event while holding the
        same guard, so either cancellation is observed before the file becomes
        visible or the publish has already happened.
        """
        extension = source.suffix.lstrip(".") or "bin"
        target = self.final_target(media_kind, f"{title}.{extension}")
        operation = asyncio.create_task(
            asyncio.to_thread(
                self._place_final_sync,
                source,
                target,
                cancel_requested or (lambda: False),
                publish_guard,
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

    def _place_final_sync(
        self,
        source: Path,
        target: Path,
        cancel_requested: Callable[[], bool],
        publish_guard: AbstractContextManager[Any] | None = None,
    ) -> PlacementResult:
        guard = publish_guard if publish_guard is not None else nullcontext()
        self._raise_if_cancelled(cancel_requested)
        if target.exists():
            return self._placement_result(target, skipped_existing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        cross_filesystem = False
        with guard:
            self._raise_if_cancelled(cancel_requested)
            try:
                os.link(source, target)
            except FileExistsError:
                return self._placement_result(target, skipped_existing=True)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                cross_filesystem = True
        if cross_filesystem:
            return self._copy_across_filesystems(
                source, target, cancel_requested, publish_guard
            )
        source.unlink()
        return self._placement_result(target, skipped_existing=False)

    def _copy_across_filesystems(
        self,
        source: Path,
        target: Path,
        cancel_requested: Callable[[], bool],
        publish_guard: AbstractContextManager[Any] | None = None,
    ) -> PlacementResult:
        guard = publish_guard if publish_guard is not None else nullcontext()
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".part", dir=target.parent
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        try:
            shutil.copy2(source, temporary_path)
            if temporary_path.stat().st_size != source.stat().st_size:
                raise OSError("cross-filesystem copy size mismatch")
            with temporary_path.open("rb") as copied_file:
                os.fsync(copied_file.fileno())
            with guard:
                self._raise_if_cancelled(cancel_requested)
                try:
                    os.link(temporary_path, target)
                except FileExistsError:
                    return self._placement_result(target, skipped_existing=True)
            source.unlink()
            return self._placement_result(target, skipped_existing=False)
        finally:
            temporary_path.unlink(missing_ok=True)

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

    def _placement_result(
        self, target: Path, *, skipped_existing: bool
    ) -> PlacementResult:
        # The placed file is verified inside the storage boundary so callers
        # never stat library paths themselves (ARCHITECTURE.md §37); the
        # captured identity is what the catalog-write protocol re-verifies.
        target_stat = target.stat()
        return PlacementResult(
            final_path=target,
            relative_path=self._relative(target),
            skipped_existing=skipped_existing,
            size_bytes=target_stat.st_size,
            identity=(target_stat.st_dev, target_stat.st_ino),
        )

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self._library_root.resolve()).as_posix()
