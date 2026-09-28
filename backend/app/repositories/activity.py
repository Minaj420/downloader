import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jobs import ActivityEvent


async def max_event_id(session: AsyncSession) -> int:
    """Current high-water mark for SSE replay (0 when the table is empty)."""
    result = await session.execute(select(func.max(ActivityEvent.id)))
    value = result.scalar_one()
    return int(value) if value is not None else 0


async def add_activity_event(
    session: AsyncSession,
    *,
    job_id: str | None,
    level: str,
    event_type: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> ActivityEvent:
    record = ActivityEvent(
        job_id=job_id,
        level=level,
        event_type=event_type,
        message=message,
        details_json=json.dumps(details) if details is not None else None,
        created_at=datetime.now(UTC),
    )
    session.add(record)
    await session.flush()
    return record


async def list_events_after(
    session: AsyncSession,
    last_id: int,
    *,
    limit: int = 200,
    max_id: int | None = None,
) -> list[ActivityEvent]:
    """Rows with ``last_id < id`` (and ``id <= max_id`` when given), id ASC."""
    query = (
        select(ActivityEvent)
        .where(ActivityEvent.id > last_id)
        .order_by(ActivityEvent.id.asc())
        .limit(limit)
    )
    if max_id is not None:
        query = query.where(ActivityEvent.id <= max_id)
    result = await session.execute(query)
    return list(result.scalars().all())


async def latest_events_for_job(
    session: AsyncSession, job_id: str, *, limit: int = 5
) -> list[ActivityEvent]:
    result = await session.execute(
        select(ActivityEvent)
        .where(ActivityEvent.job_id == job_id)
        .order_by(ActivityEvent.id.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def list_events_for_job(session: AsyncSession, job_id: str) -> list[ActivityEvent]:
    """Full activity history for one job, id ASC.

    Serves the job activity REST endpoint (initial history for the
    Activity panel). A single job's history stays small in v0.1, so no
    limit is applied.
    """
    result = await session.execute(
        select(ActivityEvent)
        .where(ActivityEvent.job_id == job_id)
        .order_by(ActivityEvent.id.asc())
    )
    return list(result.scalars().all())
