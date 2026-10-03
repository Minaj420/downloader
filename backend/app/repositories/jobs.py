from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.jobs import ACTIVE_JOB_STATUSES, JobStatus
from app.models.jobs import DownloadJob


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
    return result.scalar_one_or_none()


async def get_job_any_owner(session: AsyncSession, job_id: str) -> DownloadJob | None:
    return await session.get(DownloadJob, job_id)


async def list_jobs(
    session: AsyncSession, owner_id: str, *, limit: int = 50
) -> list[DownloadJob]:
    result = await session.execute(
        select(DownloadJob)
        .where(DownloadJob.owner_id == owner_id)
        .order_by(DownloadJob.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


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
    session: AsyncSession, *, limit: int
) -> list[DownloadJob]:
    result = await session.execute(
        select(DownloadJob)
        .where(DownloadJob.status == JobStatus.QUEUED.value)
        .order_by(DownloadJob.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def mark_interrupted_active_jobs(session: AsyncSession) -> list[str]:
    """Restart recovery (ARCHITECTURE.md §21): active jobs become retryable failures."""
    now = _now()
    result = await session.execute(
        update(DownloadJob)
        .where(DownloadJob.status.in_([status.value for status in ACTIVE_JOB_STATUSES]))
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
