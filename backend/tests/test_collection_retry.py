import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, event, text, update
from sqlalchemy.exc import IntegrityError

from app.domain.actors import Actor
from app.domain.jobs import (
    JobError,
    JobErrorCode,
    collection_child_retryable,
    collection_retryable,
    job_error_code_retryable,
)
from app.models.jobs import DownloadJob, JobItem
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs
from app.services import collection_persistence as persistence
from app.services import job_service as service_module
from app.services.job_mapping import item_to_dto, job_to_dto
from tests.test_collection_cancel import checkpoint, runtime
from tests.test_collection_persistence import complete, placed, snapshot
from tests.test_collection_repository import child, factory, parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


CODES = [code.value for code in JobErrorCode] + [None, "unknown"]


@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("status", ["failed", "cancelled", "queued", "analyzing",
                                   "downloading", "post_processing", "finalizing",
                                   "completed", "unknown", "incomplete"])
def test_locked_child_policy_and_transient_override(status: str, code: str | None) -> None:
    expected = status in {"cancelled", "queued", "analyzing", "downloading",
                          "post_processing", "finalizing"} or (
        status == "failed" and job_error_code_retryable(code or ""))
    assert collection_child_retryable(status, code) == expected
    assert collection_retryable("failed", [(status, code)]) == expected
    assert collection_retryable("cancelled", [(status, code)]) == expected
    for terminal in ("completed", "queued", "analyzing", "downloading", "post_processing",
                     "finalizing", "unknown"):
        assert not collection_retryable(terminal, [(status, code)])
    # A transient override cannot change the persisted classifier.
    if code in {c.value for c in JobErrorCode}:
        error = JobError(JobErrorCode(code), retryable=not expected)
        assert collection_child_retryable(status, error.code.value) == expected


async def seed(sessions: Any, *, status: str = "failed", children: list[JobItem] | None = None):
    now = datetime.now(UTC)
    async with sessions() as session:
        await jobs.stage_collection_job(session, parent(
            status=status, error_code="all_children_failed", error_message="old summary",
            error_detail="old detail", collection_cancel_requested_at=now,
            started_at=now, completed_at=now, staging_path="old staging", final_path="old path",
            downloaded_bytes=123, total_bytes=456, speed_bytes_per_second=7, eta_seconds=8,
        ), children if children is not None else [child(
            status="failed", error_code="network_error", error_message="old child",
            error_detail="child detail", started_at=now, completed_at=now,
            staging_path="placed-uncompleted", final_path="old child path",
            progress_percent=91, downloaded_bytes=23, total_bytes=56,
            speed_bytes_per_second=2, eta_seconds=3,
        )], owner_id="owner")
        await session.commit()


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
@pytest.mark.parametrize("catalog_deleted", [False, True])
async def test_subset_actual_counters_and_completed_catalog_immutable(
    tmp_path: Path, status: str, catalog_deleted: bool,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    storage, placement = await placed(tmp_path)
    try:
        async with sessions() as session:
            await jobs.stage_collection_job(session, parent(status="analyzing"), [
                child(1, status="failed", error_code="unsupported_url", error_detail="preserve"),
                child(2, status="finalizing"),
                child(3, status="failed", error_code="network_error", final_path="uncompleted"),
                child(4, status="cancelled", error_code="unknown"),
                child(5, status="downloading"), child(6, status="unknown"),
                child(7, status="failed"),
            ], owner_id="owner")
            await session.commit()
        async with sessions() as session:
            assert await complete(session, storage, placement)
            if catalog_deleted:
                # Fixture removes the old catalog reference, not the immutable
                # completed child. Retry must not rediscover/redownload it.
                await session.execute(delete(MediaFile))
                await session.execute(delete(LibraryItem))
            await session.execute(update(DownloadJob).values(
                status=status, total_items=99, completed_items=90, failed_items=9,
                collection_cancel_requested_at=datetime.now(UTC),
                error_code="partial_failure", error_message="summary", progress_percent=99))
            await session.commit()
        async with sessions() as session:
            before = await snapshot(session)
        manager.cancel_event_for("parent").set()
        record = await service.retry_job(Actor(id="owner"), "parent")
        assert record is not None
        assert (record.status, record.total_items, record.completed_items, record.failed_items,
                record.progress_percent) == ("queued", 7, 1, 2, 100 / 7)
        assert record.collection_cancel_requested_at is None and record.error_code is None
        assert record.downloaded_bytes is None and record.eta_seconds is None
        async with sessions() as session:
            after = await snapshot(session)
        for table in ("library_items", "media_files"):
            assert after[table] == before[table]
        for index in (0, 1, 5, 6):
            assert after["job_items"][index] == before["job_items"][index]
        for index in (2, 3, 4):
            row = after["job_items"][index]
            assert row["status"] == "queued" and row["progress_percent"] == 0
            for field in ("error_code", "error_message", "error_detail", "started_at",
                          "completed_at", "staging_path", "final_path", "downloaded_bytes",
                          "total_bytes", "speed_bytes_per_second", "eta_seconds"):
                assert row[field] is None
            for field in ("id", "item_id", "source_url", "source_order", "created_at", "title"):
                assert row[field] == before["job_items"][index][field]
        assert len(after["activity_events"]) == len(before["activity_events"]) + 1
        assert [e["event"] for e in bus.events] == ["activity.created", "job.updated"]
        assert bus.events[-1]["data"]["progress"]["percent"] == 100 / 7
        assert not manager.cancel_event_for("parent").is_set() and manager._wake_event.is_set()
        # Actual production guard still rejects this fresh collection.
        await manager._claim_and_run()
        assert "parent" not in manager._tasks
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["queued", "analyzing", "downloading", "post_processing",
                                 "finalizing", "completed", "unknown", "owner", "kind",
                                 "no_eligible", "empty"])
async def test_rejected_reset_preserves_every_fact(tmp_path: Path, case: str) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    try:
        await seed(sessions, status=case if case not in {
            "owner", "kind", "no_eligible", "empty"} else "failed",
            children=[] if case == "empty" else ([child(status="completed")]
                                                 if case == "no_eligible" else None))
        if case in {"owner", "kind"}:
            async with sessions() as session:
                await session.execute(update(DownloadJob).values(
                    **({"owner_id": "other"} if case == "owner" else {"job_type": "single"})))
                await session.commit()
        async with sessions() as session:
            before = await snapshot(session)
        old = manager.cancel_event_for("parent")
        old.set()
        async with sessions() as reset:
            assert not await persistence.stage_collection_retry(
                reset, job_id="parent", owner_id="owner")
            await reset.rollback()
        if case == "owner":
            assert await service.retry_job(Actor(id="owner"), "parent") is None
        else:
            with pytest.raises(JobError) as caught:
                await service.retry_job(Actor(id="owner"), "parent")
            assert caught.value.code == JobErrorCode.INVALID_STATE
        async with sessions() as session:
            assert await snapshot(session) == before
        assert manager.cancel_event_for("parent") is old and old.is_set()
        assert not manager._wake_event.is_set() and bus.events == []
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_duplicate_retry_one_committed_writer(tmp_path: Path) -> None:
    engine, sessions = factory(tmp_path)
    service, _, bus = runtime(sessions)
    try:
        await seed(sessions)
        outcomes = await asyncio.gather(*[service.retry_job(Actor(id="owner"), "parent")
                                          for _ in range(2)], return_exceptions=True)
        assert sum(isinstance(result, DownloadJob) for result in outcomes) == 1
        assert sum(isinstance(result, JobError) for result in outcomes) == 1
        assert [e["event"] for e in bus.events] == ["activity.created", "job.updated"]
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_concurrent_internal_resets_first_sql_writer_boundary(tmp_path: Path) -> None:
    engine, sessions = factory(tmp_path)
    try:
        await seed(sessions)
        first_staged, release = asyncio.Event(), asyncio.Event()
        statements: list[str] = []

        def record_query(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record_query)

        async def writer(first: bool) -> bool:
            async with sessions() as session:
                won = await persistence.stage_collection_retry(
                    session, job_id="parent", owner_id="owner")
                if first:
                    first_staged.set()
                    await release.wait()
                if won:
                    await session.commit()
                else:
                    await session.rollback()
                return won

        one = asyncio.create_task(writer(True))
        await asyncio.wait_for(first_staged.wait(), 5)
        assert statements[0].startswith("UPDATE download_jobs")
        two = asyncio.create_task(writer(False))
        try:
            await checkpoint()
            assert not two.done()
        finally:
            release.set()
        assert await asyncio.gather(one, two) == [True, False]
        async with sessions() as observer:
            facts = await snapshot(observer)
        assert len(facts["activity_events"]) == 1
        assert facts["download_jobs"][0]["status"] == "queued"
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["parent", "child"])
async def test_actual_eligibility_revalidated_after_owned_attempt_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    request = None
    try:
        await seed(sessions)
        old = manager.cancel_event_for("parent")
        old.set()

        async def old_attempt() -> None:
            entered.set()
            await release.wait()
            async with sessions() as writer:
                if change == "parent":
                    await writer.execute(update(DownloadJob).values(status="completed"))
                else:
                    await writer.execute(update(JobItem).values(error_code="unknown"))
                await writer.commit()
            raise JobError(JobErrorCode.NETWORK_ERROR, retryable=True)

        manager._tasks["parent"] = asyncio.create_task(old_attempt())
        await entered.wait()
        real_wait = manager.wait_for_attempt
        waiting = asyncio.Event()

        async def wait(job_id: str, **kwargs: Any) -> None:
            waiting.set()
            await real_wait(job_id, **kwargs)

        monkeypatch.setattr(manager, "wait_for_attempt", wait)
        request = asyncio.create_task(service.retry_job(Actor(id="owner"), "parent"))
        await asyncio.wait_for(waiting.wait(), 5)
        release.set()
        with pytest.raises(JobError) as caught:
            await request
        assert caught.value.code == JobErrorCode.INVALID_STATE
        async with sessions() as observer:
            facts = await snapshot(observer)
        assert facts["download_jobs"][0]["collection_cancel_requested_at"] is not None
        assert facts["job_items"][0]["status"] == "failed"
        assert facts["activity_events"] == []
        assert manager.cancel_event_for("parent") is old and old.is_set()
        assert bus.events == [] and not manager._wake_event.is_set()
    finally:
        release.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["parent", "child", "marker"])
async def test_cached_parent_and_child_are_not_reset_authority(tmp_path: Path, change: str) -> None:
    engine, sessions = factory(tmp_path)
    try:
        await seed(sessions)
        async with sessions() as stale:
            cached = await stale.get(DownloadJob, "parent")
            cached_child = await stale.get(JobItem, "child-1")
            await stale.commit()
            async with sessions() as writer:
                if change == "parent":
                    await writer.execute(update(DownloadJob).values(status="completed"))
                elif change == "child":
                    await writer.execute(update(JobItem).values(error_code="unsupported_url"))
                else:
                    await writer.execute(update(DownloadJob).values(
                        collection_cancel_requested_at=None, total_items=99,
                        completed_items=90, failed_items=9))
                await writer.commit()
            async with sessions() as observer:
                before = await snapshot(observer)
            assert cached and cached.status == "failed"
            assert cached_child and cached_child.error_code == "network_error"
            won = await persistence.stage_collection_retry(stale, job_id="parent", owner_id="owner")
            assert won == (change == "marker")
            if won:
                await stale.commit()
            else:
                await stale.rollback()
            async with sessions() as observer:
                after = await snapshot(observer)
            if won:
                row = after["download_jobs"][0]
                assert (row["total_items"], row["completed_items"], row["failed_items"]) == (
                    1, 0, 0)
            else:
                assert after == before
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["initial_cas", "initial_write", "child_cas", "child_write",
                                    "final_cas", "final_write", "activity", "commit"])
async def test_all_failure_boundaries_roll_back_no_runtime_or_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    try:
        await seed(sessions)
        async with sessions() as observer:
            before = await snapshot(observer)
        old = manager.cancel_event_for("parent")
        old.set()
        gate = threading.Lock()
        process = object()
        manager._cancel_gates["parent"] = gate
        manager._processes["parent"] = process  # type: ignore[assignment]
        calls: list[str] = []
        monkeypatch.setattr(manager, "reset_attempt", lambda _: calls.append("reset"))
        monkeypatch.setattr(manager, "notify", lambda: calls.append("notify"))
        # Real SQLite triggers force lost CAS or technical write errors, including
        # a deferred violation that fails the actual COMMIT, not a mock commit.
        async with sessions() as setup:
            if failure == "commit":
                await setup.execute(text("CREATE TABLE retry_probe (id INTEGER PRIMARY KEY, "
                                         "parent INTEGER REFERENCES retry_probe(id) "
                                         "DEFERRABLE INITIALLY DEFERRED)"))
            elif failure != "activity":
                child_write = failure.startswith("child")
                table = "job_items" if child_write else "download_jobs"
                condition = ("NEW.status = 'queued'" if child_write or failure.startswith("final")
                             else "NEW.status = 'failed'")
                action = "IGNORE" if failure.endswith("cas") else "ABORT, 'injected write failure'"
                await setup.execute(text(f"CREATE TRIGGER retry_failure BEFORE UPDATE ON {table} "
                                         f"WHEN {condition} BEGIN SELECT RAISE({action}); END"))
            await setup.commit()
        if failure == "activity":
            async def fail_activity(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("activity failure")
            monkeypatch.setattr(persistence, "add_activity_event", fail_activity)
        if failure == "commit":
            real_stage = service_module.stage_collection_retry

            async def deferred(session: Any, **kwargs: Any) -> bool:
                won = await real_stage(session, **kwargs)
                await session.execute(text("INSERT INTO retry_probe VALUES (1, 2)"))
                return won
            monkeypatch.setattr(service_module, "stage_collection_retry", deferred)
        expected = JobError if failure.endswith("cas") else (
            RuntimeError if failure == "activity" else IntegrityError)
        with pytest.raises(expected):
            await service.retry_job(Actor(id="owner"), "parent")
        async with sessions() as observer:
            assert await snapshot(observer) == before
            if failure == "commit":
                assert (await observer.execute(text("SELECT * FROM retry_probe"))).all() == []
        assert calls == [] and bus.events == []
        assert manager.cancel_event_for("parent") is old and old.is_set()
        assert manager._cancel_gates["parent"] is gate
        assert manager._processes["parent"] is process
        assert not manager.lifecycle_lock("parent").locked()
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["cancelled", "mapped", "technical"])
@pytest.mark.parametrize("phase", ["drain", "commit", "close"])
async def test_owned_drain_and_definite_outcome_hold_lock_under_repeated_api_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, phase: str,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    old_release, old_started = asyncio.Event(), asyncio.Event()
    order: list[str] = []
    real_stage = service_module.stage_collection_retry
    real_reset = manager.reset_attempt
    real_publish = bus.publish

    async def old_attempt() -> None:
        old_started.set()
        await old_release.wait()
        order.append("drained")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        if outcome == "mapped":
            raise JobError(JobErrorCode.NETWORK_ERROR)
        raise RuntimeError("technical old outcome")

    async def stage(session: Any, **kwargs: Any) -> bool:
        assert order == ["drained"]
        won = await real_stage(session, **kwargs)
        real_commit, real_close = session.commit, session.close

        async def commit() -> None:
            await real_commit()
            order.append("committed")
            if phase == "commit":
                entered.set()
                await release.wait()

        async def close() -> None:
            if phase == "close":
                entered.set()
                await release.wait()
            await real_close()
            order.append("closed")

        session.commit, session.close = commit, close
        return won

    def reset(job_id: str) -> None:
        assert order == ["drained", "committed", "closed"]
        assert manager._tasks[job_id].done()
        order.append("reset")
        real_reset(job_id)

    def publish(value: dict[str, Any]) -> None:
        order.append(value["event"])
        real_publish(value)

    request = None
    try:
        await seed(sessions)
        old = manager.cancel_event_for("parent")
        old.set()
        manager._tasks["parent"] = asyncio.create_task(old_attempt())
        gate = threading.Lock()
        process = object()
        manager._cancel_gates["parent"] = gate
        manager._processes["parent"] = process  # type: ignore[assignment]
        await old_started.wait()
        monkeypatch.setattr(service_module, "stage_collection_retry", stage)
        monkeypatch.setattr(manager, "reset_attempt", reset)
        monkeypatch.setattr(bus, "publish", publish)
        monkeypatch.setattr(manager, "notify", lambda: order.append("notify"))
        real_wait = manager.wait_for_attempt

        async def wait(job_id: str, **kwargs: Any) -> None:
            if phase == "drain":
                entered.set()
            await real_wait(job_id, **kwargs)
        monkeypatch.setattr(manager, "wait_for_attempt", wait)
        request = asyncio.create_task(service.retry_job(Actor(id="owner"), "parent"))
        if phase != "drain":
            old_release.set()
        await asyncio.wait_for(entered.wait(), 5)
        lock = manager.lifecycle_lock("parent")
        for _ in range(3):
            request.cancel()
            await checkpoint()
            await checkpoint()
            assert not request.done() and lock.locked()
            assert manager.cancel_event_for("parent") is old
            assert manager._cancel_gates["parent"] is gate
            assert manager._processes["parent"] is process
            assert bus.events == [] and "reset" not in order
        if phase == "drain":
            # An independent writer succeeds while the old attempt is blocked:
            # retry does not hold SQLite's writer lock during drain.
            async with sessions() as writer:
                await writer.execute(update(DownloadJob).values(error_detail="writer during drain"))
                await writer.commit()
        old_release.set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 5)
        assert order == ["drained", "committed", "closed", "reset", "activity.created",
                         "job.updated", "notify"]
        assert not lock.locked() and manager.cancel_event_for("parent") is not old
        assert "parent" not in manager._cancel_gates and "parent" not in manager._processes
    finally:
        old_release.set()
        release.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("summary", ["partial_failure", "all_children_failed", "network_error"])
@pytest.mark.parametrize("eligible", [False, True])
async def test_scoped_read_list_sync_sse_mapping_uses_child_policy(
    tmp_path: Path, summary: str, eligible: bool,
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    try:
        await seed(sessions, children=[child(status="failed", error_code=(
            "timeout" if eligible else "unsupported_url")), child(2, status="completed")])
        async with sessions() as session:
            await session.execute(update(DownloadJob).values(error_code=summary))
            await session.commit()
        async with sessions() as session:
            record = await jobs.get_job(session, "parent", "owner")
            records = await jobs.list_jobs(session, "owner")
            children = await jobs.list_job_items(session, "parent", "owner")
            assert await jobs.get_job(session, "parent", "foreign") is None
            assert await jobs.list_jobs(session, "foreign") == []
        assert record is not None
        read = job_to_dto(record, children).model_dump(mode="json")
        assert read["error"]["retryable"] == eligible
        assert read["items"][0]["error"]["retryable"] == eligible
        list_error = job_to_dto(records[0]).error
        assert list_error is not None and list_error.retryable == eligible
        # Sync publication must not issue SQL or require a live session.
        queries: list[str] = []
        def record_query(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            queries.append(statement)
        event.listen(engine.sync_engine, "before_cursor_execute", record_query)
        manager.publish_snapshot("job.failed", record)
        assert queries == []
        sse = bus.events[-1]["data"]
        assert sse["error"] == read["error"] and set(sse) == set(read)
        assert sse["items"] is None  # Existing snapshot shape stays unchanged.
        children[0].status, children[0].error_code = "cancelled", "unknown"
        child_error = item_to_dto(children[0]).error
        assert child_error is not None and child_error.retryable
        record.error_code = None
        assert job_to_dto(record).error is None  # Contract gap; no fabricated error.
    finally:
        await engine.dispose()
