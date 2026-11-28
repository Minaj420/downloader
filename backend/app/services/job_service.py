"""Job creation and lifecycle queries.

Job creation resolves ``analysis_id + variant_id`` against the persisted
analysis session and stores only application-level data. The raw yt-dlp
format mapping stays backend-side in the stored analysis (ARCHITECTURE.md
§9–10): the browser can never submit format ids or CLI arguments.
"""

import asyncio
import base64
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.actors import Actor
from app.domain.analysis import COLLECTION_SELECTED_ITEM_LIMIT, NormalizedAnalysis
from app.domain.jobs import (
    ACTIVE_JOB_STATUSES,
    CLEAR_HISTORY_WARNING_EVENT_TYPE,
    CLEAR_HISTORY_WARNING_MESSAGE,
    ORIGINAL_OUTPUT_PROFILE,
    RETRY_QUEUED_EVENT_TYPE,
    RETRY_QUEUED_MESSAGE,
    ClearHistoryError,
    CollectionProfile,
    JobError,
    JobErrorCode,
    JobStatus,
    OutputProfile,
    output_profile_allowed,
    single_job_retryable,
)
from app.models.analysis import AnalysisSession
from app.models.jobs import DownloadJob, JobItem
from app.repositories import activity
from app.repositories import jobs as job_repo
from app.repositories.jobs import ClearHistoryMember
from app.services.collection_persistence import (
    CollectionCancelRequestOutcome,
    stage_collection_cancel_request,
    stage_collection_cancel_settlement,
    stage_collection_retry,
)
from app.services.conversion_plan import plan_conversion, resolve_output_profile
from app.services.event_bus import EventBus
from app.services.job_runner import resolve_variant
from app.services.queue_manager import QueueManager
from app.services.storage_service import StorageService
from app.utils.datetime import utc_isoformat

APP_LIBRARY_DESTINATION = "app_library"
SKIP_EXISTING_POLICY = "skip"
HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 100
CLEAR_PREVIEW_TTL_SECONDS = 600.0
CLEAR_PREVIEW_CAP = 16

_CANCELLABLE_STATUSES = {JobStatus.QUEUED, *ACTIVE_JOB_STATUSES}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClearHistoryPreview:
    """Process-local, single-use Clear History snapshot (ARCHITECTURE §59 Q3)."""

    owner_id: str
    include_cancelled: bool
    created: float
    members: tuple[ClearHistoryMember, ...]


@dataclass(frozen=True)
class ClearHistoryResult:
    deleted_count: int
    failed_count: int


class _ClearOutcome(StrEnum):
    DELETED = "deleted"
    FAILED = "failed"
    SKIPPED = "skipped"
    STOPPED = "stopped"


class _ClearHistorySystemicError(Exception):
    """Class (c): no further job may start; mapped to one fixed masked error."""


# Primary SQLite result codes that mean the database as a whole cannot be
# written (not a per-job condition): READONLY (8, incl. extended codes such as
# READONLY_DBMOVED), IOERR (10), CORRUPT (11), FULL (13), CANTOPEN (14) and
# NOTADB (26). BUSY/LOCKED stay per-job (contention, not a systemic failure).
_SYSTEMIC_SQLITE_PRIMARY_CODES = frozenset({8, 10, 11, 13, 14, 26})


def _is_systemic_db_error(exc: BaseException) -> bool:
    """Classify by the DBAPI ``sqlite_errorcode`` (SQLAlchemy ``orig``), never text."""
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        code = getattr(current, "sqlite_errorcode", None)
        if isinstance(code, int) and (code & 0xFF) in _SYSTEMIC_SQLITE_PRIMARY_CODES:
            return True
        orig = getattr(current, "orig", None)
        pending.extend([
            orig if isinstance(orig, BaseException) else None,
            current.__cause__, current.__context__,
        ])
    return False


class JobService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue_manager: QueueManager,
        bus: EventBus,
        *,
        storage: StorageService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._queue_manager = queue_manager
        self._bus = bus
        # Explicit Clear History storage dependency; without it confirm fails
        # closed before consuming a preview or touching anything.
        self._storage = storage
        self._clear_previews: OrderedDict[str, ClearHistoryPreview] = OrderedDict()
        self._clock = time.monotonic

    async def create_job(
        self,
        actor: Actor,
        *,
        analysis_id: str,
        variant_id: str | None = None,
        destination: str,
        item_ids: list[str] | None = None,
        collection_mode: str | None = None,
        collection_profile: CollectionProfile | None = None,
        output_profile: OutputProfile | None = None,
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
        children: list[JobItem] = []
        is_collection = normalized.content_type == "collection"
        if is_collection:
            if variant_id is not None or collection_mode not in {"audio", "video"}:
                raise JobError(JobErrorCode.INVALID_PROFILE)
            profile = collection_profile or CollectionProfile()
            if collection_mode == "audio" and profile.max_height is not None:
                raise JobError(JobErrorCode.INVALID_PROFILE)
            if (
                not item_ids
                or len(item_ids) > COLLECTION_SELECTED_ITEM_LIMIT
                or len(set(item_ids)) != len(item_ids)
                or normalized.analysis_id != analysis.id
                or normalized.collection is None
            ):
                raise JobError(JobErrorCode.INVALID_SELECTION)
            available = {item.item_id: item for item in normalized.collection.items}
            if any(
                item_id not in available
                or not available[item_id].selectable
                or not available[item_id].url
                for item_id in item_ids
            ):
                raise JobError(JobErrorCode.INVALID_SELECTION)
            selection = sorted(
                (available[item_id] for item_id in item_ids),
                key=lambda item: item.source_order,
            )
        elif item_ids is not None or collection_mode is not None or collection_profile is not None:
            raise JobError(JobErrorCode.INVALID_SELECTION)
        selected = None if is_collection else next(
            (fmt for fmt in normalized.formats if fmt.variant_id == variant_id), None
        )
        if not is_collection and selected is None:
            raise JobError(
                JobErrorCode.INVALID_VARIANT, detail=f"unknown variant {variant_id}"
            )
        output = ORIGINAL_OUTPUT_PROFILE if output_profile is None else output_profile
        media_kind = (
            collection_mode if selected is None
            else "audio" if selected.kind == "audio_only" else "video"
        )
        if not output_profile_allowed(media_kind or "", output):
            raise JobError(JobErrorCode.INVALID_PROFILE)
        if selected is not None and output != ORIGINAL_OUTPUT_PROFILE:
            # Same pure plan the runner uses: an incompatible single source
            # fails before any row, Activity, publish or notify. Collection
            # children are checked by the runner after their own probe.
            target = resolve_output_profile(output)
            if target is not None:
                plan_conversion(target, *resolve_variant(normalized, selected.variant_id))

        now = datetime.now(UTC)
        record = DownloadJob(
            id=uuid4().hex,
            owner_id=actor.id,
            analysis_id=analysis.id,
            source_url=analysis.source_url,
            title=_display_title(normalized),
            variant_id=variant_id or "",
            job_type="collection" if is_collection else "single",
            collection_mode=collection_mode,
            collection_profile_json=(
                profile.model_dump_json()
                if is_collection and profile.max_height is not None else None
            ),
            # Original (explicit, omitted or null) is stored as NULL, like legacy rows.
            output_profile=None if output == ORIGINAL_OUTPUT_PROFILE else output,
            destination=destination,
            conflict_policy=SKIP_EXISTING_POLICY,
            keep_app_copy=False,
            status=JobStatus.QUEUED.value,
            progress_percent=0.0,
            created_at=now,
            updated_at=now,
        )
        if is_collection:
            children = [
                JobItem(
                    id=uuid4().hex,
                    job_id=record.id,
                    item_id=item.item_id,
                    source_order=item.source_order,
                    source_id=item.source_id,
                    source_url=item.url,
                    title=item.title,
                    playlist_index=item.position,
                    status=JobStatus.QUEUED.value,
                    progress_percent=0.0,
                    created_at=now,
                    updated_at=now,
                )
                for item in selection
            ]
        async with self._session_factory() as session:
            # Single transaction: the job row and its initial "Queued"
            # activity event commit together or not at all.
            if is_collection:
                await job_repo.stage_collection_job(session, record, children, owner_id=actor.id)
            else:
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

    async def list_jobs_with_items(
        self, actor: Actor
    ) -> list[tuple[DownloadJob, list[JobItem] | None]]:
        """Legacy newest-50 page; collection children in one batched owner-scoped read."""
        async with self._session_factory() as session:
            records = await job_repo.list_jobs(session, actor.id)
            items = await job_repo.list_job_items_for_jobs(
                session,
                [record.id for record in records if record.job_type == "collection"],
                actor.id,
            )
        return [(record, items.get(record.id)) for record in records]

    async def list_active_jobs(self, actor: Actor) -> list[DownloadJob]:
        async with self._session_factory() as session:
            return await job_repo.list_active_jobs(session, actor.id)

    async def list_history_jobs(
        self, actor: Actor, *, limit: int, cursor: str | None = None
    ) -> tuple[list[DownloadJob], str | None]:
        """One keyset page; raises ``InvalidJobCursor`` before any read."""
        before = decode_job_cursor(cursor) if cursor is not None else None
        async with self._session_factory() as session:
            records = await job_repo.list_history_jobs(
                session, actor.id, limit=limit + 1, before=before
            )
        if len(records) <= limit:
            return records, None
        page = records[:limit]
        return page, encode_job_cursor(page[-1])

    async def list_job_items(self, actor: Actor, job_id: str) -> list[JobItem]:
        async with self._session_factory() as session:
            return await job_repo.list_job_items(session, job_id, actor.id)

    async def cancel_job(self, actor: Actor, job_id: str) -> DownloadJob:
        async with self._queue_manager.runtime_operation():
            async with self._queue_manager.lifecycle_lock(job_id):
                return await self._cancel_job(actor, job_id)

    async def retry_job(self, actor: Actor, job_id: str) -> DownloadJob | None:
        async with self._queue_manager.runtime_operation():
            return await self._retry_job(actor, job_id)

    async def _retry_job(self, actor: Actor, job_id: str) -> DownloadJob | None:
        async with self._queue_manager.lifecycle_lock(job_id):
            record = await asyncio.shield(
                self._queue_manager.own_operation(self.get_job(actor, job_id)))
            if record is None:
                return None
            if record.job_type == "collection":
                if record.status not in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
                    raise JobError(JobErrorCode.INVALID_STATE)
                # Own drain as well as transaction/close/finalization. Repeated
                # API cancellation cannot release this lifecycle lock early.
                operation = self._queue_manager.own_operation(self._retry_collection(actor, job_id))
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
            if not single_job_retryable(record.status, record.error_code):
                raise JobError(JobErrorCode.INVALID_STATE)
            # A cancelled single job's old attempt (or a stale queued-cancel
            # signal) is drained here; reset_attempt after the CAS commit drops
            # its cancel event so it never reaches the new attempt.
            await self._queue_manager.wait_for_attempt(job_id)
            # Keep the lock until the transaction, session close and runtime
            # finalization have a definite outcome, even on repeated request
            # cancellation. Never forward that cancellation to the operation.
            operation = self._queue_manager.own_operation(self._finalize_retry(actor, job_id))
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

    async def _retry_collection(self, actor: Actor, job_id: str) -> DownloadJob | None:
        # Old cancellation/JobError/technical outcomes are not eligibility:
        # drain all owned work, then revalidate actual persisted terminal facts.
        await self._queue_manager.wait_for_attempt(job_id, retrieve_outcome=True)
        async with self._session_factory() as session:
            try:
                if not await stage_collection_retry(session, job_id=job_id, owner_id=actor.id):
                    await session.rollback()
                    fresh = await self.get_job(actor, job_id)
                    if fresh is None:
                        return None
                    raise JobError(JobErrorCode.INVALID_STATE)
                events = await activity.latest_events_for_job(session, job_id, limit=1)
                record = await job_repo.get_job(session, job_id, actor.id)
                assert record is not None
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        self._queue_manager.reset_attempt(job_id)
        self._publish_collection_activities(events)
        self._queue_manager.publish_snapshot("job.updated", record)
        self._queue_manager.notify()
        return record

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
                event_type=RETRY_QUEUED_EVENT_TYPE,
                message=RETRY_QUEUED_MESSAGE,
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
        record = await asyncio.shield(
            self._queue_manager.own_operation(self.get_job(actor, job_id)))
        if record is None:
            raise JobError(
                JobErrorCode.ANALYSIS_NOT_FOUND,
                detail=f"job {job_id} not found for owner",
            )
        if record.job_type == "collection":
            # Own the ENTIRE durable lifecycle, including request commit/session
            # close, signal, thread/attempt drain, settlement and publication.
            operation = self._queue_manager.own_operation(self._cancel_collection(actor, job_id))
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

    async def _cancel_collection(self, actor: Actor, job_id: str) -> DownloadJob:
        async with self._session_factory() as session:
            try:
                outcome = await stage_collection_cancel_request(
                    session, job_id=job_id, owner_id=actor.id,
                )
                if outcome == CollectionCancelRequestOutcome.REJECTED:
                    await session.rollback()
                    fresh = await self.get_job(actor, job_id)
                    raise JobError(JobErrorCode.ANALYSIS_NOT_FOUND if fresh is None
                                   else JobErrorCode.INVALID_STATE)
                events = await activity.list_events_for_job(session, job_id) if (
                    outcome == CollectionCancelRequestOutcome.STAGED) else []
                request_events = [e for e in events if e.event_type == "cancel_requested"][-1:]
                await session.commit()
            except BaseException:
                # COMMIT itself can fail with a live SQLite transaction. Close
                # is not a substitute for an explicit whole-transaction rollback.
                await session.rollback()
                raise
        if outcome == CollectionCancelRequestOutcome.STAGED:
            self._publish_collection_activities(request_events)
            fresh = await self.get_job(actor, job_id)
            if fresh is not None:
                self._queue_manager.publish_snapshot("job.updated", fresh)
        await self._queue_manager.request_collection_cancel(job_id)
        await self._queue_manager.wait_for_attempt(job_id, retrieve_outcome=True)
        async with self._session_factory() as session:
            # Capture the boundary within this fresh writer transaction; publish
            # only its newly committed terminal Activities, never an own miss.
            try:
                terminal_events = await stage_collection_cancel_settlement(
                    session, job_id=job_id, owner_id=actor.id,
                )
                if terminal_events is None:
                    # An accepted request may have lost settlement to the fully
                    # drained runner. Roll back the lock-touch, then revalidate
                    # persisted ownership/kind/marker/terminal in a fresh session.
                    # This caller owns no terminal Activity or snapshot on a miss.
                    await session.rollback()
                    fresh = await self.get_job(actor, job_id)
                    if (fresh is not None and fresh.job_type == "collection"
                            and fresh.collection_cancel_requested_at is not None
                            and fresh.status in {"completed", "cancelled"}):
                        return fresh
                    raise JobError(JobErrorCode.INVALID_STATE)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        self._publish_collection_activities(terminal_events)
        fresh = await self.get_job(actor, job_id)
        if fresh is None:
            raise JobError(JobErrorCode.ANALYSIS_NOT_FOUND)
        self._queue_manager.publish_snapshot(f"job.{fresh.status}", fresh)
        return fresh

    def _publish_collection_activities(self, events: list[Any]) -> None:
        for event in events:
            self._bus.publish({"event": "activity.created", "id": event.id, "data": {
                "id": event.id, "job_id": event.job_id, "level": event.level,
                "event_type": event.event_type, "message": event.message,
                "created_at": utc_isoformat(event.created_at),
            }})

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

    # --- Clear History (ARCHITECTURE §59 Q3, PRODUCT_SPEC §17) ---------------

    async def clear_history_preview(
        self, actor: Actor, *, include_cancelled: bool
    ) -> tuple[str, int]:
        """Store the exact eligible owned members; the count is that same list."""
        try:
            async with self._session_factory() as session:
                members = await job_repo.list_clear_history_snapshot(
                    session, actor.id, include_cancelled=include_cancelled,
                )
        except Exception as exc:
            logger.warning("Clear History preview read failed (%s)", type(exc).__name__)
            raise ClearHistoryError(ClearHistoryError.FAILED) from None
        # Synchronous from here: prune, evict oldest, insert.
        now = self._clock()
        self._prune_clear_previews(now)
        while len(self._clear_previews) >= CLEAR_PREVIEW_CAP:
            self._clear_previews.popitem(last=False)
        preview_id = secrets.token_urlsafe(32)
        self._clear_previews[preview_id] = ClearHistoryPreview(
            actor.id, include_cancelled, now, tuple(members),
        )
        return preview_id, len(members)

    def _prune_clear_previews(self, now: float) -> None:
        expired = [
            key for key, entry in self._clear_previews.items()
            if now - entry.created >= CLEAR_PREVIEW_TTL_SECONDS
        ]
        for key in expired:
            del self._clear_previews[key]

    def _take_clear_preview(self, actor: Actor, preview_id: str) -> ClearHistoryPreview:
        """Lookup -> expiry/owner check -> pop, one synchronous step.

        A foreign owner gets the same error without consuming the entry.
        """
        self._prune_clear_previews(self._clock())
        entry = self._clear_previews.get(preview_id)
        if entry is None or entry.owner_id != actor.id:
            raise ClearHistoryError(ClearHistoryError.PREVIEW_EXPIRED)
        del self._clear_previews[preview_id]
        return entry

    async def clear_history_confirm(self, actor: Actor, preview_id: str) -> ClearHistoryResult:
        if self._storage is None:
            raise ClearHistoryError(ClearHistoryError.FAILED)
        # Consumed before the first await: single use even under concurrency.
        preview = self._take_clear_preview(actor, preview_id)
        deleted = failed = 0
        try:
            async with self._queue_manager.runtime_operation():
                for member in preview.members:
                    async with self._queue_manager.lifecycle_lock(member.job_id):
                        outcome = await self._await_owned(self._queue_manager.own_operation(
                            self._clear_one(actor.id, member)))
                    if outcome == _ClearOutcome.STOPPED:
                        break
                    if outcome == _ClearOutcome.DELETED:
                        deleted += 1
                    elif outcome == _ClearOutcome.FAILED:
                        failed += 1
        except _ClearHistorySystemicError:
            raise ClearHistoryError(ClearHistoryError.FAILED) from None
        return ClearHistoryResult(deleted, failed)

    @staticmethod
    async def _await_owned[T](operation: asyncio.Task[T]) -> T:
        """Same shield loop as Retry/Cancel: repeated caller cancellation never
        releases the lock before the owned operation has a definite outcome."""
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

    async def _clear_one(self, owner_id: str, member: ClearHistoryMember) -> _ClearOutcome:
        """Steps (2)-(7) for one job, under its lifecycle lock (caller)."""
        if self._queue_manager.stopping:
            return _ClearOutcome.STOPPED
        fresh, _ = await self._clear_read(owner_id, member, with_keys=False)
        if fresh != member:
            # Lost race (Retried, changed, gone): silent skip, never a drain.
            return _ClearOutcome.SKIPPED
        await self._queue_manager.wait_for_attempt(member.job_id, retrieve_outcome=True)
        fresh, keys = await self._clear_read(owner_id, member, with_keys=True)
        if fresh != member:
            return _ClearOutcome.SKIPPED
        try:
            await asyncio.to_thread(self._remove_clear_staging, keys)
        except Exception as exc:
            logger.warning(
                "Clear History staging removal failed for job %s: %s (%s, errno=%s)",
                member.job_id, getattr(exc, "reason", None), type(exc).__name__,
                getattr(exc, "errno", None),
            )
            await self._write_clear_warning(owner_id, member.job_id)
            return _ClearOutcome.FAILED
        return await self._clear_database(owner_id, member)

    async def _clear_read(
        self, owner_id: str, member: ClearHistoryMember, *, with_keys: bool
    ) -> tuple[ClearHistoryMember | None, tuple[str, ...]]:
        try:
            async with self._session_factory() as session:
                fresh = await job_repo.get_clear_history_member(
                    session, member.job_id, owner_id)
                keys: tuple[str, ...] = ()
                if with_keys and fresh == member:
                    keys = await job_repo.list_clear_history_staging_keys(
                        session, member, owner_id)
        except Exception as exc:
            logger.warning("Clear History read failed for job %s (%s)",
                           member.job_id, type(exc).__name__)
            raise _ClearHistorySystemicError from None
        return fresh, keys

    def _remove_clear_staging(self, keys: tuple[str, ...]) -> None:
        """Worker thread; holds no SQL writer. DB-derived keys only."""
        storage = self._storage
        assert storage is not None
        for key in keys:
            storage.remove_staging_for_clear_history(key)

    async def _clear_database(self, owner_id: str, member: ClearHistoryMember) -> _ClearOutcome:
        """One short CAS transaction; a failure is decided by a fresh read."""
        job_id = member.job_id
        try:
            async with self._session_factory() as session:
                try:
                    rowcount = await job_repo.stage_clear_history_delete(
                        session, member, owner_id)
                    if rowcount == 0:
                        await session.rollback()
                        return _ClearOutcome.SKIPPED
                    if rowcount == 1:
                        await activity.delete_events_for_job(session, job_id)
                        await session.commit()
                        return _ClearOutcome.DELETED
                    await session.rollback()
                    logger.warning("Clear History unexpected rowcount for job %s", job_id)
                except BaseException:
                    await session.rollback()
                    raise
        except Exception as exc:
            logger.warning("Clear History database step failed for job %s (%s, code=%s)",
                           job_id, type(exc).__name__,
                           getattr(getattr(exc, "orig", None), "sqlite_errorcode", None))
            if _is_systemic_db_error(exc):
                # Database-wide write failure (also on COMMIT): stop with the
                # masked error; no warning, count or further job is attempted.
                raise _ClearHistorySystemicError from None
        # Ambiguous per-job outcome: decided only through a fresh session.
        if not await self._clear_job_present(owner_id, job_id):
            return _ClearOutcome.DELETED
        await self._write_clear_warning(owner_id, job_id)
        return _ClearOutcome.FAILED

    async def _clear_job_present(self, owner_id: str, job_id: str) -> bool:
        try:
            async with self._session_factory() as session:
                return await job_repo.job_exists(session, job_id, owner_id)
        except Exception as exc:
            logger.warning("Clear History outcome unverifiable for job %s (%s)",
                           job_id, type(exc).__name__)
            raise _ClearHistorySystemicError from None

    async def _write_clear_warning(self, owner_id: str, job_id: str) -> None:
        """Best effort, fixed text, only on a remaining job.

        Local failures are swallowed; a detected database-wide write failure
        raises the systemic stop instead of continuing with the next job.
        """
        try:
            async with self._session_factory() as session:
                try:
                    # Writer first, then the existence check in the same transaction.
                    event = await activity.add_activity_event(
                        session, job_id=job_id, level="warning",
                        event_type=CLEAR_HISTORY_WARNING_EVENT_TYPE,
                        message=CLEAR_HISTORY_WARNING_MESSAGE,
                    )
                    if not await job_repo.job_exists(session, job_id, owner_id):
                        await session.rollback()
                        return
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise
        except Exception as exc:
            logger.warning("Clear History warning not written for job %s (%s)",
                           job_id, type(exc).__name__)
            if _is_systemic_db_error(exc):
                raise _ClearHistorySystemicError from None
            return
        self._publish_collection_activities([event])


class InvalidJobCursor(ValueError):
    """A history cursor that this server could not have issued."""


_CURSOR_KEYS = {"created_at", "id"}
_CURSOR_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
_CURSOR_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}\+00:00"
)
_CURSOR_ID = re.compile(r"[0-9a-f]{32}")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def encode_job_cursor(record: DownloadJob) -> str:
    """Opaque keyset cursor for the last returned history row (ARCHITECTURE §59 Q1)."""
    created_at = record.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    payload = {
        "created_at": created_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "id": record.id,
    }
    return _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def decode_job_cursor(cursor: str) -> tuple[datetime, str]:
    """Strict inverse of ``encode_job_cursor``; anything else is ``InvalidJobCursor``."""
    if not _CURSOR_TOKEN.fullmatch(cursor) or len(cursor) % 4 == 1:
        raise InvalidJobCursor
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidJobCursor from exc
    if _b64url(raw) != cursor or not isinstance(payload, dict) or set(payload) != _CURSOR_KEYS:
        raise InvalidJobCursor
    created_at, job_id = payload["created_at"], payload["id"]
    if not isinstance(created_at, str) or not _CURSOR_TIME.fullmatch(created_at):
        raise InvalidJobCursor
    if not isinstance(job_id, str) or not _CURSOR_ID.fullmatch(job_id):
        raise InvalidJobCursor
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise InvalidJobCursor from exc
    return parsed, job_id


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
