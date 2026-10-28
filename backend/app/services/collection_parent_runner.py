"""Sequential parent attempt dispatched in production by QueueManager.

Caller registers ONE parent slot and supplies its event/gate/process registry.
Keep that slot through child drain, definite transaction outcomes and publication.
Never acquire the lifecycle lock: Cancel holds it while awaiting this attempt.
Retry/reset/recovery/shutdown classification lives in job_service,
collection_persistence and queue_manager, not in this module.
"""

import asyncio
import threading
from collections.abc import Coroutine
from enum import StrEnum
from typing import Any

from sqlalchemy import select

from app.domain.jobs import ACTIVE_JOB_STATUSES, JobError, JobErrorCode, JobStatus
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.repositories import activity, jobs
from app.services.collection_child_runner import ChildResult, ChildRunnerDeps, run_collection_child
from app.services.collection_persistence import (
    stage_child_transition,
    stage_collection_failure_settlement,
    stage_collection_success_settlement,
)
from app.services.event_bus import EventBus
from app.services.job_mapping import job_to_dto
from app.utils.datetime import utc_isoformat


class ParentRunOutcome(StrEnum):
    """EXHAUSTED means this attempt committed its narrow terminal settlement.

    SKIPPED is a lost claim; LOST_ELIGIBILITY makes no terminal claim, including
    when another writer won. The database remains authoritative.
    """

    EXHAUSTED = "exhausted"
    SKIPPED = "skipped"
    LOST_ELIGIBILITY = "lost_eligibility"


class UnsupportedChildSelection(ValueError):
    """Empty/corrupt or old active work, without inventing retry classification."""


async def _definite[T](operation: Coroutine[Any, Any, T]) -> T:
    """Own transaction/close/publication through repeated caller cancellation."""
    task = asyncio.create_task(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def run_collection_parent(
    job_id: str, owner_id: str, *, deps: ChildRunnerDeps,
    cancel_event: asyncio.Event, processes: dict[str, asyncio.subprocess.Process],
    cancel_gate: threading.Lock, bus: EventBus | None = None,
) -> ParentRunOutcome:
    """Visit queued rows in raw order; completed/failed rows are immutable skips.

    Children are directly awaited and fully drain before recording mapped errors
    or terminal SQL. INTERNAL_ERROR is a technical stop, not a child outcome.
    Actual failure recording bypasses the marker, never ownership/status guards.
    General queued/active Cancel settlement remains the API's responsibility.
    """
    def check_cancelled() -> None:
        if cancel_event.is_set():
            raise asyncio.CancelledError

    async def eligible() -> bool:
        async with deps.session_factory() as session:
            return await session.scalar(select(DownloadJob.id).where(
                DownloadJob.id == job_id, DownloadJob.owner_id == owner_id,
                DownloadJob.job_type == "collection",
                DownloadJob.status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
                DownloadJob.collection_cancel_requested_at.is_(None),
            )) is not None

    def publish_activities(events: list[ActivityEvent]) -> None:
        if bus is None:
            return
        for event in events:
            bus.publish({"event": "activity.created", "id": event.id, "data": {
                "id": event.id, "job_id": event.job_id, "level": event.level,
                "event_type": event.event_type, "message": event.message,
                "created_at": utc_isoformat(event.created_at),
            }})

    async def publish_snapshot(terminal: bool = False) -> None:
        if bus is None:
            return
        async with deps.session_factory() as session:
            record = await jobs.get_job(session, job_id, owner_id)
        if record is not None and record.job_type == "collection":
            bus.publish({"event": f"job.{record.status}" if terminal else "job.updated",
                         "id": None, "data": job_to_dto(record).model_dump(mode="json")})

    async def claim() -> bool:
        async with deps.session_factory() as session:
            try:
                if not await jobs.stage_collection_claim(session, job_id, owner_id):
                    await session.rollback()
                    return False
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return True

    async def record_failure(child_id: str, error: JobError) -> bool:
        async with deps.session_factory() as session:
            try:
                # Fresh column query, not the selected child's cached status.
                actual = await session.scalar(select(JobItem.status).join(
                    DownloadJob, JobItem.job_id == DownloadJob.id,
                ).where(
                    JobItem.id == child_id, JobItem.job_id == job_id,
                    DownloadJob.owner_id == owner_id, DownloadJob.job_type == "collection",
                    DownloadJob.status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
                ))
                if actual not in {s.value for s in ACTIVE_JOB_STATUSES}:
                    await session.rollback()
                    return False
                won = await stage_child_transition(
                    session, job_id=job_id, owner_id=owner_id, child_id=child_id,
                    expected_status=JobStatus(actual), status=JobStatus.FAILED, error=error,
                )
                if not won:
                    await session.rollback()
                    return False
                event = await activity.add_activity_event(
                    session, job_id=job_id, level="error", event_type="failed",
                    message=error.message, details={"child_id": child_id, "code": error.code.value},
                )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        publish_activities([event])
        await publish_snapshot()
        return True

    async def settle() -> bool:
        # Each helper gets a fresh transaction whose FIRST SQL is its writer
        # boundary. An ineligible success touch must not leak into failure SQL.
        for stage in (stage_collection_success_settlement, stage_collection_failure_settlement):
            async with deps.session_factory() as session:
                try:
                    if not await stage(session, job_id=job_id, owner_id=owner_id):
                        await session.rollback()
                        continue
                    events = await activity.latest_events_for_job(session, job_id, limit=1)
                    # This writer staged exactly one parent terminal Activity.
                    event = events[-1]
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise
            publish_activities([event])
            await publish_snapshot(terminal=True)
            return True
        return False

    check_cancelled()
    if not await _definite(claim()):
        return ParentRunOutcome.SKIPPED
    check_cancelled()
    async with deps.session_factory() as session:
        children = await jobs.list_job_items(session, job_id, owner_id)
    if not children or any(
        c.status not in {"queued", "completed", "failed"}
        or not c.id or not c.item_id
        or not isinstance(c.source_order, int) or c.source_order < 1
        or (c.status == "queued" and not c.source_url)
        for c in children
    ) or len({c.source_order for c in children}) != len(children):
        raise UnsupportedChildSelection("Requires a nonempty queued/completed/failed selected set")

    for child in children:
        # Immutable rows need no runtime eligibility. This permits the narrow
        # children-win/failure-race reducers to read already committed facts.
        if child.status in {"completed", "failed"}:
            continue
        if not await eligible():
            # Could be the final failed/completed boundary with a durable
            # marker; the narrow helpers refuse any remaining queued/active row.
            return (ParentRunOutcome.EXHAUSTED if await _definite(settle())
                    else ParentRunOutcome.LOST_ELIGIBILITY)
        check_cancelled()
        try:
            result = await run_collection_child(
                job_id, owner_id, child.id, deps=deps, cancel_event=cancel_event,
                processes=processes, cancel_gate=cancel_gate,
            )
        except JobError as error:
            if error.code == JobErrorCode.INTERNAL_ERROR:
                raise
            if not await _definite(record_failure(child.id, error)):
                raise
        else:
            if result == ChildResult.SKIPPED:
                return ParentRunOutcome.LOST_ELIGIBILITY
            await _definite(publish_snapshot())

    # All child work has returned/drained. Durable facts decide terminal
    # precedence, never the event. No marker: caller cancellation propagates.
    if cancel_event.is_set() and await eligible():
        raise asyncio.CancelledError
    return (ParentRunOutcome.EXHAUSTED if await _definite(settle())
            else ParentRunOutcome.LOST_ELIGIBILITY)
