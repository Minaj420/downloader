"""Executes a single download job through the state machine.

Flow (ARCHITECTURE.md §18, §28–29):

    downloading → post_processing (merge only when needed) → finalizing
        → completed

All state changes, progress, and activity events are persisted and published
to the SSE bus. Raw yt-dlp format ids never leave this module's inputs: the
runner resolves ``variant_id`` against the stored analysis session.
"""

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.analysis import NormalizedAnalysis, NormalizedFormat
from app.domain.jobs import ACTIVE_JOB_STATUSES, JobError, JobErrorCode, JobStatus
from app.integrations.ffmpeg import FFmpegError, FFmpegFailureKind, FFmpegProcessor
from app.integrations.ytdlp import (
    DownloadProgress,
    YtDlpDownloader,
    YtDlpError,
    YtDlpFailureKind,
)
from app.models.analysis import AnalysisSession
from app.models.jobs import DownloadJob
from app.repositories import activity
from app.repositories import jobs as job_repo
from app.services.event_bus import EventBus
from app.services.job_mapping import job_to_dto
from app.services.storage_service import PlacementCancelled, StorageService
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


def merge_container(video: NormalizedFormat, audio: NormalizedFormat) -> str:
    if audio.audio_codec == "aac" and video.video_codec in {"h264", "hevc"}:
        return "mp4"
    return "mkv"


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
    try:
        await _JobExecution(job_id, deps, cancel_event, processes, cancel_gates).run()
    finally:
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
        self._source_url_cache: str | None = None
        self._owner_id: str | None = None

    async def run(self) -> None:
        record = await self._load_job()
        if record is None or record.status != JobStatus.QUEUED.value:
            return
        self._source_url_cache = record.source_url
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

        staging_dir = self._deps.storage.job_staging_dir(self._job_id)
        await self._update(staging_path=self._job_id)

        if selected.kind == "video_only" and companion is not None:
            await self._check_cancelled()
            video_file = await self._download_step(
                "video", selected.format_id, staging_dir, step_index=0, total_steps=2
            )
            await self._check_cancelled()
            audio_file = await self._download_step(
                "audio", companion.format_id, staging_dir, step_index=1, total_steps=2
            )
            await self._check_cancelled()
            await self._set_status(
                JobStatus.POST_PROCESSING, "Post-processing (merging video and audio)"
            )
            merged = staging_dir / f"merged.{merge_container(selected, companion)}"
            await self._deps.ffmpeg.merge_streams(
                video_file,
                audio_file,
                merged,
                on_process_started=self._register_process,
            )
            media_file = merged
        else:
            await self._check_cancelled()
            media_file = await self._download_step(
                "media", selected.format_id, staging_dir, step_index=0, total_steps=1
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

        await self._complete(placement.relative_path)
        self._deps.storage.remove_staging(self._job_id)

    async def _download_step(
        self,
        prefix: str,
        format_id: str,
        staging_dir: Path,
        *,
        step_index: int,
        total_steps: int,
    ) -> Path:
        assert self._source_url_cache is not None
        produced = await self._deps.downloader.download(
            url=self._source_url_cache,
            format_selector=format_id,
            output_template=str(staging_dir / f"{prefix}.%(ext)s"),
            on_progress=self._progress_forwarder(step_index, total_steps),
            on_process_started=self._register_process,
        )
        if not produced:
            raise JobError(
                JobErrorCode.MALFORMED_OUTPUT,
                detail=f"download produced no file for format {format_id}",
            )
        return Path(produced[0])

    def _progress_forwarder(
        self, step_index: int, total_steps: int
    ) -> Callable[[DownloadProgress], None]:
        def forward(progress: DownloadProgress) -> None:
            now = time.monotonic()
            if now - self._last_progress_emit < self._deps.progress_min_interval:
                return
            self._last_progress_emit = now
            total = progress.total_bytes or progress.total_bytes_estimate
            step_fraction = (
                progress.downloaded_bytes / total
                if total and progress.downloaded_bytes
                else 0.0
            )
            percent = min((step_index + step_fraction) / total_steps * 100.0, 100.0)
            asyncio.get_running_loop().create_task(
                self._update_progress(
                    progress_percent=percent,
                    downloaded_bytes=progress.downloaded_bytes,
                    total_bytes=progress.total_bytes,
                    speed_bytes_per_second=progress.speed_bytes_per_second,
                    eta_seconds=progress.eta_seconds,
                )
            )

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

    async def _complete(self, final_path: str) -> None:
        async with self._deps.session_factory() as session:
            won = await job_repo.update_job_if_status(
                session,
                self._job_id,
                _ACTIVE_STATUS_VALUES,
                {
                    "status": JobStatus.COMPLETED.value,
                    "progress_percent": 100.0,
                    "final_path": final_path,
                    "completed_at": datetime.now(UTC),
                },
            )
        if not won:
            return
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

    def _register_process(self, process: asyncio.subprocess.Process) -> None:
        self._processes[self._job_id] = process
        if self._cancel_event.is_set() and process.returncode is None:
            # Cancellation arrived between the pre-step gate and this
            # registration; terminate instead of letting the step run on.
            asyncio.get_running_loop().create_task(
                ensure_process_terminated(process, _TERMINATION_GRACE_SECONDS)
            )

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
