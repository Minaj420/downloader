"""Job creation and lifecycle queries.

Job creation resolves ``analysis_id + variant_id`` against the persisted
analysis session and stores only application-level data. The raw yt-dlp
format mapping stays backend-side in the stored analysis (ARCHITECTURE.md
§9–10): the browser can never submit format ids or CLI arguments.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.actors import Actor
from app.domain.analysis import NormalizedAnalysis
from app.domain.jobs import (
    ACTIVE_JOB_STATUSES,
    JobError,
    JobErrorCode,
    JobStatus,
    job_error_code_retryable,
)
from app.models.analysis import AnalysisSession
from app.models.jobs import DownloadJob
from app.repositories import activity
from app.repositories import jobs as job_repo
from app.services.event_bus import EventBus
from app.services.queue_manager import QueueManager
from app.utils.datetime import utc_isoformat

APP_LIBRARY_DESTINATION = "app_library"
SKIP_EXISTING_POLICY = "skip"

_CANCELLABLE_STATUSES = {JobStatus.QUEUED, *ACTIVE_JOB_STATUSES}


class JobService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue_manager: QueueManager,
        bus: EventBus,
    ) -> None:
        self._session_factory = session_factory
        self._queue_manager = queue_manager
        self._bus = bus

    async def create_job(
        self,
        actor: Actor,
        *,
        analysis_id: str,
        variant_id: str,
        destination: str,
    ) -> DownloadJob:
        if destination != APP_LIBRARY_DESTINATION:
            raise JobError(JobErrorCode.INVALID_DESTINATION)
        async with self._session_factory() as session:
            result = await session.execute(
                select(AnalysisSession).where(
                    AnalysisSession.id == analysis_id,
                    AnalysisSession.owner_id == actor.id,
                )
            )
            analysis = result.scalar_one_or_none()
        if analysis is None:
            raise JobError(
                JobErrorCode.ANALYSIS_NOT_FOUND,
                detail=f"analysis {analysis_id} not found for owner",
            )
        if _is_expired(analysis):
            raise JobError(JobErrorCode.ANALYSIS_EXPIRED)
        normalized = _parse_normalized(analysis)
        if normalized.content_type != "single":
            raise JobError(
                JobErrorCode.INVALID_VARIANT,
                detail="playlist jobs are not supported yet",
            )
        selected = next(
            (fmt for fmt in normalized.formats if fmt.variant_id == variant_id), None
        )
        if selected is None:
            raise JobError(
                JobErrorCode.INVALID_VARIANT, detail=f"unknown variant {variant_id}"
            )

        now = datetime.now(UTC)
        record = DownloadJob(
            id=uuid4().hex,
            owner_id=actor.id,
            analysis_id=analysis.id,
            source_url=analysis.source_url,
            title=_display_title(normalized),
            variant_id=variant_id,
            destination=destination,
            conflict_policy=SKIP_EXISTING_POLICY,
            keep_app_copy=False,
            status=JobStatus.QUEUED.value,
            progress_percent=0.0,
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            # Single transaction: the job row and its initial "Queued"
            # activity event commit together or not at all.
            await job_repo.add_job(session, record)
            event = await activity.add_activity_event(
                session,
                job_id=record.id,
                level="info",
                event_type="job",
                message="Queued",
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
        # Publish only after the commit above succeeded: a rolled-back
        # transaction must never emit a live event. Same event schema as
        # job_runner's activity events.
        self._bus.publish(
            {"event": "activity.created", "id": event.id, "data": payload}
        )
        # Wake the queue only after the event is live:
        # commit -> publish -> notify.
        self._queue_manager.notify()
        return record

    async def get_job(self, actor: Actor, job_id: str) -> DownloadJob | None:
        async with self._session_factory() as session:
            return await job_repo.get_job(session, job_id, owner_id=actor.id)

    async def list_jobs(self, actor: Actor) -> list[DownloadJob]:
        async with self._session_factory() as session:
            return await job_repo.list_jobs(session, actor.id)

    async def cancel_job(self, actor: Actor, job_id: str) -> DownloadJob:
        async with self._queue_manager.lifecycle_lock(job_id):
            return await self._cancel_job(actor, job_id)

    async def retry_job(self, actor: Actor, job_id: str) -> DownloadJob | None:
        async with self._queue_manager.lifecycle_lock(job_id):
            record = await self.get_job(actor, job_id)
            if record is None:
                return None
            if record.status != JobStatus.FAILED.value or not job_error_code_retryable(
                record.error_code or ""
            ):
                raise JobError(JobErrorCode.INVALID_STATE)
            await self._queue_manager.wait_for_attempt(job_id)
            # Keep the lock until the transaction, session close and runtime
            # finalization have a definite outcome, even on repeated request
            # cancellation. Never forward that cancellation to the operation.
            operation = asyncio.create_task(self._finalize_retry(actor, job_id))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(operation)
                    break
                except asyncio.CancelledError:
                    if operation.cancelled():
                        raise
                    cancelled = True
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def _finalize_retry(self, actor: Actor, job_id: str) -> DownloadJob | None:
        async with self._session_factory() as session:
            won = await job_repo.stage_retry_job(session, job_id, actor.id)
            if not won:
                if await job_repo.get_job(session, job_id, actor.id) is None:
                    return None
                raise JobError(JobErrorCode.INVALID_STATE)
            event = await activity.add_activity_event(
                session,
                job_id=job_id,
                level="info",
                event_type="job",
                message="Retry queued",
            )
            record = await job_repo.get_job(session, job_id, actor.id)
            assert record is not None
            await session.commit()
            payload = {
                "id": event.id,
                "job_id": event.job_id,
                "level": event.level,
                "event_type": event.event_type,
                "message": event.message,
                "created_at": utc_isoformat(event.created_at),
            }
        self._queue_manager.reset_attempt(job_id)
        self._bus.publish(
            {"event": "activity.created", "id": event.id, "data": payload}
        )
        self._queue_manager.publish_snapshot("job.updated", record)
        self._queue_manager.notify()
        return record

    async def _cancel_job(self, actor: Actor, job_id: str) -> DownloadJob:
        record = await self.get_job(actor, job_id)
        if record is None:
            raise JobError(
                JobErrorCode.ANALYSIS_NOT_FOUND,
                detail=f"job {job_id} not found for owner",
            )
        if JobStatus(record.status) not in _CANCELLABLE_STATUSES:
            raise JobError(JobErrorCode.INVALID_STATE)

        # Signal first, independent of the snapshot's freshness: the record
        # may have been claimed between the read above and any DB write below.
        await self._queue_manager.request_cancel(job_id)
        if record.status == JobStatus.QUEUED.value:
            await self._cancel_queued(record)
        refreshed = await self.get_job(actor, job_id)
        assert refreshed is not None
        return refreshed

    async def _cancel_queued(self, record: DownloadJob) -> None:
        """Cancel a queued job only if it has not been claimed meanwhile."""
        async with self._session_factory() as session:
            won = await job_repo.update_job_if_status(
                session,
                record.id,
                {JobStatus.QUEUED.value},
                {
                    "status": JobStatus.CANCELLED.value,
                    "error_code": JobErrorCode.CANCELLED.value,
                    "completed_at": datetime.now(UTC),
                },
            )
        if not won:
            # The claim won the race; the cancel signal already sent above
            # stops the runner at its next cancellation gate.
            return
        async with self._session_factory() as session:
            event = await activity.add_activity_event(
                session,
                job_id=record.id,
                level="info",
                event_type="job",
                message="Cancelled",
            )
            await session.commit()
            payload = {
                "id": event.id,
                "job_id": event.job_id,
                "level": event.level,
                "event_type": event.event_type,
                "message": event.message,
                "created_at": utc_isoformat(event.created_at),
            }
        # Match the runner invariant: persist first, then publish activity,
        # then publish the authoritative cancelled job snapshot.
        self._bus.publish(
            {"event": "activity.created", "id": event.id, "data": payload}
        )
        refreshed = await self._get_any(record.id)
        if refreshed is not None:
            self._queue_manager.publish_snapshot("job.cancelled", refreshed)

    async def _get_any(self, job_id: str) -> DownloadJob | None:
        async with self._session_factory() as session:
            return await job_repo.get_job_any_owner(session, job_id)


def _is_expired(analysis: AnalysisSession) -> bool:
    expires_at: Any = analysis.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)


def _parse_normalized(analysis: AnalysisSession) -> NormalizedAnalysis:
    if not analysis.normalized_result_json:
        raise JobError(JobErrorCode.ANALYSIS_NOT_FOUND)
    return NormalizedAnalysis.model_validate_json(analysis.normalized_result_json)


def _display_title(normalized: NormalizedAnalysis) -> str:
    return (normalized.title or "download").strip() or "download"
