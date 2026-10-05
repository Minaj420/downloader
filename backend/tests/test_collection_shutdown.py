import asyncio
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.actors import Actor
from app.domain.jobs import JobError
from app.models.jobs import DownloadJob
from app.repositories import jobs
from app.services import job_service as service_module
from app.services import queue_manager as queue_module
from app.services.collection_parent_runner import run_collection_parent
from tests.test_collection_cancel import checkpoint, runtime
from tests.test_collection_child_runner import Harness
from tests.test_collection_parent_runner import seed as seed_parent
from tests.test_collection_persistence import snapshot
from tests.test_collection_recovery import _updates_column, seed
from tests.test_collection_repository import child, factory, parent
from tests.test_collection_retry import seed as seed_retry


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize("statuses", [
    ["completed"], ["failed"], ["completed", "failed"],
    ["completed", "downloading", "queued", "failed"],
])
async def test_stop_uses_actual_recovery_matrix_once(
    tmp_path: Path, marker: bool, statuses: list[str],
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    try:
        await seed(sessions, statuses, marker=marker)
        async with sessions() as session:
            await jobs.stage_collection_job(session, parent("unclaimed"), [
                child(20, job_id="unclaimed")], owner_id="owner")
            await jobs.add_job(session, parent("single", job_type="single", status="downloading"))
            await session.commit()
            before = await snapshot(session)
        manager._started = True  # Internal collection dispatch remains disabled.
        await asyncio.gather(manager.stop(), manager.stop())
        async with sessions() as session:
            after = await snapshot(session)
        rows = {r["id"]: r for r in after["download_jobs"]}
        old = {r["id"]: r for r in before["download_jobs"]}
        assert rows["single"] == old["single"]  # No generic single recovery on stop.
        assert rows["unclaimed"] == old["unclaimed"]
        c, f, n = statuses.count("completed"), statuses.count("failed"), len(statuses)
        settled = c + f == n
        expected = "completed" if c == n else "cancelled" if marker else "failed"
        code = (None if c == n else "cancelled" if marker else
                "partial_failure" if settled and c else "all_children_failed" if settled
                else "interrupted_by_restart")
        assert (rows["parent"]["status"], rows["parent"]["error_code"],
                rows["parent"]["total_items"], rows["parent"]["completed_items"],
                rows["parent"]["failed_items"]) == (
                    expected, code, n, c, f + (0 if marker else statuses.count("downloading")))
        assert bool(rows["parent"]["collection_cancel_requested_at"]) == marker
        assert [e["event"] for e in bus.events] == [f"job.{expected}"]
        assert len([e for e in after["activity_events"] if e["job_id"] == "parent"]) == (
            3 if marker and not settled else 1)
        await manager.stop()
        async with sessions() as session:
            assert await snapshot(session) == after
        assert len(bus.events) == 1 and not manager.started
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("step", ["probe", "high", "merge"])
async def test_parent_full_drain_slot_and_db_lifetime_under_repeated_stop_cancellation(
    tmp_path: Path, step: str,
) -> None:
    h = Harness(tmp_path)
    _, manager, bus = runtime(h.sessions)
    manager._processes = h.processes
    manager._cancel_events["parent"] = h.adapters.cancel
    manager._cancel_gates["parent"] = h.gate
    manager._started = True
    h.adapters.block = step
    stops: list[asyncio.Task[None]] = []
    disposed = asyncio.Event()
    try:
        await seed_parent(h, [child(2), child(9)])
        async with h.sessions() as session:
            await jobs.add_job(session, parent("next-single", job_type="single"))
            await session.commit()

        async def attempt() -> None:
            await run_collection_parent(
                "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
                processes=h.processes, cancel_gate=h.gate, bus=bus)

        owned = asyncio.create_task(attempt())
        manager._tasks["parent"] = owned
        await asyncio.wait_for(h.adapters.started.wait(), 5)

        async def lifespan() -> None:
            await manager.stop()
            assert owned.done() and h.adapters.reaped and not h.processes
            assert bus.events[-1]["event"] == "job.failed"
            await h.engine.dispose()
            disposed.set()

        stops = [asyncio.create_task(manager.stop()), asyncio.create_task(lifespan())]
        await asyncio.wait_for(h.adapters.cleanup.wait(), 5)
        for _ in range(3):
            stops[0].cancel()
            await checkpoint()
            assert owned.cancelling() == 1
            assert not owned.done() and not stops[0].done() and not disposed.is_set()
            assert manager._tasks == {"parent": owned}
            assert manager._cancel_gates["parent"] is h.gate
            assert not h.adapters.cancel.is_set()  # Shutdown is not User Cancel.
            await manager._claim_and_run()
            assert manager._tasks == {"parent": owned}
        # Independent writer works while adapters drain; settlement owns no writer yet.
        async with h.sessions() as session:
            await session.execute(update(DownloadJob).where(
                DownloadJob.id == "parent").values(error_detail="during drain"))
            await session.commit()
        h.adapters.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stops[0], 5)
        await asyncio.wait_for(stops[1], 5)
        assert disposed.is_set() and not manager._tasks and not manager._cancel_gates
        async with h.sessions() as session:
            facts = await snapshot(session)
        rows = {r["id"]: r for r in facts["download_jobs"]}
        assert rows["parent"]["error_code"] == "interrupted_by_restart"
        assert rows["next-single"]["status"] == "queued"
        children = {r["id"]: r for r in facts["job_items"]}
        assert children["child-2"]["error_code"] == "interrupted_by_restart"
        assert children["child-9"]["status"] == "queued"
    finally:
        h.adapters.release.set()
        await asyncio.gather(*stops, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
async def test_storage_publication_gate_and_signal_thread_finish_before_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    _, manager, bus = runtime(h.sessions)
    manager._started = True
    manager._cancel_gates["parent"] = h.gate
    manager._cancel_events["parent"] = h.adapters.cancel
    entered, signal_waiting = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    real_link = os.link
    real_place = h.storage._place_child_final_sync
    storage_thread: list[int] = []
    stop = None

    class Gate:
        def __enter__(self) -> None:
            if threading.get_ident() != storage_thread[0]:
                loop.call_soon_threadsafe(signal_waiting.set)
            h.gate.acquire()

        def __exit__(self, *args: Any) -> None:
            h.gate.release()

    def link(src: Path, dst: Path) -> None:
        real_link(src, dst)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    def place(*args: Any, **kwargs: Any) -> Any:
        storage_thread.append(threading.get_ident())
        try:
            return real_place(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(h.storage, "_place_child_final_sync", place)
    try:
        await seed_parent(h, [child(2)])

        async def attempt() -> None:
            await run_collection_parent(
                "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
                processes=h.processes, cancel_gate=Gate(), bus=bus)  # type: ignore[arg-type]

        owned = asyncio.create_task(attempt())
        manager._tasks["parent"] = owned
        await asyncio.wait_for(entered.wait(), 5)
        stop = asyncio.create_task(manager.stop())
        await asyncio.wait_for(signal_waiting.wait(), 5)
        for _ in range(3):
            stop.cancel()
            await checkpoint()
            assert not stop.done() and not owned.done() and not finished.is_set()
            assert not bus.events and manager._cancel_gates["parent"] is h.gate
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop, 5)
        assert finished.is_set() and not h.processes
        async with h.sessions() as session:
            after = await snapshot(session)
        assert not after["library_items"] and not after["media_files"]
        assert after["job_items"][0]["error_code"] == "interrupted_by_restart"
        assert bus.events[-1]["event"] == "job.failed"
    finally:
        release.set()
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["cancel_request", "cancel_settlement", "retry"])
@pytest.mark.parametrize("barrier", ["commit", "close"])
@pytest.mark.parametrize("started", [False, True])
async def test_inflight_disconnected_api_and_lifecycle_waiter_finish_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, barrier: str, started: bool,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    name = f"stage_collection_{phase}"
    real_stage = getattr(service_module, name)
    request = waiter = stop = None
    try:
        if phase == "retry":
            await seed_retry(sessions)
        else:
            await seed(sessions, ["downloading"])
        manager._started = started

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
        action = service.retry_job if phase == "retry" else service.cancel_job
        request = asyncio.create_task(action(Actor(id="owner"), "parent"))
        await asyncio.wait_for(entered.wait(), 5)
        waiter = asyncio.create_task(action(Actor(id="owner"), "parent"))
        await checkpoint()
        assert len(manager._operations) == 3  # Both API scopes plus shield-owned work.
        stop = asyncio.create_task(manager.stop())
        await checkpoint()  # Start stop before cancelling its awaiting caller.
        assert manager._stop_task is not None
        for _ in range(3):
            request.cancel()
            stop.cancel()
            await checkpoint()
            assert len(manager._operations) == 3
            assert manager.lifecycle_lock("parent").locked()
            assert not request.done() and not waiter.done() and not stop.done()
            assert manager.started == started
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 5)
        with pytest.raises(JobError):
            await asyncio.wait_for(waiter, 5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop, 5)
        assert not manager._operations and not manager.lifecycle_lock("parent").locked()
        async with sessions() as session:
            after = await snapshot(session)
        row = after["download_jobs"][0]
        assert row["status"] == ("queued" if phase == "retry" else "cancelled")
        assert (row["collection_cancel_requested_at"] is None) == (phase == "retry")
        assert [e["event"] for e in bus.events].count("job.cancelled") == (phase != "retry")
        assert bus.events[-1]["event"] == ("job.updated" if phase == "retry" else "job.cancelled")
    finally:
        release.set()
        await asyncio.gather(*(t for t in (request, waiter, stop) if t is not None),
                             return_exceptions=True)
        await engine.dispose()


@pytest.mark.anyio
async def test_retry_waiting_old_callback_does_not_deadlock_stop_or_interrupt_reset_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, bus = runtime(sessions)
    started, draining, release, waiting = [asyncio.Event() for _ in range(4)]
    request = stop = None
    real_wait = manager.wait_for_attempt
    try:
        await seed_retry(sessions)
        manager._started = True

        async def attempt() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                draining.set()
                await release.wait()
                # A last old callback/session close is owned by this attempt.
                async with sessions() as session:
                    await session.execute(update(DownloadJob).where(
                        DownloadJob.id == "parent").values(error_detail="last old callback"))
                    await session.commit()

        async def wait(job_id: str, **kwargs: Any) -> None:
            waiting.set()
            await real_wait(job_id, **kwargs)

        monkeypatch.setattr(manager, "wait_for_attempt", wait)
        owned = asyncio.create_task(attempt())
        manager._tasks["parent"] = owned
        await started.wait()
        request = asyncio.create_task(service.retry_job(Actor(id="owner"), "parent"))
        await asyncio.wait_for(waiting.wait(), 5)
        stop = asyncio.create_task(manager.stop())
        await asyncio.wait_for(draining.wait(), 5)
        request.cancel()
        await checkpoint()
        assert not stop.done() and manager.lifecycle_lock("parent").locked()
        assert manager._tasks["parent"] is owned and owned.cancelling() == 1
        assert not bus.events
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 5)
        await asyncio.wait_for(stop, 5)
        async with sessions() as session:
            row = await jobs.get_job(session, "parent", "owner")
        assert row and row.status == "queued" and row.collection_cancel_requested_at is None
        assert row.error_detail is None
        assert [e["event"] for e in bus.events] == ["activity.created", "job.updated"]
    finally:
        release.set()
        await asyncio.gather(*(t for t in (request, stop) if t is not None), return_exceptions=True)
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["writer", "child", "parent", "activity", "commit"])
async def test_stop_failure_after_drain_rolls_back_no_events_and_startup_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    drained = asyncio.Event()
    real_stage = queue_module.stage_collection_startup_recovery
    try:
        await seed(sessions, ["downloading", "completed"])
        async with sessions() as session:
            await session.execute(text(
                "CREATE TABLE shutdown_probe (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES shutdown_probe(id) DEFERRABLE INITIALLY DEFERRED)"))
            await session.commit()
            before = await snapshot(session)
        manager._started = True
        gate = threading.Lock()
        manager._cancel_gates["parent"] = gate

        async def attempt() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        owned = asyncio.create_task(attempt())
        manager._tasks["parent"] = owned
        await checkpoint()

        def fail_sql(conn: Any, cursor: Any, sql: str, parameters: Any,
                     context: Any, many: bool) -> None:
            hit = ((failure == "writer" and sql.startswith("UPDATE download_jobs"))
                   or (failure == "child" and _updates_column(sql, "job_items", "status"))
                   or (failure == "parent" and _updates_column(sql, "download_jobs", "status"))
                   or (failure == "activity" and sql.startswith("INSERT INTO activity_events")))
            if hit:
                assert drained.is_set() and owned.done()
                raise RuntimeError("injected shutdown failure")

        async def deferred(session: AsyncSession, **kwargs: Any) -> Any:
            assert drained.is_set() and owned.done()
            result = await real_stage(session, **kwargs)
            await session.execute(text("INSERT INTO shutdown_probe VALUES (1, 2)"))
            return result

        with monkeypatch.context() as patch:
            if failure == "commit":
                patch.setattr(queue_module, "stage_collection_startup_recovery", deferred)
            else:
                event.listen(engine.sync_engine, "before_cursor_execute", fail_sql)
            try:
                with pytest.raises(IntegrityError if failure == "commit" else RuntimeError):
                    await manager.stop()
            finally:
                if failure != "commit":
                    event.remove(engine.sync_engine, "before_cursor_execute", fail_sql)
        assert drained.is_set() and manager.started and manager._tasks["parent"] is owned
        assert manager._cancel_gates["parent"] is gate and not bus.events
        async with sessions() as session:
            assert await snapshot(session) == before
            assert (await session.execute(text("SELECT * FROM shutdown_probe"))).all() == []
        # A fresh process still has the existing startup fallback after failed stop.
        _, restarted, _ = runtime(sessions)
        await restarted._recover_interrupted_jobs()
        await manager.stop()  # Retry is idempotent; does not cancel the drained attempt again.
        assert not manager.started and owned.cancelling() == 1 and not bus.events
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("barrier", ["commit", "close"])
async def test_stop_owns_settlement_close_and_publication_through_repeated_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, barrier: str,
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    real_stage = queue_module.stage_collection_startup_recovery
    stops: list[asyncio.Task[None]] = []
    try:
        await seed(sessions, ["completed"])
        manager._started = True

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

        monkeypatch.setattr(queue_module, "stage_collection_startup_recovery", stage)
        stops = [asyncio.create_task(manager.stop()), asyncio.create_task(manager.stop())]
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(3):
            stops[0].cancel()
            await checkpoint()
            assert not any(t.done() for t in stops) and manager.started and not bus.events
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stops[0], 5)
        await asyncio.wait_for(stops[1], 5)
        assert [e["event"] for e in bus.events] == ["job.completed"]
        assert not manager.started
    finally:
        release.set()
        await asyncio.gather(*stops, return_exceptions=True)
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["cancel", "retry"])
async def test_cancelled_initial_read_close_is_owned_even_after_previous_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    engine, sessions = factory(tmp_path)
    service, manager, _ = runtime(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    request = stop = None
    real_close = AsyncSession.close
    try:
        await seed_retry(sessions)
        manager._started = True
        await manager.stop()

        async def close(session: AsyncSession) -> None:
            entered.set()
            await release.wait()
            await real_close(session)

        monkeypatch.setattr(AsyncSession, "close", close)
        operation = service.cancel_job if action == "cancel" else service.retry_job
        request = asyncio.create_task(operation(Actor(id="owner"), "parent"))
        await asyncio.wait_for(entered.wait(), 5)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 5)
        assert len(manager._operations) == 1  # Caller left; owned read/close did not.
        old_stop = manager._stop_task
        stop = asyncio.create_task(manager.stop())
        await checkpoint()
        assert manager._stop_task is not old_stop and not stop.done()
        release.set()
        await asyncio.wait_for(stop, 5)
        assert not manager._operations
    finally:
        release.set()
        await asyncio.gather(*(t for t in (request, stop) if t is not None), return_exceptions=True)
        await engine.dispose()


@pytest.mark.anyio
async def test_stop_during_claim_query_never_schedules_next_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, _ = runtime(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    real_query = jobs.claimable_queued_jobs
    stop = None
    try:
        async with sessions() as session:
            await jobs.add_job(session, parent("single", job_type="single"))
            await session.commit()

        async def claim(
            session: AsyncSession, *, limit: int, include_collections: bool = True,
        ) -> Any:
            assert include_collections is False
            rows = await real_query(
                session, limit=limit, include_collections=include_collections,
            )
            entered.set()
            await release.wait()
            return rows

        async def unexpected(*args: Any, **kwargs: Any) -> None:
            pytest.fail("New attempt scheduled after stop flag")

        monkeypatch.setattr(jobs, "claimable_queued_jobs", claim)
        monkeypatch.setattr(queue_module, "run_download_job", unexpected)
        await manager.start()
        await asyncio.wait_for(entered.wait(), 5)
        stop = asyncio.create_task(manager.stop())
        await checkpoint()
        assert manager._stop_event.is_set() and not stop.done()
        release.set()
        await asyncio.wait_for(stop, 5)
        assert not manager._tasks and manager._loop_task is None
        async with sessions() as session:
            row = await jobs.get_job(session, "single", "owner")
        assert row and row.status == "queued"
    finally:
        release.set()
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["child", "parent"])
async def test_lost_settlement_cas_rolls_back_all_staged_rows_and_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    real_execute = AsyncSession.execute
    missed: list[bool] = []
    try:
        await seed(sessions, ["downloading", "completed"])
        async with sessions() as session:
            before = await snapshot(session)
        manager._started = True

        async def execute(session: AsyncSession, statement: Any,
                          *args: Any, **kwargs: Any) -> Any:
            result = await real_execute(session, statement, *args, **kwargs)
            sql = str(statement)
            if ((phase == "child" and _updates_column(sql, "job_items", "status"))
                or (phase == "parent" and _updates_column(sql, "download_jobs", "status")
                    and "download_jobs.id =" in sql)):
                missed.append(True)
                # Real staged writes happened; helper sees a losing CAS and the
                # caller must roll back even the writer touch and prior children.
                return SimpleNamespace(rowcount=0)
            return result

        monkeypatch.setattr(AsyncSession, "execute", execute)
        await manager.stop()
        assert missed == [True] and not bus.events
        async with sessions() as session:
            assert await snapshot(session) == before
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_api_gate_waiter_and_late_lock_waiter_are_drained_without_lifecycle_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    service, manager, bus = runtime(h.sessions)
    manager._cancel_gates["parent"] = h.gate
    manager._cancel_events["parent"] = h.adapters.cancel
    manager._processes = h.processes
    h.adapters.block = "probe"
    signal_entered = asyncio.Event()
    gate_entered, gate_release = threading.Event(), threading.Event()
    real_signal = manager.request_collection_cancel
    request = waiter = stop = gate_task = None

    async def signal(job_id: str) -> None:
        signal_entered.set()
        await real_signal(job_id)

    def hold_gate() -> None:
        with h.gate:
            gate_entered.set()
            assert gate_release.wait(5)

    monkeypatch.setattr(manager, "request_collection_cancel", signal)
    try:
        await seed_parent(h, [child(2), child(9, status="failed", error_code="timeout")])

        async def attempt() -> None:
            await run_collection_parent(
                "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
                processes=h.processes, cancel_gate=h.gate, bus=bus)

        owned = asyncio.create_task(attempt())
        manager._tasks["parent"] = owned
        await asyncio.wait_for(h.adapters.started.wait(), 5)
        gate_task = asyncio.create_task(asyncio.to_thread(hold_gate))
        assert await asyncio.to_thread(gate_entered.wait, 5)
        request = asyncio.create_task(service.cancel_job(Actor(id="owner"), "parent"))
        await asyncio.wait_for(signal_entered.wait(), 5)
        stop = asyncio.create_task(manager.stop())
        await checkpoint()
        # Accepted/scheduled during runtime drain, before quiescence; no new
        # request admission after lifespan's quiescent boundary is assumed.
        waiter = asyncio.create_task(service.cancel_job(Actor(id="owner"), "parent"))
        await checkpoint()
        for _ in range(3):
            request.cancel()
            stop.cancel()
            await checkpoint()
            assert not request.done() and not stop.done() and not waiter.done()
            assert manager.lifecycle_lock("parent").locked()
            assert not h.adapters.cancel.is_set() and manager._tasks["parent"] is owned
        gate_release.set()
        await gate_task
        await asyncio.wait_for(h.adapters.cleanup.wait(), 5)
        assert not stop.done() and not any(e["event"] == "job.cancelled" for e in bus.events)
        h.adapters.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 5)
        with pytest.raises(JobError):
            await asyncio.wait_for(waiter, 5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop, 5)
        assert owned.cancelling() == 1 and h.adapters.reaped
        assert [e["event"] for e in bus.events].count("job.cancelled") == 1
        async with h.sessions() as session:
            row = await jobs.get_job(session, "parent", "owner")
        assert row and (row.status, row.failed_items) == ("cancelled", 1)
        assert row.collection_cancel_requested_at is not None
        assert not manager._operations and not manager._tasks and not manager._cancel_gates
    finally:
        gate_release.set()
        h.adapters.release.set()
        await asyncio.gather(*(t for t in (request, waiter, stop, gate_task) if t is not None),
                             return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("status,marker,statuses", [
    ("queued", False, ["queued"]), ("failed", False, ["downloading"]),
    ("completed", True, ["completed"]), ("analyzing", False, ["cancelled"]),
    ("analyzing", False, []), ("analyzing", True, ["unknown"]),
])
async def test_stop_never_repairs_unsupported_or_changes_committed_terminals(
    tmp_path: Path, status: str, marker: bool, statuses: list[str],
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    try:
        await seed(sessions, statuses, status, marker)
        async with sessions() as session:
            before = await snapshot(session)
        manager._started = True
        await manager.stop()
        async with sessions() as session:
            assert await snapshot(session) == before
        assert not bus.events
    finally:
        await engine.dispose()
