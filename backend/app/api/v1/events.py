"""SSE stream for live job/activity updates (ARCHITECTURE.md §40–42).

Events use the names from the architecture (``job.updated``, ``job.completed``,
``job.failed``, ``job.cancelled``, ``activity.created``). ``activity.created``
events carry the monotonic database id so reconnecting clients can replay
missed activity via ``Last-Event-ID``; job state itself is always recoverable
through ``GET /api/v1/jobs/{id}``.

Replay semantics (R1): when ``Last-Event-ID`` is present the connection reads
a one-shot high-water mark (``MAX(activity_events.id)``) and replays the
backlog in id-ordered pages strictly bounded to ``cursor < id <= mark`` — the
upper bound is enforced in the page query (SQL) and re-checked per row
(Python) — so no persisted id is skipped regardless of backlog size and no id
above the mark leaks into the replay. Persisted activity ids at or below the
mark are suppressed from the live lane for this connection, so a persisted id
is never delivered twice; ids above the mark (committed after it was read)
still arrive live exactly once. Transient events (``id: None``) are never
deduped, and without the header no replay happens at all.

First-connect semantics (R5): a connection WITHOUT ``Last-Event-ID`` (the
browser's initial connect) is live-only — no backlog is ever replayed to it.
Initial activity history is instead fetched from the REST resource
(``GET /api/v1/jobs/{job_id}/activity``); after an SSE drop the browser
reconnects automatically (with ``Last-Event-ID``) and the UI refreshes
authoritative state from REST (ARCHITECTURE.md §42). This split is locked by
``test_first_connect_without_last_event_id_replays_nothing``.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.repositories import activity
from app.utils.datetime import utc_isoformat

router = APIRouter(tags=["events"])

_HEARTBEAT_SECONDS = 15.0
_REPLAY_PAGE_SIZE = 200


def format_sse_event(event: str, data: str, event_id: int | None = None) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    for chunk in data.splitlines() or [""]:
        lines.append(f"data: {chunk}")
    return "\n".join(lines) + "\n\n"


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    bus = request.app.state.event_bus
    session_factory = request.app.state.sessionmaker
    last_event_id = request.headers.get("Last-Event-ID")
    # StreamingResponse sends response.start before it starts iterating the
    # body. Subscribe here so an event published immediately after the client
    # observes `open` is already buffered for this connection.
    subscriber = bus.subscribe()

    async def stream() -> AsyncIterator[str]:
        try:
            # Subscribed before reading the high-water mark: anything
            # published from here on is buffered and can be delivered
            # live, so ids committed after the mark cannot be missed.
            replay_hwm: int | None = None
            if last_event_id is not None:
                cursor = _parse_event_id(last_event_id)
                async with session_factory() as session:
                    replay_hwm = await activity.max_event_id(session)
                    async for chunk in _replay_backlog(session, cursor, replay_hwm):
                        yield chunk
            while True:
                try:
                    event = await subscriber.get(timeout=_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:
                    # The bus disconnected this subscriber (persisted
                    # activity overflow): end the stream cleanly so the
                    # client reconnects and replays the skipped activity
                    # ids from the database via Last-Event-ID.
                    return
                event_id = event.get("id")
                if (
                    replay_hwm is not None
                    and event_id is not None
                    and event_id <= replay_hwm
                ):
                    # Persisted activity already covered by this
                    # connection's replay. Transient events (id None)
                    # never carry an id and are never skipped.
                    continue
                yield format_sse_event(
                    event["event"],
                    json.dumps(_live_payload(event)),
                    event_id=event_id,
                )
        finally:
            bus.unsubscribe(subscriber)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        # The generator's finally handles normal iteration/cancellation. The
        # background cleanup also covers a disconnect before body iteration.
        background=BackgroundTask(bus.unsubscribe, subscriber),
    )


async def _replay_backlog(
    session: AsyncSession, cursor: int, replay_hwm: int
) -> AsyncIterator[str]:
    """Yield the persisted backlog ``cursor < id <= replay_hwm`` in pages.

    The session must stay open for the duration of the iteration; the
    caller closes it once the backlog is exhausted, before the live loop.
    The upper bound is applied in every page query itself (SQL-level
    ``id <= replay_hwm``), so rows committed after the high-water mark
    was read never enter a page; any over-bound row that slips through
    anyway is skipped in Python so it is still delivered exactly once
    from the bus while no at-or-below-mark row is dropped.
    """
    while cursor < replay_hwm:
        page = await activity.list_events_after(
            session, cursor, limit=_REPLAY_PAGE_SIZE, max_id=replay_hwm
        )
        if not page:
            break
        page_cursor = cursor
        for replay_event in page:
            if replay_event.id > replay_hwm:
                # Defense in depth: the SQL bound above already excludes
                # this row, so reaching here means an over-bound row was
                # returned anyway. Skip it (it is still delivered live)
                # without dropping any at-or-below-mark row after it.
                continue
            payload = _activity_payload(replay_event)
            yield format_sse_event(
                "activity.created", json.dumps(payload), event_id=replay_event.id
            )
            cursor = replay_event.id
        if cursor == page_cursor:
            # The page held only rows above the mark: nothing was
            # replayed and the cursor cannot advance. Stop instead of
            # re-fetching the same page forever.
            break


def _parse_event_id(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        return 0


def _activity_payload(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "job_id": event.job_id,
        "level": event.level,
        "event_type": event.event_type,
        "message": event.message,
        "created_at": utc_isoformat(event.created_at),
    }


def _live_payload(event: dict[str, Any]) -> Any:
    """Normalize live activity timestamps to the REST/replay UTC contract."""
    data = event["data"]
    if event["event"] != "activity.created" or not isinstance(data, dict):
        return data
    created_at = data.get("created_at")
    if not isinstance(created_at, str):
        return data
    return {**data, "created_at": utc_isoformat(created_at)}
