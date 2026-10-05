import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jobs import DownloadJob
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs
from app.services import collection_persistence as persistence
from app.services import queue_manager as queue_module
from tests.test_collection_cancel import checkpoint, runtime
from tests.test_collection_persistence import complete, placed, snapshot
from tests.test_collection_repository import child, factory, parent

ACTIVE = ["analyzing", "downloading", "post_processing", "finalizing"]


def _updates_column(sql: str, table: str, column: str) -> bool:
    update = sql.partition(" WHERE ")[0]
    assignments = update.partition(" SET ")[2].split(",")
    return (
        update.startswith(f"UPDATE {table} SET ")
        and any(part.strip().startswith(f"{column}=") for part in assignments)
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def seed(sessions: Any, statuses: list[str], status: str = "analyzing",
               marker: bool = False) -> None:
    async with sessions() as session:
        await jobs.stage_collection_job(session, parent(
            status=status, progress_percent=99,
            collection_cancel_requested_at=datetime.now(UTC) if marker else None,
        ), [child(i + 1, status=s, error_code="network_error" if i % 2 else "timeout",
                  error_message="persisted reason", error_detail="persisted detail",
                  speed_bytes_per_second=9, eta_seconds=7, final_path="placed-uncompleted")
            for i, s in enumerate(statuses)], owner_id="owner")
        await session.execute(update(DownloadJob).values(
            total_items=99, completed_items=89, failed_items=10))
        await session.commit()


@pytest.mark.anyio
@pytest.mark.parametrize("parent_status", ACTIVE)
@pytest.mark.parametrize("statuses", [
    *[[s] for s in ACTIVE], ["queued", "completed", "failed", "downloading"],
    ["completed"], ["failed"], ["failed", "failed"], ["completed", "failed"],
    ["queued", "completed"],
])
async def test_actual_facts_precede_normalization_and_second_recovery_is_immutable(
    tmp_path: Path, parent_status: str, statuses: list[str],
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    try:
        await seed(sessions, statuses, parent_status)
        async with sessions() as observer:
            before = await snapshot(observer)
        await manager._recover_interrupted_jobs()
        async with sessions() as observer:
            after = await snapshot(observer)
        n, c = len(statuses), statuses.count("completed")
        f = statuses.count("failed") + sum(s in ACTIVE for s in statuses)
        settled = all(s in {"completed", "failed"} for s in statuses)
        code = (None if c == n else "partial_failure" if c else "all_children_failed"
                ) if settled else "interrupted_by_restart"
        row = after["download_jobs"][0]
        assert (row["status"], row["error_code"], row["total_items"], row["completed_items"],
                row["failed_items"], row["progress_percent"]) == (
                    "completed" if c == n else "failed", code, n, c, f, 100 * c / n)
        assert row["collection_cancel_requested_at"] is None
        for old, new in zip(before["job_items"], after["job_items"], strict=True):
            if old["status"] in ACTIVE:
                assert new["status"] == "failed"
                assert new["error_code"] == "interrupted_by_restart"
                assert new["completed_at"] and new["error_detail"] is None
                assert new["speed_bytes_per_second"] is new["eta_seconds"] is None
                assert new["final_path"] == old["final_path"]
            else:
                assert new == old
        assert len(after["activity_events"]) == 1
        assert [e["event"] for e in bus.events] == [f"job.{row['status']}"]
        if code:
            assert bus.events[0]["data"]["error"]["retryable"] == (
                any(s in ACTIVE or s in {"queued", "failed"} for s in statuses))
        await manager._recover_interrupted_jobs()
        async with sessions() as observer:
            assert await snapshot(observer) == after
        assert len(bus.events) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["queued", *ACTIVE])
@pytest.mark.parametrize("statuses", [
    ["completed"], ["failed", "failed"], ["completed", "failed"],
    ["queued", "downloading", "completed", "failed", "cancelled"],
])
async def test_durable_cancel_matrix_and_marker_retained(
    tmp_path: Path, status: str, statuses: list[str],
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    try:
        await seed(sessions, statuses, status, True)
        async with sessions() as session:
            before = await snapshot(session)
        await manager._recover_interrupted_jobs()
        async with sessions() as session:
            after = await snapshot(session)
        row = after["download_jobs"][0]
        n, c, f = len(statuses), statuses.count("completed"), statuses.count("failed")
        assert (row["status"], row["total_items"], row["completed_items"], row["failed_items"],
                row["progress_percent"]) == (
                    "completed" if c == n else "cancelled", n, c, f, 100 * c / n)
        assert row["collection_cancel_requested_at"] == (
            before["download_jobs"][0]["collection_cancel_requested_at"])
        for old, new in zip(before["job_items"], after["job_items"], strict=True):
            if old["status"] in ["queued", *ACTIVE]:
                assert (new["status"], new["error_code"]) == ("cancelled", "cancelled")
            else:
                assert new == old
        assert bus.events[0]["event"] == f"job.{row['status']}"
        await manager._recover_interrupted_jobs()
        async with sessions() as session:
            assert await snapshot(session) == after
        assert len(bus.events) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("status,marker,statuses", [
    *[(s, m, ["downloading", "completed"]) for s in ("completed", "failed", "cancelled")
      for m in (False, True)],
    ("queued", False, ["queued", "completed"]),
    *[("analyzing", False, ss) for ss in ([], ["cancelled"], ["incomplete"], ["unknown"],
                                        ["completed", "downloading", "cancelled"])],
    ("queued", True, []), ("analyzing", True, ["failed", "unknown"]),
])
async def test_unsupported_terminal_and_normal_queued_no_changes(
    tmp_path: Path, status: str, marker: bool, statuses: list[str],
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    try:
        await seed(sessions, statuses, status, marker)
        async with sessions() as session:
            before = await snapshot(session)
        async with sessions() as session:
            outcome = await persistence.stage_collection_startup_recovery(
                session, job_id="parent", owner_id="owner")
            assert outcome != persistence.CollectionRecoveryOutcome.STAGED
            await session.rollback()
        await manager._recover_interrupted_jobs()
        async with sessions() as session:
            assert await snapshot(session) == before
        assert not bus.events
        assert not manager._tasks
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("deleted", [False, True])
async def test_completed_catalog_identity_and_files_never_recreated_or_touched(
    tmp_path: Path, deleted: bool,
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, _ = runtime(sessions)
    storage, placement = await placed(tmp_path)
    try:
        await seed(sessions, ["downloading", "finalizing"])
        async with sessions() as session:
            assert await complete(session, storage, placement)
            if deleted:
                await session.execute(delete(MediaFile))
                await session.execute(delete(LibraryItem))
            await session.commit()
        async with sessions() as session:
            before = await snapshot(session)
        contents = placement.final_path.read_bytes()
        await manager._recover_interrupted_jobs()
        async with sessions() as session:
            after = await snapshot(session)
        for table in ("library_items", "media_files"):
            assert after[table] == before[table]
        assert after["job_items"][1] == before["job_items"][1]
        assert placement.final_path.read_bytes() == contents
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["child", "parent", "activity", "commit"])
@pytest.mark.parametrize("marker", [False, True])
async def test_failed_start_rolls_back_every_write_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, marker: bool,
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    try:
        await seed(sessions, ["downloading", "completed"], marker=marker)
        async with sessions() as session:
            before = await snapshot(session)

        def fail_sql(conn: Any, cursor: Any, sql: str, parameters: Any,
                     context: Any, many: bool) -> None:
            if ((failure == "child" and _updates_column(sql, "job_items", "status"))
                or (failure == "parent" and _updates_column(sql, "download_jobs", "status"))
                or (failure == "activity" and sql.startswith("INSERT INTO activity_events"))):
                raise RuntimeError("injected recovery failure")

        async def fail_commit(self: AsyncSession) -> None:
            raise RuntimeError("injected recovery failure")

        with monkeypatch.context() as patch:
            if failure == "commit":
                patch.setattr(AsyncSession, "commit", fail_commit)
            else:
                event.listen(engine.sync_engine, "before_cursor_execute", fail_sql)
            try:
                with pytest.raises(RuntimeError, match="injected recovery failure"):
                    await manager.start()
            finally:
                if failure != "commit":
                    event.remove(engine.sync_engine, "before_cursor_execute", fail_sql)
        assert not manager.started and manager._loop_task is None and not manager._tasks
        assert not bus.events
        async with sessions() as session:
            assert await snapshot(session) == before
        await manager.start()
        await manager.stop()
        assert bus.events[0]["event"] == ("job.cancelled" if marker else "job.failed")
    finally:
        await manager.stop()
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("mismatch", ["owner", "kind", "terminal"])
async def test_stale_candidate_revalidated_under_writer_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str,
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    real = queue_module.stage_collection_startup_recovery
    before: dict[str, list[dict[str, Any]]] = {}

    async def stale(session: AsyncSession, **args: Any):
        async with sessions() as writer:
            values = ({"owner_id": "foreign"} if mismatch == "owner" else
                      {"job_type": "single", "status": "queued"} if mismatch == "kind"
                      else {"status": "completed"})
            await writer.execute(update(DownloadJob).values(**values))
            await writer.commit()
        async with sessions() as observer:
            before.update(await snapshot(observer))
        return await real(session, **args)

    try:
        await seed(sessions, ["downloading"])
        monkeypatch.setattr(queue_module, "stage_collection_startup_recovery", stale)
        await manager._recover_interrupted_jobs()
        async with sessions() as observer:
            assert await snapshot(observer) == before
        assert not bus.events
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("statuses,marker,phase", [
    (["downloading"], False, "child"), (["downloading"], True, "child"),
    (["downloading"], False, "parent"), (["downloading"], True, "parent"),
    (["completed"], False, "parent"), (["failed"], False, "parent"),
])
async def test_lost_child_or_final_parent_cas_rolls_back_even_the_outer_touch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    statuses: list[str], marker: bool, phase: str,
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    real = AsyncSession.execute
    lost: list[bool] = []

    async def execute(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await real(self, statement, *args, **kwargs)
        sql = str(statement)
        if (
            (phase == "child" and _updates_column(sql, "job_items", "status"))
            or (
                phase == "parent"
                and _updates_column(sql, "download_jobs", "status")
                and "download_jobs.id =" in sql
            )
        ):
            lost.append(True)
            return SimpleNamespace(rowcount=0)
        return result

    try:
        await seed(sessions, statuses, marker=marker)
        async with sessions() as observer:
            before = await snapshot(observer)
        monkeypatch.setattr(AsyncSession, "execute", execute)
        await manager._recover_interrupted_jobs()
        assert lost == [True]
        async with sessions() as observer:
            assert await snapshot(observer) == before
        assert not bus.events
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_waiting_writer_revalidates_marker_after_prior_transaction_outcome(
    tmp_path: Path, cancel_first: bool,
) -> None:
    engine, sessions = factory(tmp_path)
    entered = asyncio.Event()
    try:
        await seed(sessions, ["downloading"])
        async with sessions() as first:
            if cancel_first:
                assert await persistence.stage_collection_cancel_request(
                    first, job_id="parent", owner_id="owner"
                ) == persistence.CollectionCancelRequestOutcome.STAGED
            else:
                assert await persistence.stage_collection_startup_recovery(
                    first, job_id="parent", owner_id="owner"
                ) == persistence.CollectionRecoveryOutcome.STAGED

            async def waiting() -> None:
                async with sessions() as second:
                    entered.set()
                    if cancel_first:
                        assert await persistence.stage_collection_startup_recovery(
                            second, job_id="parent", owner_id="owner"
                        ) == persistence.CollectionRecoveryOutcome.STAGED
                    else:
                        assert await persistence.stage_collection_cancel_request(
                            second, job_id="parent", owner_id="owner"
                        ) == persistence.CollectionCancelRequestOutcome.STAGED
                    await second.commit()

            waiter = asyncio.create_task(waiting())
            await entered.wait()
            await checkpoint()
            assert not waiter.done()
            if cancel_first:
                await first.commit()
            else:
                await first.rollback()
            await asyncio.wait_for(waiter, 5)
        if not cancel_first:
            _, manager, _ = runtime(sessions)
            await manager._recover_interrupted_jobs()
        async with sessions() as session:
            after = await snapshot(session)
        assert after["download_jobs"][0]["status"] == "cancelled"
        assert after["job_items"][0]["status"] == "cancelled"
        assert after["download_jobs"][0]["collection_cancel_requested_at"]
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("concurrency", [1, 2])
async def test_real_start_recovery_before_claim_single_only_and_single_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrency: int,
) -> None:
    engine, sessions = factory(tmp_path)
    _, manager, bus = runtime(sessions)
    manager._concurrency = concurrency
    ran = asyncio.Event()
    ids: list[str] = []

    async def single(job_id: str, **kwargs: Any) -> None:
        async with sessions() as session:
            state = await snapshot(session)
        rows = {r["id"]: r for r in state["download_jobs"]}
        assert rows["parent"]["status"] == "failed"
        assert rows["old-single"]["error_code"] == "interrupted_by_restart"
        assert rows["old-single"]["completed_items"] == 7  # Legacy semantics unchanged.
        assert rows["normal"]["status"] == "queued"
        ids.append(job_id)
        if len(ids) == concurrency:
            ran.set()
        await manager._stop_event.wait()

    try:
        await seed(sessions, ["downloading"])
        async with sessions() as session:
            await jobs.stage_collection_job(session, parent("normal"), [child(
                8, job_id="normal", status="queued")], owner_id="owner")
            await jobs.add_job(session, parent(
                "old-single", job_type="single", status="finalizing",
                total_items=7, completed_items=7,
            ))
            for i in range(concurrency):
                await jobs.add_job(session, parent(f"single-{i}", job_type="single"))
            await session.commit()
        monkeypatch.setattr(queue_module, "run_download_job", single)
        await manager.start()
        await asyncio.wait_for(ran.wait(), 5)
        assert set(ids) == {f"single-{i}" for i in range(concurrency)}
        assert [e["event"] for e in bus.events] == ["job.failed", "job.failed"]
        await manager.stop()
        async with sessions() as session:
            before = await snapshot(session)
        await manager.start()
        await manager.stop()
        async with sessions() as session:
            assert await snapshot(session) == before
        assert len(bus.events) == 2
    finally:
        await manager.stop()
        await engine.dispose()
