"""Executes a single download job through the state machine.

Flow (ARCHITECTURE.md §18, §28–29):

    downloading → post_processing (merge only when needed) → finalizing
        → completed

All state changes, progress, and activity events are persisted and published
to the SSE bus. Raw yt-dlp format ids never leave this module's inputs: the
runner resolves ``variant_id`` against the stored analysis session. A
successful App Library finalization also writes the job's Library catalog
rows in the same transaction as the completion transition (§37, §51).
"""

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.analysis import NormalizedAnalysis, NormalizedFormat
from app.domain.jobs import ACTIVE_JOB_STATUSES, JobError, JobErrorCode, JobStatus
from app.domain.library import MediaStorageType
from app.integrations.ffmpeg import FFmpegError, FFmpegFailureKind, FFmpegProcessor
from app.integrations.ytdlp import (
    DownloadProgress,
    YtDlpDownloader,
    YtDlpError,
    YtDlpFailureKind,
)
from app.models.analysis import AnalysisSession
from app.models.jobs import DownloadJob
from app.models.library import LibraryItem, MediaFile
from app.repositories import activity
from app.repositories import jobs as job_repo
from app.services.event_bus import EventBus
from app.services.job_mapping import job_to_dto
from app.services.library_service import stage_media_catalog_reference
from app.services.prepared_media import prepare_media
from app.services.storage_service import (
    MediaFileMissingError,
    MediaFileNotRegularError,
    MediaFileTargetChangedError,
    PlacementCancelled,
    PlacementResult,
    StorageDeleteFailedError,
    StoragePathError,
    StorageService,
    StorageTargetError,
)
from app.utils.processes import ensure_process_terminated

_YT_DLP_KIND_TO_JOB_CODE: dict[YtDlpFailureKind, JobErrorCode] = {
    YtDlpFailureKind.BINARY_MISSING: JobErrorCode.YT_DLP_MISSING,
    YtDlpFailureKind.UNSUPPORTED_URL: JobErrorCode.UNSUPPORTED_URL,
    YtDlpFailureKind.AUTHENTICATION_REQUIRED: JobErrorCode.AUTHENTICATION_REQUIRED,
    YtDlpFailureKind.GEO_RESTRICTED: JobErrorCode.GEO_RESTRICTED,
    YtDlpFailureKind.MEDIA_UNAVAILABLE: JobErrorCode.MEDIA_UNAVAILABLE,
    YtDlpFailureKind.NETWORK_ERROR: JobErrorCode.NETWORK_ERROR,
    YtDlpFailureKind.TIMEOUT: JobErrorCode.TIMEOUT,
    YtDlpFailureKind.MALFORMED_OUTPUT: JobErrorCode.MALFORMED_OUTPUT,
    YtDlpFailureKind.EXTRACTOR_FAILED: JobErrorCode.EXTRACTOR_FAILED,
}

_FFMPEG_KIND_TO_JOB_CODE: dict[FFmpegFailureKind, JobErrorCode] = {
    FFmpegFailureKind.BINARY_MISSING: JobErrorCode.FFMPEG_MISSING,
    FFmpegFailureKind.FAILED: JobErrorCode.POST_PROCESSING_FAILED,
    FFmpegFailureKind.TIMEOUT: JobErrorCode.POST_PROCESSING_FAILED,
}

_DETAIL_LIMIT = 4_000
_TERMINATION_GRACE_SECONDS = 5.0
_ACTIVE_STATUS_VALUES = {status.value for status in ACTIVE_JOB_STATUSES}


class _RunAborted(Exception):
    """The job left its expected state concurrently; stop without writing."""


@dataclass(slots=True)
class RunnerDeps:
    session_factory: async_sessionmaker[AsyncSession]
    downloader: YtDlpDownloader
    ffmpeg: FFmpegProcessor
    storage: StorageService
    bus: EventBus
    progress_min_interval: float


ProcessRegistry = dict[str, asyncio.subprocess.Process]
CancelGateRegistry = dict[str, threading.Lock]


def resolve_variant(
    normalized: NormalizedAnalysis, variant_id: str
) -> tuple[NormalizedFormat, NormalizedFormat | None]:
    """Resolve the selected variant and, for video-only, the audio companion.

    The companion is the best source-derived audio format; the browser never
    chooses it (ARCHITECTURE.md §10).
    """
    selected = next(
        (fmt for fmt in normalized.formats if fmt.variant_id == variant_id), None
    )
    if selected is None:
        raise JobError(
            JobErrorCode.INVALID_VARIANT, detail=f"unknown variant {variant_id}"
        )
    companion = None
    if selected.kind == "video_only":
        companion = next(
            (fmt for fmt in normalized.formats if fmt.kind == "audio_only"), None
        )
    return selected, companion


def build_app_library_catalog(
    job: DownloadJob,
    normalized: NormalizedAnalysis,
    selected: NormalizedFormat,
    placement: PlacementResult,
) -> tuple[LibraryItem, MediaFile]:
    """Derive the Library catalog rows for a finalized App Library download.

    Only fields with an authoritative existing source are populated — the
    job, the stored analysis (including the raw source id when the source
    provided one), the selected normalized format, and the storage
    placement (ARCHITECTURE.md §23, §37). Music metadata fields
    (artist/album/…) have no source until Milestone 6 and stay null; the
    physical row always carries the storage-controlled path and the real
    file size read inside the storage boundary.
    """
    now = datetime.now(UTC)
    audio = selected.kind == "audio_only"
    item = LibraryItem(
        id=uuid4().hex,
        owner_id=job.owner_id,
        job_id=job.id,
        source_url=job.source_url,
        source_id=normalized.source_id,
        platform=normalized.detected_platform,
        media_type="audio" if audio else "video",
        title=job.title,
        format=placement.final_path.suffix.lstrip(".") or None,
        codec=selected.audio_codec if audio else selected.video_codec,
        bitrate=selected.bitrate_kbps,
        resolution=selected.resolution_label,
        duration=normalized.duration_seconds,
        created_at=now,
    )
    media_file = MediaFile(
        library_item_id=item.id,
        storage_type=MediaStorageType.APP_LIBRARY.value,
        path=placement.relative_path,
        size_bytes=placement.size_bytes,
        created_at=now,
    )
    return item, media_file


def _truncate_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    return detail[:_DETAIL_LIMIT]


async def _get_job(
    session_factory: async_sessionmaker[AsyncSession], job_id: str
) -> DownloadJob | None:
    async with session_factory() as session:
        return await job_repo.get_job_any_owner(session, job_id)


async def _get_analysis(
    session_factory: async_sessionmaker[AsyncSession],
    analysis_id: str,
    owner_id: str,
) -> AnalysisSession | None:
    async with session_factory() as session:
        result = await session.execute(
            select(AnalysisSession).where(
                AnalysisSession.id == analysis_id,
                AnalysisSession.owner_id == owner_id,
            )
        )
        return result.scalar_one_or_none()


async def run_download_job(
    job_id: str,
    *,
    deps: RunnerDeps,
    cancel_event: asyncio.Event,
    processes: ProcessRegistry,
    cancel_gates: CancelGateRegistry,
) -> None:
    execution = _JobExecution(job_id, deps, cancel_event, processes, cancel_gates)
    try:
        await execution.run()
    finally:
        await execution.drain_helpers()
        processes.pop(job_id, None)
        cancel_gates.pop(job_id, None)


class _JobExecution:
    def __init__(
        self,
        job_id: str,
        deps: RunnerDeps,
        cancel_event: asyncio.Event,
        processes: ProcessRegistry,
        cancel_gates: CancelGateRegistry,
    ) -> None:
        self._job_id = job_id
        self._deps = deps
        self._cancel_event = cancel_event
        self._processes = processes
        self._cancel_gate = cancel_gates.setdefault(job_id, threading.Lock())
        self._last_progress_emit = 0.0
        self._owner_id: str | None = None
        self._helpers: set[asyncio.Task[Any]] = set()
        self._closed = False

    async def drain_helpers(self) -> None:
        # Close callbacks before waiting: no old callback can schedule a write
        # after the queue manager observes this attempt's task as finished.
        self._closed = True
        if self._helpers:
            await asyncio.gather(*self._helpers, return_exceptions=True)

    async def run(self) -> None:
        record = await self._load_job()
        if record is None or record.status != JobStatus.QUEUED.value:
            return
        self._owner_id = record.owner_id
        try:
            if not await self._transition_to_downloading():
                return
            await self._execute(record)
        except _RunAborted:
            return
        except JobError as exc:
            await self._fail(exc)
        except YtDlpError as exc:
            await self._fail(
                JobError(_YT_DLP_KIND_TO_JOB_CODE[exc.kind], detail=exc.detail)
            )
        except FFmpegError as exc:
            await self._fail(
                JobError(_FFMPEG_KIND_TO_JOB_CODE[exc.kind], detail=exc.detail)
            )
        except PlacementCancelled:
            await self._fail(JobError(JobErrorCode.CANCELLED))
        except OSError as exc:
            await self._fail(JobError(JobErrorCode.DISK_WRITE_FAILED, detail=repr(exc)))
        except asyncio.CancelledError:
            await self._fail(JobError(JobErrorCode.INTERRUPTED_BY_RESTART))
            raise
        except Exception as exc:
            await self._fail(JobError(JobErrorCode.INTERNAL_ERROR, detail=repr(exc)))

    async def _execute(self, record: DownloadJob) -> None:
        analysis = await _get_analysis(
            self._deps.session_factory, record.analysis_id, record.owner_id
        )
        if analysis is None or not analysis.normalized_result_json:
            raise JobError(
                JobErrorCode.ANALYSIS_NOT_FOUND,
                detail=f"analysis {record.analysis_id} missing",
            )
        normalized = NormalizedAnalysis.model_validate_json(
            analysis.normalized_result_json
        )
        selected, companion = resolve_variant(normalized, record.variant_id)

        await self._update(staging_path=self._job_id)

        async def before_merge() -> None:
            await self._set_status(
                JobStatus.POST_PROCESSING, "Post-processing (merging video and audio)"
            )

        media_file = await prepare_media(
            selected=selected,
            companion=companion,
            source_url=record.source_url,
            staging_key=self._job_id,
            storage=self._deps.storage,
            downloader=self._deps.downloader,
            ffmpeg=self._deps.ffmpeg,
            check_cancelled=self._check_cancelled,
            on_process_started=self._register_process,
            progress_forwarder=self._progress_forwarder,
            before_merge=before_merge,
        )

        await self._check_cancelled()
        await self._set_status(JobStatus.FINALIZING, "Finalizing")
        media_kind = "audio" if selected.kind == "audio_only" else "video"
        placement = await self._deps.storage.place_final(
            media_file,
            media_kind=media_kind,
            title=record.title,
            cancel_requested=self._cancel_event.is_set,
            publish_guard=self._cancel_gate,
        )
        if placement.skipped_existing:
            await self._activity(
                "A file with this name already exists — kept the existing copy",
                level="warning",
            )
        else:
            await self._activity("Saved to App Library")

        await self._complete(record, normalized, selected, placement)
        self._deps.storage.remove_staging(self._job_id)

    def _progress_forwarder(
        self, step_index: int, total_steps: int
    ) -> Callable[[DownloadProgress], asyncio.Task[Any] | None]:
        def forward(progress: DownloadProgress) -> asyncio.Task[Any] | None:
            if self._closed:
                return None
            now = time.monotonic()
            if now - self._last_progress_emit < self._deps.progress_min_interval:
                return None
            self._last_progress_emit = now
            total = progress.total_bytes or progress.total_bytes_estimate
            step_fraction = (
                progress.downloaded_bytes / total
                if total and progress.downloaded_bytes
                else 0.0
            )
            percent = min((step_index + step_fraction) / total_steps * 100.0, 100.0)
            task = asyncio.get_running_loop().create_task(
                self._update_progress(
                    progress_percent=percent,
                    downloaded_bytes=progress.downloaded_bytes,
                    total_bytes=progress.total_bytes,
                    speed_bytes_per_second=progress.speed_bytes_per_second,
                    eta_seconds=progress.eta_seconds,
                )
            )
            self._helpers.add(task)
            return task

        return forward

    async def _update_progress(self, **values: Any) -> None:
        async with self._deps.session_factory() as session:
            won = await job_repo.update_job_if_status(
                session, self._job_id, _ACTIVE_STATUS_VALUES, values
            )
        if won:
            await self._publish_job_event("job.updated")

    async def _transition_to_downloading(self) -> bool:
        """Claim the queued job atomically; a concurrent cancel wins here."""
        values: dict[str, Any] = {
            "status": JobStatus.DOWNLOADING.value,
            "started_at": datetime.now(UTC),
        }
        async with self._deps.session_factory() as session:
            won = await job_repo.update_job_if_status(
                session, self._job_id, {JobStatus.QUEUED.value}, values
            )
        if not won:
            return False
        await self._activity("Download started")
        await self._publish_job_event("job.updated")
        return True

    async def _set_status(self, status: JobStatus, message: str) -> None:
        async with self._deps.session_factory() as session:
            won = await job_repo.update_job_if_status(
                session,
                self._job_id,
                _ACTIVE_STATUS_VALUES,
                {"status": status.value},
            )
        if not won:
            raise _RunAborted
        await self._activity(message)

    async def _complete(
        self,
        record: DownloadJob,
        normalized: NormalizedAnalysis,
        selected: NormalizedFormat,
        placement: PlacementResult,
    ) -> None:
        """Complete the job and write its Library catalog rows atomically.

        The conditional status update and the catalog inserts share one
        transaction (ARCHITECTURE.md §51): a stale runner that lost the
        status race writes no catalog rows, and a failed catalog insert
        rolls the completion back so the job never reports success without
        its database-backed Library record. The catalog rows go through
        ``stage_media_catalog_reference``: after the staged completion
        update has taken the SQLite write lock, the physical target is
        re-verified through the storage boundary, so a concurrent Delete
        File that committed while this completion waited on the lock
        fails the whole transaction instead of leaving a completed job
        with a dangling reference to a deleted file.
        """
        item, media_file = build_app_library_catalog(
            record, normalized, selected, placement
        )
        async with self._deps.session_factory() as session:
            won = await job_repo.stage_job_update_if_status(
                session,
                self._job_id,
                _ACTIVE_STATUS_VALUES,
                {
                    "status": JobStatus.COMPLETED.value,
                    "progress_percent": 100.0,
                    "final_path": placement.relative_path,
                    "completed_at": datetime.now(UTC),
                },
            )
            if not won:
                return
            try:
                await stage_media_catalog_reference(
                    session,
                    self._deps.storage,
                    item,
                    media_file,
                    placement_identity=placement.identity,
                )
            except (
                StorageTargetError,
                StoragePathError,
                MediaFileMissingError,
                MediaFileNotRegularError,
                MediaFileTargetChangedError,
                StorageDeleteFailedError,
            ) as exc:
                # The finalized file vanished or changed under a
                # concurrent Delete File (or its location became
                # unverifiable): committing the catalog rows would lie,
                # so the completion rolls back and the job must not
                # silently report success (ARCHITECTURE.md §51).
                raise JobError(
                    JobErrorCode.INTERNAL_ERROR,
                    detail=(
                        "finalized file failed storage re-validation "
                        f"before catalog commit: {exc!r}"
                    ),
                ) from exc
            await session.commit()
        await self._publish_job_event("job.completed")

    async def _fail(self, error: JobError) -> None:
        cancelled = self._cancel_event.is_set()
        now = datetime.now(UTC)
        if cancelled:
            status = JobStatus.CANCELLED
            code = JobErrorCode.CANCELLED
        else:
            status = JobStatus.FAILED
            code = error.code
        async with self._deps.session_factory() as session:
            won = await job_repo.update_job_if_status(
                session,
                self._job_id,
                _ACTIVE_STATUS_VALUES,
                {
                    "status": status.value,
                    "error_code": code.value,
                    "error_message": error.message,
                    "error_detail": _truncate_detail(error.detail),
                    "completed_at": now,
                },
            )
        if cancelled:
            self._deps.storage.remove_staging(self._job_id)
        if not won:
            return
        await self._activity(
            "Cancelled" if cancelled else f"Failed: {error.message}",
            level="info" if cancelled else "error",
        )
        await self._publish_job_event("job.cancelled" if cancelled else "job.failed")

    async def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise JobError(JobErrorCode.CANCELLED)

    def _register_process(
        self, process: asyncio.subprocess.Process
    ) -> asyncio.Task[Any] | None:
        self._processes[self._job_id] = process
        if self._cancel_event.is_set() and process.returncode is None:
            # Cancellation arrived between the pre-step gate and this
            # registration; terminate instead of letting the step run on.
            task = asyncio.get_running_loop().create_task(
                ensure_process_terminated(process, _TERMINATION_GRACE_SECONDS)
            )
            self._helpers.add(task)
            return task
        return None

    async def _load_job(self) -> DownloadJob | None:
        return await _get_job(self._deps.session_factory, self._job_id)

    async def _update(self, **values: Any) -> None:
        async with self._deps.session_factory() as session:
            await job_repo.update_job(session, self._job_id, values)

    async def _activity(
        self,
        message: str,
        *,
        level: str = "info",
        details: dict[str, Any] | None = None,
    ) -> None:
        async with self._deps.session_factory() as session:
            event = await activity.add_activity_event(
                session,
                job_id=self._job_id,
                level=level,
                event_type="job",
                message=message,
                details=details,
            )
            await session.commit()
            payload = {
                "id": event.id,
                "job_id": event.job_id,
                "level": event.level,
                "event_type": event.event_type,
                "message": event.message,
                "created_at": event.created_at.isoformat(),
            }
        self._deps.bus.publish(
            {"event": "activity.created", "id": event.id, "data": payload}
        )

    async def _publish_job_event(self, event_name: str) -> None:
        record = await _get_job(self._deps.session_factory, self._job_id)
        if record is None:
            return
        self._deps.bus.publish(
            {
                "event": event_name,
                "id": None,
                "data": job_to_dto(record).model_dump(mode="json"),
            }
        )
