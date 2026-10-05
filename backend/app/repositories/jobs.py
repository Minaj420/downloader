from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, case, func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.jobs import (
    ACTIVE_JOB_STATUSES,
    JobErrorCode,
    JobStatus,
    job_error_code_retryable,
)
from app.models.jobs import DownloadJob, JobItem


def _now() -> datetime:
    return datetime.now(UTC)


async def add_job(session: AsyncSession, record: DownloadJob) -> None:
    """Stage a new job row in the caller's transaction.

    Flushes so the insert (and any generated defaults) are visible inside
    the current transaction, but leaves the commit to the caller. This
    keeps job creation atomic with related writes, such as the initial
    "Queued" activity event, within a single commit.
    """
    session.add(record)
    await session.flush()


async def get_job(
    session: AsyncSession, job_id: str, owner_id: str
) -> DownloadJob | None:
    result = await session.execute(
        select(DownloadJob).where(
            DownloadJob.id == job_id,
            DownloadJob.owner_id == owner_id,
        )
    )
    record = result.scalar_one_or_none()
    if record is not None:
        await _preload_collection_retry_children(session, [record])
    return record


async def _preload_collection_retry_children(
    session: AsyncSession, records: Sequence[DownloadJob],
) -> None:
    """Internal read enrichment for synchronous snapshots; no DTO/model fields.

    Queries only already owner-scoped collection IDs, in one bounded read.
    Unknown statuses remain raw facts, not invented DTO enum values.
    """
    collections = {record.id: record for record in records if record.job_type == "collection"}
    if not collections:
        return
    facts: dict[str, list[tuple[str, str | None]]] = {job_id: [] for job_id in collections}
    rows = await session.execute(select(JobItem.job_id, JobItem.status, JobItem.error_code)
                                 .where(JobItem.job_id.in_(collections)))
    for job_id, status, code in rows:
        facts[job_id].append((status, code))
    for job_id, record in collections.items():
        record.__dict__["_collection_retry_children"] = facts[job_id]


async def stage_collection_job(
    session: AsyncSession,
    parent: DownloadJob,
    children: Sequence[JobItem],
    *,
    owner_id: str,
) -> None:
    """Insert a new owned parent and selection in the caller's transaction.

    Only structural integrity is checked here; owned snapshot validation,
    selection limits and profile support belong to the create service.
    On any flush failure the caller must roll back, including its activity writes.
    No existing parent or child can be mutated through this insertion helper.
    """
    if not inspect(parent).transient or parent.owner_id != owner_id:
        raise ValueError("A new parent owned by the caller is required")
    if parent.job_type != "collection":
        raise ValueError("A collection parent is required")
    ids: set[str] = set()
    item_ids: set[str] = set()
    orders: set[int] = set()
    for child in children:
        if not inspect(child).transient or child.job_id != parent.id:
            raise ValueError("Each new child must reference this parent")
        if not child.id or not child.item_id or child.source_order < 1:
            raise ValueError("Child identity and positive source order are required")
        if child.id in ids or child.item_id in item_ids or child.source_order in orders:
            raise ValueError("Duplicate child identity or source order")
        ids.add(child.id)
        item_ids.add(child.item_id)
        orders.add(child.source_order)
    parent.total_items = len(children)
    session.add(parent)
    # Explicit ordering avoids relying on an ORM relationship for FK insert order.
    await session.flush()
    session.add_all(children)
    await session.flush()


async def list_job_items(
    session: AsyncSession, job_id: str, owner_id: str
) -> list[JobItem]:
    """Read selected children in raw snapshot order, scoped through parent ownership."""
    result = await session.execute(
        select(JobItem)
        .join(DownloadJob, JobItem.job_id == DownloadJob.id)
        .where(DownloadJob.id == job_id, DownloadJob.owner_id == owner_id)
        .order_by(JobItem.source_order.asc())
    )
    return list(result.scalars().all())


async def get_job_any_owner(session: AsyncSession, job_id: str) -> DownloadJob | None:
    record = await session.get(DownloadJob, job_id)
    if record is not None:
        await _preload_collection_retry_children(session, [record])
    return record


async def stage_collection_claim(
    session: AsyncSession, job_id: str, owner_id: str
) -> bool:
    """Internal only: caller owns commit; does not activate queue dispatch."""
    result = await session.execute(
        update(DownloadJob).where(
            DownloadJob.id == job_id, DownloadJob.owner_id == owner_id,
            DownloadJob.job_type == "collection", DownloadJob.status == "queued",
            DownloadJob.collection_cancel_requested_at.is_(None),
        ).values(status="analyzing", started_at=_now(), updated_at=_now())
        .execution_options(autoflush=False)
    )
    return cast(CursorResult[Any], result).rowcount == 1


async def _stage_collection_child_cas(
    session: AsyncSession, job_id: str, owner_id: str, child_id: str,
    expected_status: str, values: dict[str, Any],
) -> bool:
    """First statement acquires SQLite's write lock, before any catalog read.

    Private primitive: only field-scoped service operations may call this.
    Status-only attempt eligibility requires draining old helpers before reset.
    No commit; a failed subsequent write requires caller rollback.
    """
    eligible_parent = select(DownloadJob.id).where(
        DownloadJob.id == job_id, DownloadJob.owner_id == owner_id,
        DownloadJob.job_type == "collection",
        DownloadJob.status.in_([status.value for status in ACTIVE_JOB_STATUSES]),
    )
    # Only actual active -> failed recording may bypass the request marker.
    # Derive this from the scoped write, never from an arbitrary caller flag.
    actual_failure = (
        expected_status in {s.value for s in ACTIVE_JOB_STATUSES}
        and values.get("status") == "failed"
        and values.get("error_code") is not None
    )
    if not actual_failure:
        eligible_parent = eligible_parent.where(
            DownloadJob.collection_cancel_requested_at.is_(None)
        )
    result = await session.execute(
        update(JobItem).where(
            JobItem.id == child_id, JobItem.job_id == job_id,
            JobItem.status == expected_status,
            JobItem.status.in_(["queued", *[s.value for s in ACTIVE_JOB_STATUSES]]),
            JobItem.job_id.in_(eligible_parent),
        ).values(**values, updated_at=_now()).execution_options(autoflush=False)
    )
    return cast(CursorResult[Any], result).rowcount == 1


async def stage_collection_aggregates(
    session: AsyncSession, job_id: str, owner_id: str
) -> None:
    """Recompute facts under the preceding child CAS lock; never settle parent."""
    n, c, f = (await session.execute(select(
        func.count(JobItem.id),
        func.coalesce(func.sum(case((JobItem.status == "completed", 1), else_=0)), 0),
        func.coalesce(func.sum(case((JobItem.status == "failed", 1), else_=0)), 0),
    ).where(JobItem.job_id == job_id))).one()
    await session.execute(update(DownloadJob).where(
        DownloadJob.id == job_id, DownloadJob.owner_id == owner_id,
        DownloadJob.job_type == "collection",
        DownloadJob.status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
    ).values(
        total_items=n, completed_items=c, failed_items=f,
        progress_percent=100.0 * c / n if n else 0.0,
        downloaded_bytes=None, total_bytes=None, eta_seconds=None,
        speed_bytes_per_second=None, updated_at=_now(),
    ))


async def list_jobs(
    session: AsyncSession, owner_id: str, *, limit: int = 50
) -> list[DownloadJob]:
    result = await session.execute(
        select(DownloadJob)
        .where(DownloadJob.owner_id == owner_id)
        .order_by(DownloadJob.created_at.desc())
        .limit(limit)
    )
    records = list(result.scalars().all())
    await _preload_collection_retry_children(session, records)
    return records


async def update_job(
    session: AsyncSession, job_id: str, values: dict[str, Any]
) -> None:
    await session.execute(
        update(DownloadJob)
        .where(DownloadJob.id == job_id)
        .values(**values, updated_at=_now())
    )
    await session.commit()


async def stage_job_update_if_status(
    session: AsyncSession,
    job_id: str,
    expected_statuses: set[str],
    values: dict[str, Any],
) -> bool:
    """Atomically update a job only while its status still matches, without
    committing.

    The caller keeps the transaction open so related rows (for example the
    Library catalog written at completion) commit together with this
    update or not at all.
    """
    result = await session.execute(
        update(DownloadJob)
        .where(DownloadJob.id == job_id, DownloadJob.status.in_(expected_statuses))
        .values(**values, updated_at=_now())
    )
    return cast(CursorResult[Any], result).rowcount > 0


async def update_job_if_status(
    session: AsyncSession,
    job_id: str,
    expected_statuses: set[str],
    values: dict[str, Any],
) -> bool:
    """Atomically update a job only while its status still matches.

    Returns ``False`` when the job was concurrently transitioned (for example
    cancelled), so callers never revive a terminal job from a stale
    in-memory snapshot.
    """
    won = await stage_job_update_if_status(session, job_id, expected_statuses, values)
    if won:
        await session.commit()
    return won


async def claimable_queued_jobs(
    session: AsyncSession, *, limit: int, include_collections: bool = True
) -> list[DownloadJob]:
    result = await session.execute(
        select(DownloadJob)
        # Supported parent kinds share the global queue, before LIMIT.
        .where(
            DownloadJob.status == JobStatus.QUEUED.value,
            DownloadJob.job_type.in_(
                ("single", "collection") if include_collections else ("single",)
            ),
        )
        .order_by(DownloadJob.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def stage_retry_job(session: AsyncSession, job_id: str, owner_id: str) -> bool:
    """Compare-and-set at the actual write; caller owns activity and commit."""
    result = await session.execute(
        update(DownloadJob)
        .where(
            DownloadJob.id == job_id,
            DownloadJob.owner_id == owner_id,
            DownloadJob.status == JobStatus.FAILED.value,
            DownloadJob.error_code.in_(
                [code.value for code in JobErrorCode if job_error_code_retryable(code)]
            ),
        )
        .values(
            status=JobStatus.QUEUED.value,
            progress_percent=0.0,
            downloaded_bytes=None,
            total_bytes=None,
            speed_bytes_per_second=None,
            eta_seconds=None,
            error_code=None,
            error_message=None,
            error_detail=None,
            started_at=None,
            completed_at=None,
            final_path=None,
            staging_path=None,
            updated_at=_now(),
        )
    )
    return cast(CursorResult[Any], result).rowcount > 0


async def mark_interrupted_active_jobs(session: AsyncSession) -> list[str]:
    """Restart recovery (ARCHITECTURE.md §21): active jobs become retryable failures."""
    now = _now()
    result = await session.execute(
        update(DownloadJob)
        .where(
            DownloadJob.job_type == "single",
            DownloadJob.status.in_([status.value for status in ACTIVE_JOB_STATUSES]),
        )
        .values(
            status=JobStatus.FAILED.value,
            error_code="interrupted_by_restart",
            error_message=(
                "The application restarted while this job was running. You can retry it."
            ),
            updated_at=now,
            completed_at=now,
        )
        .returning(DownloadJob.id)
    )
    return [row[0] for row in result.all()]
