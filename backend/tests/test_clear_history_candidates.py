"""Clear History candidate count/list, read-only (ARCHITECTURE §59 M6-Q Q3a)."""

import dataclasses
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jobs import DownloadJob, JobItem
from app.repositories import jobs as job_repo
from app.repositories.jobs import ClearHistoryCandidate
from tests.test_job_listing import (
    ACTIVE,
    BASE,
    TERMINAL,
    app_of,
    collection,
    make_child,
    make_job,
    seed,
)
from tests.test_job_listing import paused_queue as paused_queue


def in_session[T](client: TestClient, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def run() -> T:
        async with app_of(client).state.sessionmaker() as session:
            return await work(session)

    assert client.portal is not None
    return client.portal.call(run)


def count(client: TestClient, owner: str = "local", *, include_cancelled: bool) -> int:
    return in_session(client, lambda s: job_repo.count_clear_history_candidates(
        s, owner, include_cancelled=include_cancelled,
    ))


def candidates(
    client: TestClient, owner: str = "local", *, include_cancelled: bool,
) -> list[ClearHistoryCandidate]:
    return in_session(client, lambda s: job_repo.list_clear_history_candidates(
        s, owner, include_cancelled=include_cancelled,
    ))


@contextmanager
def capture_statements(client: TestClient) -> Iterator[list[str]]:
    statements: list[str] = []
    engine = app_of(client).state.engine.sync_engine

    def listener(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def snapshot(client: TestClient) -> tuple[list[Any], list[Any]]:
    async def read(session: AsyncSession) -> tuple[list[Any], list[Any]]:
        jobs = await session.execute(select(DownloadJob.__table__).order_by(DownloadJob.id))
        items = await session.execute(select(JobItem.__table__).order_by(JobItem.id))
        return [tuple(r) for r in jobs], [tuple(r) for r in items]

    return in_session(client, read)


@pytest.mark.parametrize("include_cancelled", [False, True])
def test_owner_and_status_filter_for_both_job_types(
    client: TestClient, include_cancelled: bool,
) -> None:
    statuses = [*ACTIVE, *TERMINAL, "mystery"]
    singles = [make_job(s, BASE + timedelta(seconds=i)) for i, s in enumerate(statuses)]
    parents: list[DownloadJob] = []
    items: list[JobItem] = []
    for i, status in enumerate(statuses):
        parent, children = collection(
            BASE + timedelta(minutes=1, seconds=i), status, [("completed", None)],
        )
        parents.append(parent)
        items += children
    cancel_requested = make_job("queued", BASE + timedelta(minutes=2), job_type="collection")
    cancel_requested.collection_cancel_requested_at = BASE
    foreign = [
        make_job(s, BASE + timedelta(minutes=3), owner="other", job_type=t)
        for s in TERMINAL for t in ("single", "collection")
    ]
    seed(client, singles + parents + [cancel_requested] + foreign, items)

    wanted = {"completed", "failed"} | ({"cancelled"} if include_cancelled else set())
    expected = [r.id for r in singles + parents if r.status in wanted]
    listed = candidates(client, include_cancelled=include_cancelled)
    assert [c.job_id for c in listed] == expected
    assert {c.job_type for c in listed} == {"single", "collection"}
    assert count(client, include_cancelled=include_cancelled) == len(listed) == len(expected)
    assert count(client, "other", include_cancelled=include_cancelled) == (
        6 if include_cancelled else 4
    )
    assert count(client, "nobody", include_cancelled=include_cancelled) == 0
    assert candidates(client, "nobody", include_cancelled=include_cancelled) == []


def test_staging_keys_are_backend_ids_never_staging_paths(client: TestClient) -> None:
    single = make_job("failed", BASE)
    single.staging_path = "/staging/not-a-key"
    stray = make_child(single, 1, "completed")  # Corrupt child row: never a single key.
    mixed, mixed_items = collection(
        BASE + timedelta(seconds=1), "cancelled",
        [("completed", None), ("failed", "network_error"), ("queued", None),
         ("cancelled", "cancelled"), ("downloading", None), ("mystery", None)],
        error_code="cancelled",
    )
    mixed.staging_path = "/staging/parent"
    for child in mixed_items:
        child.staging_path = f"/staging/path-{child.source_order}"
    empty, _ = collection(BASE + timedelta(seconds=2), "completed", [])
    seed(client, [single, mixed, empty], [stray, *mixed_items])

    listed = candidates(client, include_cancelled=True)
    by_order = sorted(mixed_items, key=lambda c: c.source_order)
    assert listed == [
        ClearHistoryCandidate(single.id, "single", (single.id,)),
        ClearHistoryCandidate(mixed.id, "collection", tuple(c.id for c in by_order)),
        ClearHistoryCandidate(empty.id, "collection", ()),
    ]
    keys = {key for c in listed for key in c.staging_keys}
    assert mixed.id not in keys and stray.id not in keys
    assert all(not key.startswith("/") for key in keys)
    assert count(client, include_cancelled=True) == 3
    assert [c.job_id for c in candidates(client, include_cancelled=False)] == [
        single.id, empty.id,
    ]


def test_order_is_deterministic_on_created_at_ties(client: TestClient) -> None:
    tied = [make_job("completed", BASE, job_id=f"{n:032x}") for n in (9, 3, 7)]
    older = make_job("failed", BASE - timedelta(microseconds=1), job_id="f" * 32)
    seed(client, [*tied, older])
    expected = [older.id, *sorted(r.id for r in tied)]
    for _ in range(2):
        assert [c.job_id for c in candidates(client, include_cancelled=False)] == expected


def test_candidate_is_immutable(client: TestClient) -> None:
    seed(client, [make_job("completed", BASE)])
    candidate = candidates(client, include_cancelled=False)[0]
    assert isinstance(candidate.staging_keys, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.job_id = "x"  # type: ignore[misc]


def test_read_only_single_select_each_independent_of_collection_count(
    client: TestClient,
) -> None:
    def add(offset: int) -> None:
        parent, children = collection(
            BASE + timedelta(seconds=offset), "failed",
            [("completed", None), ("failed", "network_error")],
            error_code="partial_failure",
        )
        seed(client, [parent], children)
        seed(client, [make_job("cancelled", BASE + timedelta(seconds=offset, microseconds=1))])

    def measure() -> tuple[list[str], list[str]]:
        before = snapshot(client)
        with capture_statements(client) as counted:
            count(client, include_cancelled=True)
        with capture_statements(client) as listed:
            candidates(client, include_cancelled=True)
        assert snapshot(client) == before
        return counted, listed

    add(0)
    small_count, small_list = measure()
    for offset in range(1, 8):
        add(offset)
    large_count, large_list = measure()
    for statements in (small_count, small_list, large_count, large_list):
        assert len(statements) == 1
        assert statements[0].lstrip().upper().startswith("SELECT")
    assert "count(" in large_count[0].lower()
    assert len(candidates(client, include_cancelled=True)) == 16

    async def no_pending(session: AsyncSession) -> None:
        await job_repo.list_clear_history_candidates(session, "local", include_cancelled=True)
        await job_repo.count_clear_history_candidates(session, "local", include_cancelled=True)
        assert not session.new and not session.dirty and not session.deleted

    in_session(client, no_pending)


def test_count_does_not_autoflush_pending_status_change(client: TestClient) -> None:
    record = make_job("completed", BASE)
    seed(client, [record])
    before = snapshot(client)

    async def run(session: AsyncSession) -> None:
        loaded = await session.get(DownloadJob, record.id)
        assert loaded is not None
        loaded.status = "queued"  # Would drop the count if flushed.
        try:
            with capture_statements(client) as statements:
                assert await job_repo.count_clear_history_candidates(
                    session, "local", include_cancelled=False,
                ) == 1
            assert len(statements) == 1
            assert statements[0].lstrip().upper().startswith("SELECT")
            assert loaded in session.dirty
        finally:
            await session.rollback()

    in_session(client, run)
    assert snapshot(client) == before


def test_list_does_not_autoflush_pending_status_change(client: TestClient) -> None:
    record = make_job("queued", BASE)
    seed(client, [record])
    before = snapshot(client)

    async def run(session: AsyncSession) -> None:
        loaded = await session.get(DownloadJob, record.id)
        assert loaded is not None
        loaded.status = "completed"  # Would add a candidate if flushed.
        try:
            with capture_statements(client) as statements:
                assert await job_repo.list_clear_history_candidates(
                    session, "local", include_cancelled=False,
                ) == []
            assert len(statements) == 1
            assert statements[0].lstrip().upper().startswith("SELECT")
            assert loaded in session.dirty
        finally:
            await session.rollback()

    in_session(client, run)
    assert snapshot(client) == before


def test_unknown_job_type_fails_closed(client: TestClient) -> None:
    async def run(session: AsyncSession) -> None:
        # The schema CHECK forbids this; bypass it only inside this rolled-back session.
        await session.execute(text("PRAGMA ignore_check_constraints = ON"))
        try:
            session.add(make_job("completed", BASE, job_type="mystery"))
            await session.flush()
            assert await job_repo.count_clear_history_candidates(
                session, "local", include_cancelled=False,
            ) == 1
            with pytest.raises(ValueError, match="Unsupported job type"):
                await job_repo.list_clear_history_candidates(
                    session, "local", include_cancelled=False,
                )
        finally:
            await session.rollback()
            await session.execute(text("PRAGMA ignore_check_constraints = OFF"))

    in_session(client, run)
    assert count(client, include_cancelled=True) == 0
