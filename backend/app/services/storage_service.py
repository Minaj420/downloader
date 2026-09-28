"""Storage boundary: staging layout and finalization into the App Library.

All filesystem behaviour for downloads lives here (ARCHITECTURE.md §28–30).
Filename conflicts default to Skip Existing (PRODUCT_SPEC.md §16): the
existing file is never overwritten.
"""

import asyncio
import errno
import os
import shutil
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.utils.filenames import sanitize_media_filename
from app.utils.paths import ensure_within_root


@dataclass(slots=True)
class PlacementResult:
    final_path: Path
    relative_path: str
    skipped_existing: bool


class PlacementCancelled(Exception):
    """Finalization was cancelled before the file became visible."""


class StorageService:
    def __init__(self, staging_root: Path, library_root: Path) -> None:
        self._staging_root = staging_root
        self._library_root = library_root

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

    def _placement_result(
        self, target: Path, *, skipped_existing: bool
    ) -> PlacementResult:
        return PlacementResult(
            final_path=target,
            relative_path=self._relative(target),
            skipped_existing=skipped_existing,
        )

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self._library_root.resolve()).as_posix()
