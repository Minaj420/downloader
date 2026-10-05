"""Guard-preserving internal collection persistence; no runner or retry activation.

All operations stage into the caller's transaction. Callers must roll back on
any exception, commit before publishing, and drain old helpers before reset.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analysis import NormalizedFormat
from app.domain.jobs import (
    ACTIVE_JOB_STATUSES,
    JobError,
    JobErrorCode,
    JobStatus,
    collection_child_retryable,
    collection_retryable,
)
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs
from app.repositories.activity import add_activity_event
from app.services.library_service import stage_media_catalog_reference
from app.services.storage_service import PlacementResult, StorageService


class CollectionCancelRequestOutcome(StrEnum):
    STAGED = "staged"
    ALREADY_REQUESTED = "already_requested"
    REJECTED = "rejected"


class CollectionRecoveryOutcome(StrEnum):
    STAGED = "staged"
    NO_CHANGE = "no_change"
    UNSUPPORTED = "unsupported"


async def stage_collection_startup_recovery(
    session: AsyncSession, *, job_id: str, owner_id: str,
) -> CollectionRecoveryOutcome:
    """Recovery with no old live attempt, subprocess or storage work remaining.

    Valid before startup claims, or after orderly stop's full attempt/API drain.

    First SQL owns the writer boundary; candidates/ORM caches are not decisions.
    Caller commits STAGED only, rolls back the WHOLE fresh transaction otherwise
    (also on write/commit errors), then closes before publishing a fresh snapshot.
    Unsupported sets are left untouched, never repaired or automatically resumed.
    """
    now = datetime.now(UTC)
    active = [s.value for s in ACTIVE_JOB_STATUSES]
    eligible = (
        DownloadJob.id == job_id, DownloadJob.owner_id == owner_id,
        DownloadJob.job_type == "collection",
        or_(DownloadJob.status.in_(active),
            (DownloadJob.status == "queued")
            & DownloadJob.collection_cancel_requested_at.is_not(None)),
    )
    result = await session.execute(update(DownloadJob).where(*eligible).values(updated_at=now)
                                   .execution_options(autoflush=False, synchronize_session=False))
    if cast(CursorResult[Any], result).rowcount != 1:
        return CollectionRecoveryOutcome.NO_CHANGE
    marker = await session.scalar(select(DownloadJob.collection_cancel_requested_at)
                                  .where(*eligible).execution_options(autoflush=False))
    if marker is not None:
        events = await stage_collection_cancel_settlement(
            session, job_id=job_id, owner_id=owner_id)
        return (CollectionRecoveryOutcome.STAGED if events is not None
                else CollectionRecoveryOutcome.UNSUPPORTED)
    rows = (await session.execute(select(JobItem.id, JobItem.status)
                                  .where(JobItem.job_id == job_id)
                                  .execution_options(autoflush=False))).all()
    statuses = [r.status for r in rows]
    n, c, f = len(rows), statuses.count("completed"), statuses.count("failed")
    if not n or any(s not in {*active, "queued", "completed", "failed"} for s in statuses):
        return CollectionRecoveryOutcome.UNSUPPORTED
    # Only PRE-normalization committed facts may select normal terminal codes.
    if c + f == n:
        helper = (stage_collection_success_settlement if c == n
                  else stage_collection_failure_settlement)
        won = await helper(session, job_id=job_id, owner_id=owner_id)
        return CollectionRecoveryOutcome.STAGED if won else CollectionRecoveryOutcome.NO_CHANGE
    error = JobError(JobErrorCode.INTERRUPTED_BY_RESTART)
    for row in rows:
        if row.status not in active:
            continue
        result = await session.execute(update(JobItem).where(
            JobItem.id == row.id, JobItem.job_id == job_id, JobItem.status == row.status,
        ).values(
            status="failed", error_code=error.code.value, error_message=error.message,
            error_detail=None, completed_at=now, updated_at=now,
            speed_bytes_per_second=None, eta_seconds=None,
        ).execution_options(autoflush=False, synchronize_session=False))
        if cast(CursorResult[Any], result).rowcount != 1:
            return CollectionRecoveryOutcome.NO_CHANGE
        f += 1
    result = await session.execute(update(DownloadJob).where(
        *eligible, DownloadJob.status.in_(active),
        DownloadJob.collection_cancel_requested_at.is_(None),
    ).values(
        status="failed", error_code=error.code.value, error_message=error.message,
        error_detail=None, completed_at=now, updated_at=now,
        total_items=n, completed_items=c, failed_items=f, progress_percent=100.0 * c / n,
        downloaded_bytes=None, total_bytes=None, eta_seconds=None, speed_bytes_per_second=None,
    ).execution_options(autoflush=False, synchronize_session=False))
    if cast(CursorResult[Any], result).rowcount != 1:
        return CollectionRecoveryOutcome.NO_CHANGE
    await add_activity_event(
        session, job_id=job_id, level="error", event_type="failed", message=error.message,
        details={"code": error.code.value, "total_items": n, "completed_items": c,
                 "failed_items": f},
    )
    return CollectionRecoveryOutcome.STAGED


async def stage_collection_retry(
    session: AsyncSession, *, job_id: str, owner_id: str,
) -> bool:
    """Caller-owned reset AFTER full drain, under the terminal writer boundary.

    First SQL is a parent CAS (no autoflush/eligibility SELECT). False or any
    write/commit failure requires whole-transaction rollback, including touch.
    True stages eligible children, actual N/C/F, NULL marker and Activity;
    caller commits/closes before runtime reset, publication or wake.
    Completed and ineligible rows/catalog are never written. No filesystem I/O.
    """
    now = datetime.now(UTC)
    eligible = (
        DownloadJob.id == job_id, DownloadJob.owner_id == owner_id,
        DownloadJob.job_type == "collection", DownloadJob.status.in_(["failed", "cancelled"]),
    )
    result = await session.execute(update(DownloadJob).where(*eligible).values(updated_at=now)
                                   .execution_options(autoflush=False, synchronize_session=False))
    if cast(CursorResult[Any], result).rowcount != 1:
        return False
    parent_status = await session.scalar(select(DownloadJob.status).where(*eligible)
                                         .execution_options(autoflush=False))
    rows = (await session.execute(select(JobItem.id, JobItem.status, JobItem.error_code)
                                   .where(JobItem.job_id == job_id)
                                   .execution_options(autoflush=False))).all()
    if not collection_retryable(parent_status or "", [(r.status, r.error_code) for r in rows]):
        return False
    selected = [r for r in rows if collection_child_retryable(r.status, r.error_code)]
    for row in selected:
        result = await session.execute(update(JobItem).where(
            JobItem.id == row.id, JobItem.job_id == job_id, JobItem.status == row.status,
            JobItem.error_code == row.error_code,
        ).values(
            status="queued", progress_percent=0.0, downloaded_bytes=None, total_bytes=None,
            speed_bytes_per_second=None, eta_seconds=None, error_code=None, error_message=None,
            error_detail=None, started_at=None, completed_at=None, staging_path=None,
            final_path=None, updated_at=now,
        ).execution_options(autoflush=False, synchronize_session=False))
        if cast(CursorResult[Any], result).rowcount != 1:
            return False
    n = len(rows)
    c = sum(r.status == "completed" for r in rows)
    f = sum(r.status == "failed" and not collection_child_retryable(r.status, r.error_code)
            for r in rows)
    result = await session.execute(update(DownloadJob).where(*eligible).values(
        status="queued", collection_cancel_requested_at=None,
        total_items=n, completed_items=c, failed_items=f, progress_percent=100.0 * c / n,
        downloaded_bytes=None, total_bytes=None, speed_bytes_per_second=None, eta_seconds=None,
        error_code=None, error_message=None, error_detail=None, started_at=None, completed_at=None,
        staging_path=None, final_path=None, updated_at=now,
    ).execution_options(autoflush=False, synchronize_session=False))
    if cast(CursorResult[Any], result).rowcount != 1:
        return False
    await add_activity_event(session, job_id=job_id, level="info", event_type="job",
                             message="Retry queued")
    return True


async def stage_collection_cancel_request(
    session: AsyncSession, *, job_id: str, owner_id: str,
) -> CollectionCancelRequestOutcome:
    """First SQL is the writer CAS, without ORM autoflush or a cached read.

    Use a fresh short transaction. STAGED requires caller commit before any
    signal/publication. ALREADY_REQUESTED is idempotent (no timestamp/Activity
    rewrite); REJECTED requires rollback. Exceptions/commit failures require
    whole-transaction rollback. This helper never signals or commits.
    """
    now = datetime.now(UTC)
    eligible = (
        DownloadJob.id == job_id, DownloadJob.owner_id == owner_id,
        DownloadJob.job_type == "collection",
        DownloadJob.status.in_(["queued", *[s.value for s in ACTIVE_JOB_STATUSES]]),
    )
    result = await session.execute(
        update(DownloadJob).where(
            *eligible, DownloadJob.collection_cancel_requested_at.is_(None),
        ).values(collection_cancel_requested_at=now, updated_at=now)
        .execution_options(autoflush=False, synchronize_session=False)
    )
    if cast(CursorResult[Any], result).rowcount == 1:
        await add_activity_event(
            session, job_id=job_id, level="info", event_type="cancel_requested",
            message="Cancellation requested.",
        )
        return CollectionCancelRequestOutcome.STAGED
    # Even a zero-row UPDATE holds the SQLite writer boundary. Read columns,
    # not the identity map, to distinguish duplicate from an ineligible loser.
    marker = await session.scalar(select(DownloadJob.collection_cancel_requested_at)
                                  .where(*eligible).execution_options(autoflush=False))
    return (CollectionCancelRequestOutcome.ALREADY_REQUESTED if marker is not None
            else CollectionCancelRequestOutcome.REJECTED)


async def stage_collection_failure_settlement(
    session: AsyncSession, *, job_id: str, owner_id: str,
) -> bool:
    """Narrow failure-race settlement, never a general Cancel/success reducer.

    Caller MUST drain all attempt-owned work outside the DB writer lock first,
    then use a fresh short transaction. False (including ineligible children)
    MUST roll back the entire transaction, including the initial lock-touch.
    True stages terminal/error/counters/Activity only; caller commits before
    publication and rolls back all writes on any write or commit failure.
    No retry/reset, child mutation, runtime event or commit occurs here.
    """
    now = datetime.now(UTC)
    eligible = (
        DownloadJob.id == job_id, DownloadJob.owner_id == owner_id,
        DownloadJob.job_type == "collection",
        DownloadJob.status.in_(["queued", *[s.value for s in ACTIVE_JOB_STATUSES]]),
    )
    result = await session.execute(
        update(DownloadJob).where(*eligible).values(updated_at=now)
        .execution_options(autoflush=False, synchronize_session=False)
    )
    if cast(CursorResult[Any], result).rowcount != 1:
        return False
    marker = await session.scalar(select(DownloadJob.collection_cancel_requested_at)
                                  .where(*eligible).execution_options(autoflush=False))
    statuses = (await session.scalars(select(JobItem.status).where(JobItem.job_id == job_id)
                                     .execution_options(autoflush=False))).all()
    n, c, f = len(statuses), statuses.count("completed"), statuses.count("failed")
    if n == 0 or f == 0 or c + f != n:
        return False
    cancelled = marker is not None
    error = JobError(JobErrorCode.CANCELLED if cancelled else (
        JobErrorCode.PARTIAL_FAILURE if c else JobErrorCode.ALL_CHILDREN_FAILED
    ))
    result = await session.execute(update(DownloadJob).where(
        *eligible,
        DownloadJob.collection_cancel_requested_at.is_not(None) if cancelled
        else DownloadJob.collection_cancel_requested_at.is_(None),
    ).values(
        status="cancelled" if cancelled else "failed", error_code=error.code.value,
        error_message=error.message, error_detail=None, completed_at=now, updated_at=now,
        total_items=n, completed_items=c, failed_items=f, progress_percent=100.0 * c / n,
        downloaded_bytes=None, total_bytes=None, eta_seconds=None, speed_bytes_per_second=None,
    ).execution_options(autoflush=False, synchronize_session=False))
    if cast(CursorResult[Any], result).rowcount != 1:
        return False
    await add_activity_event(
        session, job_id=job_id, level="info" if cancelled else "error",
        event_type="cancelled" if cancelled else "failed", message=error.message,
        details={"code": error.code.value, "total_items": n, "completed_items": c,
                 "failed_items": f},
    )
    return True


async def stage_collection_success_settlement(
    session: AsyncSession, *, job_id: str, owner_id: str,
) -> bool:
    """Only a nonempty ALL-completed set wins, even with an accepted marker.

    Caller owns full drain, fresh transaction, whole rollback on False/error,
    commit and publication. First SQL acquires the shared SQLite writer boundary.
    No child/catalog mutation or marker reset occurs here.
    """
    now = datetime.now(UTC)
    eligible = (
        DownloadJob.id == job_id, DownloadJob.owner_id == owner_id,
        DownloadJob.job_type == "collection",
        DownloadJob.status.in_(["queued", *[s.value for s in ACTIVE_JOB_STATUSES]]),
    )
    result = await session.execute(update(DownloadJob).where(*eligible).values(updated_at=now)
                                   .execution_options(autoflush=False, synchronize_session=False))
    if cast(CursorResult[Any], result).rowcount != 1:
        return False
    statuses = (await session.scalars(select(JobItem.status).where(JobItem.job_id == job_id)
                                     .execution_options(autoflush=False))).all()
    n = len(statuses)
    if not n or any(s != "completed" for s in statuses):
        return False
    result = await session.execute(update(DownloadJob).where(*eligible).values(
        status="completed", error_code=None, error_message=None, error_detail=None,
        completed_at=now, updated_at=now, total_items=n, completed_items=n, failed_items=0,
        progress_percent=100.0, downloaded_bytes=None, total_bytes=None,
        eta_seconds=None, speed_bytes_per_second=None,
    ).execution_options(autoflush=False, synchronize_session=False))
    if cast(CursorResult[Any], result).rowcount != 1:
        return False
    await add_activity_event(
        session, job_id=job_id, level="info", event_type="completed", message="Completed",
        details={"total_items": n, "completed_items": n, "failed_items": 0},
    )
    return True


async def stage_collection_cancel_settlement(
    session: AsyncSession, *, job_id: str, owner_id: str,
) -> list[ActivityEvent] | None:
    """Caller-owned general Cancel settlement, only AFTER full attempt drain.

    Use a fresh transaction. None requires whole-transaction rollback (including
    lock-touch and child writes); exceptions/commit failures do too. The durable
    request is never cleared. Unknown/incomplete/empty selections are not policy.
    Already-cancelled children are preserved, not reclassified or rewritten.
    """
    now = datetime.now(UTC)
    pending = ["queued", *[s.value for s in ACTIVE_JOB_STATUSES]]
    eligible = (
        DownloadJob.id == job_id, DownloadJob.owner_id == owner_id,
        DownloadJob.job_type == "collection", DownloadJob.status.in_(pending),
        DownloadJob.collection_cancel_requested_at.is_not(None),
    )
    result = await session.execute(update(DownloadJob).where(*eligible).values(updated_at=now)
                                   .execution_options(autoflush=False, synchronize_session=False))
    if cast(CursorResult[Any], result).rowcount != 1:
        return None
    rows = (await session.execute(select(JobItem.id, JobItem.status)
                                  .where(JobItem.job_id == job_id)
                                  .execution_options(autoflush=False))).all()
    statuses = [row.status for row in rows]
    n, c, f = len(statuses), statuses.count("completed"), statuses.count("failed")
    if not n or any(s not in {*pending, "completed", "failed", "cancelled"} for s in statuses):
        return None
    error = JobError(JobErrorCode.CANCELLED)
    # Completed/failed/already-cancelled rows (including every catalog/output
    # fact) are never touched. These timestamps denote actual terminalization.
    result = await session.execute(update(JobItem).where(
        JobItem.job_id == job_id, JobItem.status.in_(pending),
    ).values(status="cancelled", error_code=error.code.value, error_message=error.message,
             error_detail=None, completed_at=now, updated_at=now,
             speed_bytes_per_second=None, eta_seconds=None)
        .execution_options(autoflush=False, synchronize_session=False))
    if cast(CursorResult[Any], result).rowcount != sum(s in pending for s in statuses):
        return None
    completed = c == n
    result = await session.execute(update(DownloadJob).where(*eligible).values(
        status="completed" if completed else "cancelled",
        error_code=None if completed else error.code.value,
        error_message=None if completed else error.message, error_detail=None,
        completed_at=now, updated_at=now, total_items=n, completed_items=c, failed_items=f,
        progress_percent=100.0 * c / n, downloaded_bytes=None, total_bytes=None,
        eta_seconds=None, speed_bytes_per_second=None,
    ).execution_options(autoflush=False, synchronize_session=False))
    if cast(CursorResult[Any], result).rowcount != 1:
        return None
    events = []
    for row in rows:
        if row.status not in pending:
            continue
        events.append(await add_activity_event(
            session, job_id=job_id, level="info", event_type="cancelled",
            message="Cancelled", details={"child_id": row.id},
        ))
    events.append(await add_activity_event(
        session, job_id=job_id, level="info",
        event_type="completed" if completed else "cancelled",
        message="Completed" if completed else "Cancelled",
        details={"total_items": n, "completed_items": c, "failed_items": f},
    ))
    return events


async def stage_child_transition(
    session: AsyncSession, *, job_id: str, owner_id: str, child_id: str,
    expected_status: JobStatus, status: JobStatus, error: JobError | None = None,
) -> bool:
    """Only native pipeline forward transitions and nonterminal-parent failure.

    Cancellation, reset/retry and recovery intentionally have no entry point.
    Completed is only reachable through atomic catalog completion below.
    """
    forward = {
        JobStatus.QUEUED: {JobStatus.ANALYZING},
        JobStatus.ANALYZING: {JobStatus.DOWNLOADING},
        JobStatus.DOWNLOADING: {JobStatus.POST_PROCESSING, JobStatus.FINALIZING},
        JobStatus.POST_PROCESSING: {JobStatus.FINALIZING},
    }
    failing = status == JobStatus.FAILED and expected_status in ACTIVE_JOB_STATUSES
    if not failing and status not in forward.get(expected_status, set()):
        raise ValueError("Unsupported internal child transition")
    if failing != (error is not None):
        raise ValueError("Only failure requires an error")
    values: dict[str, Any] = {"status": status.value, "speed_bytes_per_second": None,
                              "eta_seconds": None}
    if expected_status == JobStatus.QUEUED:
        values["started_at"] = datetime.now(UTC)
    if error is not None:
        values.update(error_code=error.code.value, error_message=error.message,
                      error_detail=error.detail, completed_at=datetime.now(UTC))
    won = await jobs._stage_collection_child_cas(
        session, job_id, owner_id, child_id, expected_status.value, values
    )
    if won:
        await jobs.stage_collection_aggregates(session, job_id, owner_id)
    return won


async def stage_child_progress(
    session: AsyncSession, *, job_id: str, owner_id: str, child_id: str,
    progress_percent: float, downloaded_bytes: int | None = None,
    total_bytes: int | None = None, speed_bytes_per_second: int | None = None,
    eta_seconds: int | None = None,
) -> bool:
    """Transfer observations cannot complete a child or overwrite terminal facts."""
    if not 0 <= progress_percent <= 100:
        raise ValueError("Invalid transfer percentage")
    won = await jobs._stage_collection_child_cas(
        session, job_id, owner_id, child_id, "downloading", dict(
            progress_percent=progress_percent, downloaded_bytes=downloaded_bytes,
            total_bytes=total_bytes, speed_bytes_per_second=speed_bytes_per_second,
            eta_seconds=eta_seconds,
        ),
    )
    if won:
        await jobs.stage_collection_aggregates(session, job_id, owner_id)
    return won


async def stage_child_completion(
    session: AsyncSession, storage: StorageService, *, job_id: str, owner_id: str,
    child_id: str, selected: NormalizedFormat, placement: PlacementResult,
    duration_seconds: float | None = None, platform: str | None = None,
) -> bool:
    """CAS -> catalog insert/reverify -> aggregates, all in one caller transaction.

    Placement must already be finalized by StorageService. Losing CAS never
    builds/inserts catalog rows; stable identity collision raises, never upserts.
    """
    now = datetime.now(UTC)
    won = await jobs._stage_collection_child_cas(
        session, job_id, owner_id, child_id, "finalizing", dict(
            status="completed", progress_percent=100.0,
            final_path=placement.relative_path, completed_at=now,
            speed_bytes_per_second=None, eta_seconds=None,
        ),
    )
    if not won:
        return False
    parent, child = (await session.execute(
        select(DownloadJob, JobItem).join(JobItem, JobItem.job_id == DownloadJob.id)
        .where(DownloadJob.id == job_id, JobItem.id == child_id)
        .execution_options(populate_existing=True)
    )).one()
    audio = selected.kind == "audio_only"
    item = LibraryItem(
        id=child.id, job_id=parent.id, owner_id=parent.owner_id,
        source_url=child.source_url, source_id=child.source_id,
        title=child.title or "Untitled", track_number=child.track_number,
        disc_number=child.disc_number, platform=platform,
        media_type="audio" if audio else "video",
        format=placement.final_path.suffix.lstrip(".") or None,
        codec=selected.audio_codec if audio else selected.video_codec,
        bitrate=selected.bitrate_kbps, resolution=selected.resolution_label,
        duration=duration_seconds, created_at=now,
    )
    media = MediaFile(
        library_item_id=child.id, storage_type="app_library",
        path=placement.relative_path, size_bytes=placement.size_bytes, created_at=now,
    )
    await stage_media_catalog_reference(
        session, storage, item, media, placement_identity=placement.identity
    )
    await jobs.stage_collection_aggregates(session, job_id, owner_id)
    return True
