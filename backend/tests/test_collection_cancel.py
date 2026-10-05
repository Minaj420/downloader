import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.domain.actors import Actor
from app.domain.jobs import JobError, JobErrorCode
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.repositories import jobs
from app.services import collection_persistence as persistence
from app.services import job_service as service_module
from app.services.event_bus import EventBus
from app.services.job_runner import RunnerDeps
from app.services.job_service import JobService
from app.services.queue_manager import QueueManager
from tests.test_collection_child_runner import Harness
from tests.test_collection_persistence import complete, placed, snapshot
from tests.test_collection_repository import child, factory, parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def publish(self, value: dict[str, Any]) -> None:
        self.events.append(value)


def runtime(sessions: Any) -> tuple[JobService, QueueManager, RecordingBus]:
    bus = RecordingBus()
    manager = QueueManager(session_factory=sessions,
                           deps=cast(RunnerDeps, SimpleNamespace(bus=bus)),
                           concurrency=1, poll_interval_seconds=1)
    return JobService(sessions, manager, bus), manager, bus


async def checkpoint() -> None:
    # An explicit event-loop turn, not a wall-clock delay.
    ready = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(ready.set_result, None)
    await ready


@pytest.mark.anyio
@pytest.mark.parametrize("pending", ["queued", "analyzing", "downloading",
                                     "post_processing", "finalizing", "cancelled"])
async def test_settlement_preserves_completed_catalog_failed_reason_and_real_counts(
    tmp_path: Path, pending: str,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    storage, placement = await placed(tmp_path)
    try:
        async with sessions() as session:
            await jobs.stage_collection_job(session, parent(status="analyzing"), [
                child(1, status="failed", error_code="network_error",
                      error_message="real reason", error_detail="kept"),
                child(2, status="finalizing"), child(3, status=pending),
            ], owner_id="owner")
            await session.commit()
        async with sessions() as session:
            assert await complete(session, storage, placement)
            await session.execute(update(DownloadJob).values(
                total_items=99, completed_items=90, failed_items=9, progress_percent=99))
            await session.commit()
        async with sessions() as observer:
            before = await snapshot(observer)
        record = await service.cancel_job(Actor(id="owner"), "parent")
        assert (record.status, record.total_items, record.completed_items,
                record.failed_items, record.progress_percent) == ("cancelled", 3, 1, 1, 100 / 3)
        assert record.collection_cancel_requested_at is not None
        async with sessions() as observer:
            after = await snapshot(observer)
        for table in ("library_items", "media_files"):
            assert before[table] == after[table]
        assert before["job_items"][:2] == after["job_items"][:2]
        last = after["job_items"][2]
        assert last["status"] == "cancelled"
        if pending == "cancelled":
            assert last == before["job_items"][2]
        else:
            assert last["error_code"] == "cancelled" and last["completed_at"] is not None
        assert manager.cancel_event_for("parent").is_set()
        assert not manager._wake_event.is_set()
        assert [e["event"] for e in bus.events] == [
            "activity.created", "job.updated",
            *(["activity.created"] if pending != "cancelled" else []),
            "activity.created", "job.cancelled",
        ]
        assert len({e["id"] for e in bus.events if e["id"] is not None}) == (
            2 + (pending != "cancelled"))
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_all_completed_before_request_wins_and_retains_marker(tmp_path: Path) -> None:
    engine, sessions = factory(tmp_path)
    service, _, bus = runtime(sessions)
    storage, placement = await placed(tmp_path)
    try:
        async with sessions() as session:
            await jobs.stage_collection_job(session, parent(status="analyzing"),
                                            [child(2, status="finalizing")], owner_id="owner")
            await session.commit()
        async with sessions() as session:
            assert await complete(session, storage, placement)
            await session.commit()
        async with sessions() as observer:
            before = await snapshot(observer)
        result = await service.cancel_job(Actor(id="owner"), "parent")
        assert (result.status, result.progress_percent, result.error_code,
                result.error_message, result.error_detail) == ("completed", 100, None, None, None)
        assert result.collection_cancel_requested_at is not None
        async with sessions() as observer:
            after = await snapshot(observer)
        for table in ("job_items", "library_items", "media_files"):
            assert after[table] == before[table]
        assert bus.events[-1]["event"] == "job.completed"
        events_before = list(bus.events)
        with pytest.raises(JobError) as caught:
            await service.cancel_job(Actor(id="owner"), "parent")
        assert caught.value.code == JobErrorCode.INVALID_STATE
        assert bus.events == events_before
        async with sessions() as observer:
            assert await snapshot(observer) == after
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("statuses", [[], ["incomplete"], ["unknown"], ["failed", "unknown"]])
async def test_unsettled_selection_retains_acceptance_but_never_claims_success(
    tmp_path: Path, statuses: list[str],
) -> None:
    engine, sessions = factory(tmp_path)
    service, _, bus = runtime(sessions)
    try:
        async with sessions() as session:
            await jobs.stage_collection_job(session, parent(), [
                child(i + 1, status=s) for i, s in enumerate(statuses)
            ], owner_id="owner")
            await session.commit()
        with pytest.raises(JobError) as caught:
            await service.cancel_job(Actor(id="owner"), "parent")
        assert caught.value.code == JobErrorCode.INVALID_STATE
        async with sessions() as observer:
            before = await snapshot(observer)
        assert before["download_jobs"][0]["status"] == "queued"
        assert before["download_jobs"][0]["collection_cancel_requested_at"] is not None
        assert len(before["activity_events"]) == 1
        assert [e["event"] for e in bus.events] == ["activity.created", "job.updated"]
        # Duplicate acceptance is idempotent even when settlement remains deferred.
        with pytest.raises(JobError):
            await service.cancel_job(Actor(id="owner"), "parent")
        async with sessions() as observer:
            assert await snapshot(observer) == before
        assert len(bus.events) == 2
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("first", ["request", "failure"])
async def test_durable_failure_order_not_runtime_signal(tmp_path: Path, first: str) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    try:
        async with sessions() as session:
            await jobs.stage_collection_job(session, parent(status="analyzing"),
                                            [child(1, status="failed", error_detail="real")],
                                            owner_id="owner")
            await session.commit()
        async with sessions() as session:
            if first == "request":
                assert await persistence.stage_collection_cancel_request(
                    session, job_id="parent", owner_id="owner") == (
                        persistence.CollectionCancelRequestOutcome.STAGED)
            else:
                assert await persistence.stage_collection_failure_settlement(
                    session, job_id="parent", owner_id="owner")
            await session.commit()
        if first == "request":
            result = await service.cancel_job(Actor(id="owner"), "parent")
            assert result.status == "cancelled" and result.failed_items == 1
            assert len([e for e in bus.events if e["event"] == "activity.created"]) == 1
        else:
            with pytest.raises(JobError):
                await service.cancel_job(Actor(id="owner"), "parent")
            assert bus.events == [] and manager._cancel_events == {}
        async with sessions() as observer:
            item = await observer.get(JobItem, "child-1")
            assert item is not None and item.error_detail == "real" and item.status == "failed"
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["owner", "kind", "failed", "completed", "cancelled"])
async def test_request_writer_revalidates_stale_service_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    real_stage = service_module.stage_collection_cancel_request
    try:
        async with sessions() as session:
            await jobs.stage_collection_job(session, parent(), [child()], owner_id="owner")
            await session.commit()

        async def stage(session: Any, **kwargs: Any) -> Any:
            async with sessions() as writer:
                values = ({"owner_id": "other"} if change == "owner" else
                          {"job_type": "single"} if change == "kind" else {"status": change})
                await writer.execute(update(DownloadJob).values(**values))
                await writer.commit()
            return await real_stage(session, **kwargs)

        monkeypatch.setattr(service_module, "stage_collection_cancel_request", stage)
        with pytest.raises(JobError):
            await service.cancel_job(Actor(id="owner"), "parent")
        assert manager._cancel_events == {} and bus.events == []
        async with sessions() as observer:
            assert (await snapshot(observer))["activity_events"] == []
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["request", "settlement"])
@pytest.mark.parametrize("failure", ["write", "child_write", "activity", "lost_cas", "commit"])
async def test_transaction_failure_rolls_back_every_fact_and_only_acceptance_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, failure: str,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    original = (service_module.stage_collection_cancel_request if phase == "request"
                else service_module.stage_collection_cancel_settlement)
    try:
        async with sessions() as session:
            await jobs.stage_collection_job(session, parent(), [child()], owner_id="owner")
            # Real SQLite COMMIT failure, not merely a mocked commit await.
            await session.execute(text(
                "CREATE TABLE cancel_commit_probe (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES cancel_commit_probe(id) "
                "DEFERRABLE INITIALLY DEFERRED)"))
            await session.commit()
        if phase == "settlement":
            async with sessions() as session:
                await persistence.stage_collection_cancel_request(
                    session, job_id="parent", owner_id="owner")
                await session.commit()
        async with sessions() as observer:
            before = await snapshot(observer)

        async def stage(session: Any, **kwargs: Any) -> Any:
            real_execute = session.execute
            calls = 0

            async def execute(statement: Any, *args: Any, **kw: Any) -> Any:
                nonlocal calls
                if getattr(statement, "is_update", False):
                    calls += 1
                    if failure == "write" and calls == (1 if phase == "request" else 3):
                        raise RuntimeError("write failed")
                    if failure == "child_write" and calls == (1 if phase == "request" else 2):
                        raise RuntimeError("child write failed")
                    if failure == "lost_cas" and calls == (1 if phase == "request" else 3):
                        await real_execute(update(DownloadJob).values(status="completed"))
                return await real_execute(statement, *args, **kw)

            monkeypatch.setattr(session, "execute", execute)
            if failure == "activity":
                async def fail_activity(*args: Any, **kw: Any) -> Any:
                    raise RuntimeError("activity failed")
                monkeypatch.setattr(persistence, "add_activity_event", fail_activity)
            result = await original(session, **kwargs)
            if failure == "commit":
                await session.execute(text(
                    "INSERT INTO cancel_commit_probe (id, parent_id) VALUES (1, 2)"))
            return result

        monkeypatch.setattr(service_module, f"stage_collection_cancel_{phase}", stage)
        expected = (IntegrityError if failure == "commit" else
                    JobError if failure == "lost_cas" else RuntimeError)
        with pytest.raises(expected):
            await service.cancel_job(Actor(id="owner"), "parent")
        async with sessions() as observer:
            assert await snapshot(observer) == before
        assert bus.events == [] and not manager._wake_event.is_set()
        assert bool(manager._cancel_events) == (phase == "settlement")
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("block", ["probe", "high", "merge"])
async def test_owned_child_gate_and_helpers_drain_under_lock_after_repeated_api_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: str,
) -> None:
    h = Harness(tmp_path)
    service, manager, bus = runtime(h.sessions)
    manager._processes = h.processes
    manager._cancel_events["parent"] = h.adapters.cancel
    manager._cancel_gates["parent"] = h.gate
    h.adapters.block = block
    gate_entered, gate_release = threading.Event(), threading.Event()
    signal_entered = asyncio.Event()
    real_signal = manager.request_collection_cancel

    async def signal(job_id: str) -> None:
        current = await service.get_job(Actor(id="owner"), job_id)
        assert current is not None and current.collection_cancel_requested_at is not None
        signal_entered.set()
        await real_signal(job_id)

    monkeypatch.setattr(manager, "request_collection_cancel", signal)

    def holding_gate() -> None:
        with h.gate:
            gate_entered.set()
            assert gate_release.wait(5)

    gate_task = None
    request = None
    try:
        await h.seed()
        attempt = asyncio.create_task(h.run())
        manager._tasks["parent"] = cast(asyncio.Task[None], attempt)
        await asyncio.wait_for(h.adapters.started.wait(), 5)
        gate_task = asyncio.create_task(asyncio.to_thread(holding_gate))
        assert await asyncio.to_thread(gate_entered.wait, 5)
        request = asyncio.create_task(service.cancel_job(Actor(id="owner"), "parent"))
        await asyncio.wait_for(signal_entered.wait(), 5)
        # The gate is still held: the event loop and independent SQL are usable.
        async with h.sessions() as writer:
            assert not await persistence.stage_child_progress(
                writer, job_id="parent", owner_id="owner", child_id="child-2",
                progress_percent=99)
            await writer.rollback()
        for _ in range(3):
            request.cancel()
            await checkpoint()
            assert not request.done() and manager.lifecycle_lock("parent").locked()
            assert not h.adapters.cancel.is_set()
            assert manager._tasks["parent"] is attempt and not attempt.done()
            await manager._claim_and_run()
            assert manager._tasks["parent"] is attempt
        gate_release.set()
        await gate_task
        await asyncio.wait_for(h.adapters.cleanup.wait(), 5)
        assert h.adapters.cancel.is_set()
        assert not request.done() and manager.lifecycle_lock("parent").locked()
        assert not h.adapters.reaped and not attempt.done()
        # No terminal transaction/Activity until adapter-owned helpers are drained.
        assert not any(e["event"] == "job.cancelled" for e in bus.events)
        h.adapters.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 5)
        assert attempt.done() and h.adapters.reaped
        assert not manager.lifecycle_lock("parent").locked()
        assert (await h.facts())[:5] == ("cancelled", "cancelled", 0, 1, 0)
        assert bus.events[-1]["event"] == "job.cancelled"
        assert manager._cancel_gates["parent"] is h.gate
        assert manager.cancel_event_for("parent") is h.adapters.cancel
    finally:
        gate_release.set()
        h.adapters.release.set()
        h.adapters.cancel.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        if gate_task is not None:
            await gate_task
        await manager.wait_for_attempt("parent", retrieve_outcome=True)
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["request", "settlement"])
@pytest.mark.parametrize("barrier", ["commit", "close"])
async def test_shield_owns_transaction_return_and_close_through_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, barrier: str,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    name = f"stage_collection_cancel_{phase}"
    real_stage = getattr(service_module, name)
    request = None
    try:
        async with sessions() as session:
            await jobs.stage_collection_job(session, parent(), [child()], owner_id="owner")
            await session.commit()

        async def stage(session: Any, **kwargs: Any) -> Any:
            result = await real_stage(session, **kwargs)
            original = getattr(session, barrier)

            async def held() -> None:
                if barrier == "commit":
                    await original()
                entered.set()
                await release.wait()
                if barrier == "close":
                    await original()

            monkeypatch.setattr(session, barrier, held)
            return result

        monkeypatch.setattr(service_module, name, stage)
        request = asyncio.create_task(service.cancel_job(Actor(id="owner"), "parent"))
        await asyncio.wait_for(entered.wait(), 5)
        record = await service.get_job(Actor(id="owner"), "parent")
        assert record is not None and record.collection_cancel_requested_at is not None
        assert record.status == ("queued" if phase == "request" else "cancelled")
        prior = list(bus.events)
        for _ in range(3):
            request.cancel()
            await checkpoint()
            assert not request.done() and manager.lifecycle_lock("parent").locked()
            assert bus.events == prior
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 5)
        assert not manager.lifecycle_lock("parent").locked()
        assert bus.events[-1]["event"] == "job.cancelled"
        async with sessions() as observer:
            events = (await observer.scalars(select(ActivityEvent))).all()
            assert len(events) == 3
    finally:
        release.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        await engine.dispose()
