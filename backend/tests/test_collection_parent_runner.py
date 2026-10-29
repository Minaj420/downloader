import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from app.domain.jobs import JobError, JobErrorCode
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.models.library import LibraryItem
from app.repositories import jobs
from app.services import collection_parent_runner as runner
from app.services.collection_child_runner import ChildResult
from app.services.collection_persistence import (
    CollectionCancelRequestOutcome,
    stage_collection_cancel_request,
)
from tests.test_collection_cancel import RecordingBus, checkpoint, runtime
from tests.test_collection_child_runner import Harness
from tests.test_collection_repository import child, parent
from tests.test_prepared_media import source


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def seed(h: Harness, children: list[JobItem], **values: Any) -> None:
    async with h.sessions() as session:
        await jobs.stage_collection_job(
            session, parent(collection_mode="video", **values), children, owner_id="owner")
        await session.commit()


async def run(h: Harness, owner: str = "owner") -> runner.ParentRunOutcome:
    return await runner.run_collection_parent(
        "parent", owner, deps=h.deps, cancel_event=h.adapters.cancel,
        processes=h.processes, cancel_gate=h.gate,
    )


async def status(h: Harness) -> str:
    async with h.sessions() as session:
        value = await session.scalar(select(DownloadJob.status))
        assert value is not None
        return value


@pytest.mark.anyio
async def test_real_heterogeneous_pipeline_raw_order_and_completed_without_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    active = maximum = 0
    visited: list[str] = []
    real_child = runner.run_collection_child
    real_probe = h.adapters.probe_child

    async def probe(url: str) -> dict[str, Any]:
        h.adapters.source = source(video_codec="vp9" if url.endswith("9") else "avc1")
        return await real_probe(url)

    async def owned(job_id: str, owner_id: str, child_id: str, **kwargs: Any) -> ChildResult:
        nonlocal active, maximum
        assert kwargs["cancel_event"] is h.adapters.cancel
        assert kwargs["cancel_gate"] is h.gate
        assert kwargs["processes"] is h.processes
        assert job_id == "parent" and not h.processes
        active += 1
        maximum = max(maximum, active)
        visited.append(child_id)
        try:
            return await real_child(job_id, owner_id, child_id, **kwargs)
        finally:
            active -= 1
            assert not h.processes

    real_register = h.adapters.download

    async def download(**kwargs: Any) -> list[str]:
        def registered(process: asyncio.subprocess.Process) -> Any:
            value = callback(process)
            assert h.processes == {"parent": process}
            return value
        callback = kwargs["on_process_started"]
        kwargs["on_process_started"] = registered
        return await real_register(**kwargs)

    monkeypatch.setattr(runner, "run_collection_child", owned)
    monkeypatch.setattr(h.adapters, "probe_child", probe)
    monkeypatch.setattr(h.adapters, "download", download)
    try:
        await seed(h, [child(9, playlist_index=1, source_id="same", title="Nine"),
                       child(4, status="completed"),
                       child(2, playlist_index=99, source_id="same", title="Two")])
        assert await run(h) == runner.ParentRunOutcome.EXHAUSTED
        assert visited == ["child-2", "child-9"] and maximum == 1 and active == 0
        assert h.adapters.calls == ["probe", "high", "audio", "merge"] * 2
        assert await status(h) == "completed"
        async with h.sessions() as session:
            items = (await session.scalars(select(LibraryItem).order_by(LibraryItem.id))).all()
            assert [(i.id, i.format) for i in items] == [("child-2", "mp4"), ("child-9", "mkv")]
            completed = await session.get(JobItem, "child-4")
            assert completed is not None and completed.status == "completed"
            assert await session.get(LibraryItem, "child-4") is None
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_selected_video_orders_keep_gaps_in_filenames(tmp_path: Path) -> None:
    from app.models.library import MediaFile

    h = Harness(tmp_path)
    try:
        await seed(h, [child(2, title="Same"), child(5, status="completed", title="Same"),
                       child(9, title="Same")])
        assert await run(h) == runner.ParentRunOutcome.EXHAUSTED
        assert h.adapters.calls.count("probe") == 2
        video = h.storage.library_root / "video"
        assert sorted(p.name for p in video.iterdir()) == ["02 - Same.mp4", "09 - Same.mp4"]
        async with h.sessions() as session:
            paths = (await session.scalars(select(MediaFile.path).order_by(MediaFile.path))).all()
            assert paths == ["video/02 - Same.mp4", "video/09 - Same.mp4"]
            titles = (await session.scalars(select(LibraryItem.title))).all()
            assert titles == ["Same", "Same"]
            assert await session.get(LibraryItem, "child-5") is None
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_retry_after_publication_before_catalog_skips_same_prefixed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.library import MediaFile
    from app.services import collection_child_runner as child_runner

    h = Harness(tmp_path)
    real_completion = child_runner.stage_child_completion

    async def fail_once(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(child_runner, "stage_child_completion", real_completion)
        raise RuntimeError("catalog failure after publication")

    monkeypatch.setattr(child_runner, "stage_child_completion", fail_once)
    try:
        await seed(h, [child(2, title="Same")])
        with pytest.raises(JobError) as caught:
            await run(h)
        assert caught.value.code == JobErrorCode.INTERNAL_ERROR
        target = h.storage.library_root / "video" / "02 - Same.mp4"
        published = (target.read_bytes(), target.stat().st_ino)
        # Simulated retry reset of the unfinished attempt (not under test here).
        async with h.sessions() as session:
            await session.execute(update(JobItem).values(status="queued"))
            await session.execute(update(DownloadJob).values(status="queued"))
            await session.commit()
        assert await run(h) == runner.ParentRunOutcome.EXHAUSTED
        assert (target.read_bytes(), target.stat().st_ino) == published
        assert [p.name for p in target.parent.iterdir()] == ["02 - Same.mp4"]
        async with h.sessions() as session:
            assert (await session.scalars(select(MediaFile.path))).all() == [
                "video/02 - Same.mp4"]
        assert await run(h) == runner.ParentRunOutcome.SKIPPED
        assert h.adapters.calls.count("probe") == 2
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["owner", "kind", "failed", "completed", "cancelled",
                                  "analyzing", "marker"])
async def test_actual_claim_cas_rejects_before_child(tmp_path: Path, case: str) -> None:
    h = Harness(tmp_path)
    try:
        values: dict[str, Any] = {}
        if case in {"failed", "completed", "cancelled", "analyzing"}:
            values["status"] = case
        if case == "marker":
            values["collection_cancel_requested_at"] = datetime.now(UTC)
        await seed(h, [child()], **values)
        if case == "kind":
            async with h.sessions() as session:
                await session.execute(update(DownloadJob).values(job_type="single"))
                await session.commit()
        assert await run(h, "foreign" if case == "owner" else "owner") == (
            runner.ParentRunOutcome.SKIPPED)
        assert not h.adapters.calls
        assert await status(h) == values.get("status", "queued")
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("statuses", [[], ["queued", "cancelled"],
    ["queued", "incomplete"], ["queued", "unknown"], ["queued", "analyzing"],
    ["queued", "downloading"], ["queued", "post_processing"], ["queued", "finalizing"]])
async def test_rejects_entire_unsupported_subset_before_any_probe(
    tmp_path: Path, statuses: list[str],
) -> None:
    h = Harness(tmp_path)
    try:
        await seed(h, [child(i + 1, status=s) for i, s in enumerate(statuses)])
        with pytest.raises(runner.UnsupportedChildSelection):
            await run(h)
        assert not h.adapters.calls and await status(h) == "analyzing"
        async with h.sessions() as session:
            assert (await session.scalars(select(JobItem.status)
                                         .order_by(JobItem.source_order))).all() == statuses
            assert await session.scalar(select(DownloadJob.progress_percent)) == 0
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_corrupt_queued_source_rejected(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    try:
        await seed(h, [child(source_url="")])
        with pytest.raises(runner.UnsupportedChildSelection):
            await run(h)
        assert not h.adapters.calls and await status(h) == "analyzing"
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["marker", "owner", "kind", "terminal", "event"])
async def test_boundary_stops_next_probe_before_runtime_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    h = Harness(tmp_path)
    real_child = runner.run_collection_child

    async def first(*args: Any, **kwargs: Any) -> ChildResult:
        result = await real_child(*args, **kwargs)
        async with h.sessions() as session:
            if change == "marker":
                assert await stage_collection_cancel_request(
                    session, job_id="parent", owner_id="owner") == (
                        CollectionCancelRequestOutcome.STAGED)
            elif change != "event":
                field, value = {"owner": ("owner_id", "foreign"),
                                "kind": ("job_type", "single"),
                                "terminal": ("status", "failed")}[change]
                await session.execute(update(DownloadJob).values(**{field: value}))
            await session.commit()
        if change == "event":
            h.adapters.cancel.set()
        else:
            assert not h.adapters.cancel.is_set()
        return result

    monkeypatch.setattr(runner, "run_collection_child", first)
    try:
        await seed(h, [child(2), child(9)])
        if change == "event":
            with pytest.raises(asyncio.CancelledError):
                await run(h)
        else:
            assert await run(h) == runner.ParentRunOutcome.LOST_ELIGIBILITY
        assert h.adapters.calls.count("probe") == 1
        async with h.sessions() as session:
            second = await session.get(JobItem, "child-9")
            assert second is not None and second.status == "queued"
        if change == "marker":
            assert await status(h) == "analyzing"
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["skipped", "mapped", "lost_cas"])
async def test_child_skip_or_error_never_continues_or_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    h = Harness(tmp_path)
    calls: list[str] = []

    async def controlled(job_id: str, owner_id: str, child_id: str,
                         **kwargs: Any) -> ChildResult:
        calls.append(child_id)
        if failure == "mapped":
            raise JobError(JobErrorCode.NETWORK_ERROR)
        return ChildResult.SKIPPED

    async def lose() -> None:
        async with h.sessions() as session:
            await session.execute(update(DownloadJob).values(
                collection_cancel_requested_at=datetime.now(UTC)))
            await session.commit()

    if failure == "lost_cas":
        h.adapters.after_probe = lose
    else:
        monkeypatch.setattr(runner, "run_collection_child", controlled)
    try:
        await seed(h, [child(2), child(9)])
        if failure == "mapped":
            with pytest.raises(JobError) as caught:
                await run(h)
            assert caught.value.code == JobErrorCode.NETWORK_ERROR
        else:
            assert await run(h) == runner.ParentRunOutcome.LOST_ELIGIBILITY
        assert calls == ([] if failure == "lost_cas" else ["child-2"])
        assert h.adapters.calls == (["probe"] if failure == "lost_cas" else [])
        assert await status(h) == "analyzing"
        async with h.sessions() as session:
            second = await session.get(JobItem, "child-9")
            assert second is not None and second.status == "queued"
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("step", ["probe", "high", "merge"])
@pytest.mark.parametrize("origin", ["caller", "event"])
async def test_single_registered_parent_slot_kept_through_repeated_cancel_drain(
    tmp_path: Path, step: str, origin: str,
) -> None:
    h = Harness(tmp_path)
    _, manager, _ = runtime(h.sessions)
    attempt = waiter = None
    try:
        await seed(h, [child(2), child(9)])
        h.adapters.block = step
        manager._cancel_events["parent"] = h.adapters.cancel
        manager._cancel_gates["parent"] = h.gate
        manager._processes = h.processes

        async def registered() -> None:
            await run(h)

        async with manager.lifecycle_lock("parent"):
            attempt = asyncio.create_task(registered())
            manager._tasks["parent"] = attempt
        await asyncio.wait_for(h.adapters.started.wait(), 2)
        if origin == "caller":
            attempt.cancel()
        else:
            await manager.request_collection_cancel("parent")
        await asyncio.wait_for(h.adapters.cleanup.wait(), 2)

        async def wait_under_cancel_lock() -> None:
            async with manager.lifecycle_lock("parent"):
                await manager.wait_for_attempt("parent", retrieve_outcome=True)

        waiter = asyncio.create_task(wait_under_cancel_lock())
        for _ in range(3):
            attempt.cancel()
            await checkpoint()
            await manager._claim_and_run()
            assert manager._tasks == {"parent": attempt}
            assert not attempt.done() and not waiter.done()
        h.adapters.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(attempt, 2)
        await asyncio.wait_for(waiter, 2)
        assert h.adapters.reaped and not h.processes
        assert h.adapters.calls.count("probe") == 1
        assert await status(h) == "analyzing"
        await manager._claim_and_run()
        assert not manager._tasks  # Only now may the caller release its slot.
    finally:
        h.adapters.release.set()
        for task in (attempt, waiter):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
async def test_all_completed_exhaustion_is_atomic_terminal_success(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    try:
        await seed(h, [child(status="completed")])
        assert await run(h) == runner.ParentRunOutcome.EXHAUSTED
        assert not h.adapters.calls and await status(h) == "completed"
        async with h.sessions() as session:
            assert await session.scalar(select(DownloadJob.completed_at)) is not None
            assert await session.scalar(select(DownloadJob.progress_percent)) == 100
            assert (await session.scalars(select(ActivityEvent.event_type))).all() == ["completed"]
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_real_disk_errors_recorded_and_continue_to_all_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)

    async def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError("bounded fake disk error")

    monkeypatch.setattr(h.storage, "place_child_final", fail)
    try:
        await seed(h, [child(2), child(9)])
        assert await run(h) == runner.ParentRunOutcome.EXHAUSTED
        assert not h.processes and h.adapters.calls.count("probe") == 2
        assert await status(h) == "failed"
        async with h.sessions() as session:
            first = await session.get(JobItem, "child-2")
            second = await session.get(JobItem, "child-9")
            assert first is not None and first.status == "failed"
            assert first.error_code == "disk_write_failed"
            assert first.error_detail == "OSError('bounded fake disk error')"
            assert second is not None and second.status == "failed"
            record = await session.get(DownloadJob, "parent")
            assert record is not None and record.error_code == "all_children_failed"
            assert (record.total_items, record.completed_items, record.failed_items,
                    record.progress_percent) == (2, 0, 2, 0)
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("codes", [[JobErrorCode.NETWORK_ERROR, None],
                                  [JobErrorCode.NETWORK_ERROR, JobErrorCode.TIMEOUT],
                                  [JobErrorCode.NETWORK_ERROR, JobErrorCode.NETWORK_ERROR]])
async def test_real_pipeline_mixed_and_allfailed_real_reasons_no_failed_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codes: list[JobErrorCode | None],
) -> None:
    h = Harness(tmp_path)
    bus = RecordingBus()
    real_probe = h.adapters.probe_child

    async def probe(url: str) -> dict[str, Any]:
        result = await real_probe(url)
        code = codes[0 if url.endswith("2") else 1]
        if code:
            raise JobError(code, detail=f"real reason {url}")
        return result

    monkeypatch.setattr(h.adapters, "probe_child", probe)
    try:
        await seed(h, [child(2), child(9), child(15, status="failed",
                       error_code="media_unavailable", error_detail="immutable")])
        assert await runner.run_collection_parent(
            "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
            processes=h.processes, cancel_gate=h.gate, bus=bus,
        ) == runner.ParentRunOutcome.EXHAUSTED
        assert h.adapters.calls.count("probe") == 2
        async with h.sessions() as session:
            record = await session.get(DownloadJob, "parent")
            first = await session.get(JobItem, "child-2")
            old = await session.get(JobItem, "child-15")
            assert record and first and old
            assert codes[0] is not None
            c = int(codes[1] is None)
            assert (record.status, record.error_code, record.total_items,
                    record.completed_items, record.failed_items, record.progress_percent) == (
                        "failed", "partial_failure" if c else "all_children_failed",
                        3, c, 3 - c, 100 * c / 3)
            assert (first.error_code, first.error_message, first.error_detail) == (
                codes[0].value, JobError(codes[0]).message, "real reason https://example.com/2")
            assert old.error_detail == "immutable"
            assert (record.total_bytes, record.downloaded_bytes, record.eta_seconds) == (
                None, None, None)
            assert (await session.get(LibraryItem, "child-9") is not None) == bool(c)
        assert len([e for e in bus.events if e["event"] == "job.failed"]) == 1
        assert bus.events[-1]["data"]["failed_items"] == 3 - c
        assert await run(h) == runner.ParentRunOutcome.SKIPPED
        assert h.adapters.calls.count("probe") == 2
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("terminal", ["success", "failure"])
async def test_accepted_cancel_waiting_runner_returns_winner_without_duplicate_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str,
) -> None:
    from app.domain.actors import Actor

    h = Harness(tmp_path)
    service, manager, bus = runtime(h.sessions)
    boundary, release, accepted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_child = runner.run_collection_child
    real_signal = manager.request_collection_cancel

    async def probe(*args: Any, **kwargs: Any) -> Any:
        raise JobError(JobErrorCode.NETWORK_ERROR, detail="actual child failure")

    async def child_boundary(*args: Any, **kwargs: Any) -> ChildResult:
        try:
            return await real_child(*args, **kwargs)
        finally:
            boundary.set()
            await release.wait()  # child fully drained; no SQL writer lock held

    async def signal(job_id: str) -> None:
        accepted.set()
        await real_signal(job_id)
        release.set()

    monkeypatch.setattr(runner, "run_collection_child", child_boundary)
    monkeypatch.setattr(manager, "request_collection_cancel", signal)
    if terminal == "failure":
        monkeypatch.setattr(h.adapters, "probe_child", probe)
    attempt = request = None
    try:
        await seed(h, [child(2)])
        manager._cancel_events["parent"] = h.adapters.cancel
        manager._cancel_gates["parent"] = h.gate

        async def registered() -> None:
            assert await runner.run_collection_parent(
                "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
                processes=h.processes, cancel_gate=h.gate, bus=bus) == (
                    runner.ParentRunOutcome.EXHAUSTED)

        attempt = asyncio.create_task(registered())
        manager._tasks["parent"] = attempt
        await asyncio.wait_for(boundary.wait(), 5)
        request = asyncio.create_task(service.cancel_job(Actor(id="owner"), "parent"))
        await asyncio.wait_for(accepted.wait(), 5)
        result = await asyncio.wait_for(request, 5)
        await attempt
        expected = "completed" if terminal == "success" else "cancelled"
        assert result.status == expected and result.collection_cancel_requested_at is not None
        assert (result.total_items, result.completed_items, result.failed_items) == (
            1, int(terminal == "success"), int(terminal == "failure"))
        assert len([e for e in bus.events if e["event"] == f"job.{expected}"]) == 1
        async with h.sessions() as session:
            events = (await session.scalars(select(ActivityEvent))).all()
            assert len([e for e in events if e.event_type == expected
                        and '"child_id"' not in (e.details_json or "")]) == 1
            assert len([e for e in events if e.event_type == "cancel_requested"]) == 1
        before = list(bus.events)
        with pytest.raises(JobError) as caught:
            await service.cancel_job(Actor(id="owner"), "parent")
        assert caught.value.code == JobErrorCode.INVALID_STATE and bus.events == before
    finally:
        release.set()
        for task in (attempt, request):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
async def test_request_during_real_failure_stops_queued_sibling_and_api_preserves_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.domain.actors import Actor

    h = Harness(tmp_path)
    service, manager, bus = runtime(h.sessions)
    failing, accepted = asyncio.Event(), asyncio.Event()
    real_signal = manager.request_collection_cancel

    async def probe(*args: Any, **kwargs: Any) -> Any:
        failing.set()
        await accepted.wait()
        raise JobError(JobErrorCode.TIMEOUT, detail="real timeout after acceptance")

    async def signal(job_id: str) -> None:
        # Exercise the commit-to-signal gap: marker alone must stop next child.
        accepted.set()

    monkeypatch.setattr(h.adapters, "probe_child", probe)
    monkeypatch.setattr(manager, "request_collection_cancel", signal)
    attempt = request = None
    try:
        await seed(h, [child(1, status="completed"), child(2), child(9)])

        async def registered() -> None:
            assert await run(h) == runner.ParentRunOutcome.LOST_ELIGIBILITY

        attempt = asyncio.create_task(registered())
        manager._tasks["parent"] = attempt
        await asyncio.wait_for(failing.wait(), 5)
        request = asyncio.create_task(service.cancel_job(Actor(id="owner"), "parent"))
        result = await asyncio.wait_for(request, 5)
        await attempt
        assert not h.adapters.cancel.is_set()
        assert (result.status, result.total_items, result.completed_items,
                result.failed_items, result.progress_percent) == ("cancelled", 3, 1, 1, 100 / 3)
        async with h.sessions() as session:
            rows = (await session.scalars(select(JobItem).order_by(JobItem.source_order))).all()
            assert [r.status for r in rows] == ["completed", "failed", "cancelled"]
            assert rows[1].error_detail == "real timeout after acceptance"
        assert bus.events[-1]["event"] == "job.cancelled"
        await real_signal("parent")
    finally:
        accepted.set()
        for task in (attempt, request):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
async def test_callback_100_before_merge_is_not_parent_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations.ytdlp import DownloadProgress
    from app.services import collection_child_runner as child_runner

    h = Harness(tmp_path)
    real_download = h.adapters.download
    real_merge = h.adapters.merge_streams
    real_forwarder = child_runner._ChildExecution.forwarder
    writes: list[asyncio.Task[None]] = []

    def forwarder(execution: Any, index: int, count: int) -> Any:
        forward = real_forwarder(execution, index, count)

        def recorded(value: DownloadProgress) -> asyncio.Task[None]:
            task = forward(value)
            writes.append(task)
            return task
        return recorded

    async def download(**kwargs: Any) -> list[str]:
        result = await real_download(**kwargs)
        kwargs["on_progress"](DownloadProgress(10, 10, None, 8, 0))
        await asyncio.gather(*writes)
        return result

    async def merge(*args: Any, **kwargs: Any) -> None:
        async with h.sessions() as session:
            record = await session.get(DownloadJob, "parent")
            item = await session.get(JobItem, "child-2")
            assert record and item
            assert item.status == "post_processing"
            assert (record.completed_items, record.progress_percent) == (0, 0)
            assert await session.get(LibraryItem, "child-2") is None
        await real_merge(*args, **kwargs)

    monkeypatch.setattr(h.adapters, "download", download)
    monkeypatch.setattr(child_runner._ChildExecution, "forwarder", forwarder)
    monkeypatch.setattr(h.adapters, "merge_streams", merge)
    try:
        await seed(h, [child(2)])
        assert await run(h) == runner.ParentRunOutcome.EXHAUSTED
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["failure_record", "success", "failure"])
@pytest.mark.parametrize("failure", ["activity", "commit"])
async def test_runner_transaction_failure_whole_rollback_no_partial_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, failure: str,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from tests.test_collection_persistence import snapshot

    h = Harness(tmp_path)
    bus = RecordingBus()
    baseline: dict[str, list[dict[str, Any]]] | None = None
    original = (runner.stage_child_transition if phase == "failure_record" else
                runner.stage_collection_success_settlement if phase == "success" else
                runner.stage_collection_failure_settlement)

    async def fail_probe(*args: Any, **kwargs: Any) -> Any:
        raise JobError(JobErrorCode.NETWORK_ERROR, detail="real mapped error")

    async def staged(session: Any, **kwargs: Any) -> Any:
        nonlocal baseline
        async with h.sessions() as observer:
            baseline = await snapshot(observer)
        result = await original(session, **kwargs)
        if failure == "activity":
            if phase != "failure_record":
                raise RuntimeError("after staged terminal Activity")
            async def fail_activity(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("child failure Activity write")
            monkeypatch.setattr(runner.activity, "add_activity_event", fail_activity)
        else:
            # Real SQLite COMMIT failure; the staged writes remain in the
            # transaction until the runner explicitly rolls the whole unit back.
            await session.execute(text("INSERT INTO runner_commit_probe VALUES (1, 2)"))
        return result

    target = ("stage_child_transition" if phase == "failure_record" else
              f"stage_collection_{phase}_settlement")
    monkeypatch.setattr(runner, target, staged)
    if phase == "failure_record":
        monkeypatch.setattr(h.adapters, "probe_child", fail_probe)
    try:
        await seed(h, [child(2, status=("queued" if phase == "failure_record" else
                                     "completed" if phase == "success" else "failed"))])
        async with h.sessions() as session:
            await session.execute(text(
                "CREATE TABLE runner_commit_probe (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES runner_commit_probe(id) "
                "DEFERRABLE INITIALLY DEFERRED)"))
            await session.commit()
        with pytest.raises(IntegrityError if failure == "commit" else RuntimeError):
            await runner.run_collection_parent(
                "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
                processes=h.processes, cancel_gate=h.gate, bus=bus)
        assert baseline is not None
        async with h.sessions() as observer:
            assert await snapshot(observer) == baseline
        assert not bus.events and not h.processes
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("barrier", ["commit", "close"])
async def test_runner_terminal_outcome_shield_keeps_slot_through_repeated_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, barrier: str,
) -> None:
    h = Harness(tmp_path)
    _, manager, bus = runtime(h.sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    original = runner.stage_collection_success_settlement
    attempt = None

    async def staged(session: Any, **kwargs: Any) -> bool:
        result = await original(session, **kwargs)
        real_barrier = getattr(session, barrier)

        async def held() -> None:
            if barrier == "commit":
                await real_barrier()
            entered.set()
            await release.wait()
            if barrier == "close":
                await real_barrier()

        monkeypatch.setattr(session, barrier, held)
        return result

    monkeypatch.setattr(runner, "stage_collection_success_settlement", staged)
    try:
        await seed(h, [child(2, status="completed")])

        async def registered() -> None:
            await runner.run_collection_parent(
                "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
                processes=h.processes, cancel_gate=h.gate, bus=bus)

        attempt = asyncio.create_task(registered())
        manager._tasks["parent"] = attempt
        await asyncio.wait_for(entered.wait(), 5)
        assert await status(h) == "completed"
        assert not bus.events
        for _ in range(3):
            attempt.cancel()
            await checkpoint()
            await manager._claim_and_run()
            assert manager._tasks == {"parent": attempt} and not attempt.done()
            assert not bus.events
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(attempt, 5)
        assert [e["event"] for e in bus.events] == ["activity.created", "job.completed"]
        await manager._claim_and_run()
        assert not manager._tasks and not h.processes
    finally:
        release.set()
        if attempt is not None:
            await asyncio.gather(attempt, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
async def test_unknown_pipeline_failure_stops_without_recording_or_terminal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    bus = RecordingBus()

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unknown technical failure")

    monkeypatch.setattr(h.adapters, "probe_child", broken)
    try:
        await seed(h, [child(2), child(9)])
        with pytest.raises(JobError) as caught:
            await runner.run_collection_parent(
                "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
                processes=h.processes, cancel_gate=h.gate, bus=bus)
        assert caught.value.code == JobErrorCode.INTERNAL_ERROR
        async with h.sessions() as session:
            assert (await session.scalars(select(JobItem.status)
                                         .order_by(JobItem.source_order))).all() == [
                                             "analyzing", "queued"]
            assert (await session.scalars(select(ActivityEvent))).all() == []
        assert not bus.events and not h.processes
    finally:
        await h.engine.dispose()
