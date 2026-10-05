import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.actors import Actor
from app.domain.analysis import MediaFormatKind, NormalizedAnalysis, NormalizedFormat
from app.domain.jobs import JobError, JobErrorCode
from app.main import create_app
from app.models.analysis import AnalysisSession
from app.models.jobs import DownloadJob, JobItem
from app.models.library import LibraryItem
from app.repositories import jobs
from app.services import job_service as service_module
from app.services.collection_persistence import (
    CollectionCancelRequestOutcome,
    stage_collection_cancel_request,
)
from app.services.job_runner import RunnerDeps
from app.services.job_service import JobService
from app.services.queue_manager import QueueManager
from tests.test_collection_cancel import RecordingBus, checkpoint
from tests.test_collection_child_runner import Harness
from tests.test_collection_parent_runner import seed
from tests.test_collection_repository import child, parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def runtime(h: Harness, concurrency: int = 1) -> tuple[QueueManager, JobService]:
    bus = RecordingBus()
    manager = QueueManager(
        session_factory=h.sessions,
        deps=RunnerDeps(h.sessions, h.adapters, h.adapters, h.storage, bus, 0),
        collection_deps=h.deps, concurrency=concurrency, poll_interval_seconds=0.01,
    )
    return manager, JobService(h.sessions, manager, bus)


async def terminal(h: Harness, job_id: str = "parent") -> DownloadJob:
    async def wait() -> DownloadJob:
        while True:
            async with h.sessions() as session:
                record = await jobs.get_job_any_owner(session, job_id)
            if record is not None and record.status in {"failed", "cancelled", "completed"}:
                return record
            await checkpoint()
    return await asyncio.wait_for(wait(), 5)


@pytest.mark.anyio
async def test_live_queue_order_failure_real_catalog_and_retry_skip(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    manager, service = runtime(h)
    real_probe = h.adapters.probe_child
    visited: list[str] = []
    fail = True

    async def probe(url: str):
        visited.append(url)
        if fail and url.endswith("2"):
            raise JobError(JobErrorCode.NETWORK_ERROR, detail="real child failure")
        return await real_probe(url)

    h.adapters.probe_child = probe  # type: ignore[method-assign]
    try:
        await seed(h, [child(9), child(4, status="completed"), child(2)])
        await manager.start()
        first = await terminal(h)
        await manager.wait_for_attempt("parent", retrieve_outcome=True)
        assert visited == ["https://example.com/2", "https://example.com/9"]
        assert (first.status, first.error_code, first.total_items,
                first.completed_items, first.failed_items) == ("failed", "partial_failure", 3, 2, 1)
        assert first.progress_percent == pytest.approx(200 / 3)
        async with h.sessions() as session:
            failed = await session.get(JobItem, "child-2")
            assert failed and failed.error_detail == "real child failure"
            assert (await session.scalars(select(LibraryItem.id))).all() == ["child-9"]
            completed = await session.get(JobItem, "child-9")
            assert completed
            path, stamp = completed.final_path, completed.completed_at
        old_event = manager.cancel_event_for("parent")
        old_gate = manager._cancel_gates["parent"]
        fail = False
        await service.retry_job(Actor(id="owner"), "parent")
        second = await terminal(h)
        await manager.wait_for_attempt("parent", retrieve_outcome=True)
        assert second.status == "completed" and second.completed_items == 3
        assert visited == ["https://example.com/2", "https://example.com/9",
                           "https://example.com/2"]
        assert manager.cancel_event_for("parent") is not old_event
        assert manager._cancel_gates["parent"] is not old_gate
        async with h.sessions() as session:
            completed = await session.get(JobItem, "child-9")
            assert completed and (completed.final_path, completed.completed_at) == (path, stamp)
            assert set((await session.scalars(select(LibraryItem.id))).all()) == {
                "child-2", "child-9"}
    finally:
        await manager.stop()
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("step", ["probe", "high", "merge"])
@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_live_owned_slot_full_drain_and_settlement(
    tmp_path: Path, step: str, action: str,
) -> None:
    h = Harness(tmp_path)
    manager, service = runtime(h)
    h.adapters.block = step
    operation = None
    try:
        await seed(h, [child(2), child(9)])
        await manager.start()
        await asyncio.wait_for(h.adapters.started.wait(), 5)
        owned = manager._tasks["parent"]
        gate = manager._cancel_gates["parent"]
        event = manager.cancel_event_for("parent")
        assert not event.is_set()
        async with h.sessions() as session:
            await jobs.add_job(session, parent("next", job_type="single"))
            await session.commit()
        operation = asyncio.create_task(
            service.cancel_job(Actor(id="owner"), "parent") if action == "cancel"
            else manager.stop())
        await asyncio.wait_for(h.adapters.cleanup.wait(), 5)
        for _ in range(3):
            await manager._claim_and_run()
            await checkpoint()
            assert manager._tasks == {"parent": owned}
            assert not owned.done() and not operation.done()
            assert manager._cancel_gates["parent"] is gate
        async with h.sessions() as session:
            record = await session.get(DownloadJob, "parent")
            assert record and bool(record.collection_cancel_requested_at) == (action == "cancel")
            assert not (await session.scalars(select(LibraryItem))).all()
        h.adapters.release.set()
        await asyncio.wait_for(operation, 5)
        assert owned.done() and h.adapters.reaped and not manager._processes
        settled = await terminal(h)
        assert settled.status == ("cancelled" if action == "cancel" else "failed")
        assert settled.error_code == ("cancelled" if action == "cancel"
                                      else "interrupted_by_restart")
        assert h.adapters.calls.count("probe") == 1
        async with h.sessions() as session:
            sibling = await session.get(JobItem, "child-9")
            assert sibling and sibling.status == ("cancelled" if action == "cancel" else "queued")
    finally:
        h.adapters.release.set()
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await manager.stop()
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("concurrency", [1, 2])
async def test_live_collection_global_slots_and_sequential_children(
    tmp_path: Path, concurrency: int,
) -> None:
    h = Harness(tmp_path)
    manager, _ = runtime(h, concurrency)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = maximum = 0
    visited: list[str] = []
    real_probe = h.adapters.probe_child

    async def probe(url: str):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        visited.append(url)
        if active == concurrency:
            entered.set()
        try:
            await release.wait()
            return await real_probe(url)
        finally:
            active -= 1

    h.adapters.probe_child = probe  # type: ignore[method-assign]
    try:
        async with h.sessions() as session:
            for i in range(3):
                job_id = f"parent-{i}"
                await jobs.stage_collection_job(session, parent(job_id, collection_mode="video"),
                    [child(9, job_id, id=f"last-{i}"), child(2, job_id, id=f"first-{i}")],
                    owner_id="owner")
            await session.commit()
        await manager.start()
        await asyncio.wait_for(entered.wait(), 5)
        assert len(manager._tasks) == concurrency
        for _ in range(3):
            await manager._claim_and_run()
            assert len(manager._tasks) == concurrency
        release.set()
        for i in range(3):
            assert (await terminal(h, f"parent-{i}")).status == "completed"
        assert maximum == concurrency and active == 0
        assert visited.count("https://example.com/2") == 3
        assert visited.count("https://example.com/9") == 3
    finally:
        release.set()
        await manager.stop()
        await h.engine.dispose()


@pytest.mark.anyio
async def test_startup_marker_settlement_before_any_probe_and_unsupported_filter(tmp_path: Path):
    h = Harness(tmp_path)
    manager, _ = runtime(h)
    try:
        await seed(h, [child()], collection_cancel_requested_at=datetime.now(UTC))
        async with h.sessions() as session:
            # Model a corrupt legacy row in this isolated temporary database.
            await session.execute(text("PRAGMA ignore_check_constraints = ON"))
            await jobs.add_job(session, parent("unsupported", job_type="internal"))
            await session.commit()
            assert [r.id for r in await jobs.claimable_queued_jobs(session, limit=1)] == ["parent"]
        await manager.start()
        assert (await terminal(h)).status == "cancelled"
        assert not h.adapters.calls and "parent" not in manager._tasks
        async with h.sessions() as session:
            unsupported = await session.get(DownloadJob, "unsupported")
            assert unsupported and unsupported.status == "queued"
    finally:
        await manager.stop()
        await h.engine.dispose()


@pytest.mark.anyio
async def test_production_dependencies_share_configured_analyzer(settings: Settings):
    application = create_app(settings.model_copy(update={
        "yt_dlp_binary": "configured-yt-dlp", "analysis_timeout_seconds": 17.0}))
    try:
        manager = application.state.queue_manager
        deps = manager._collection_deps
        assert deps is not None
        assert deps.analyzer is application.state.analysis_service._analyzer
        assert deps.analyzer._binary == "configured-yt-dlp"
        assert deps.analyzer._timeout_seconds == 17.0
        assert deps.downloader is manager._deps.downloader
        assert deps.ffmpeg is manager._deps.ffmpeg
        assert deps.storage is manager._deps.storage
        assert deps.session_factory is application.state.sessionmaker
    finally:
        await application.state.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("concurrency", [1, 2])
async def test_mixed_real_pipelines_bound_downloader_processes_and_raw_child_order(
    tmp_path: Path, concurrency: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    manager, _ = runtime(h, concurrency)
    entered, release = asyncio.Event(), asyncio.Event()
    active = maximum = 0
    active_owners: set[str] = set()
    downloads: list[tuple[str, str, bool]] = []
    staging_owners: dict[Path, str] = {}

    class Process:
        returncode: int | None = None

        def kill(self) -> None:
            self.returncode = 0

        async def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

    async def registered(owner: str, callback: Any) -> Process:
        nonlocal active, maximum
        assert owner not in active_owners  # No child/stream/merge fan-out.
        active_owners.add(owner)
        active += 1
        maximum = max(maximum, active)
        process = Process()
        result = callback(cast(asyncio.subprocess.Process, process))
        if result is not None:
            await result
        assert active <= concurrency
        assert len(manager._tasks) <= concurrency
        assert len(manager._processes) <= concurrency
        return process

    def finished(owner: str, process: Process) -> None:
        nonlocal active
        process.returncode = 0
        active_owners.remove(owner)
        active -= 1

    async def download(**kwargs: Any) -> list[str]:
        url = kwargs["url"]
        owner = "single" if url.endswith("single") else url.split("/")[-2]
        template = Path(kwargs["output_template"])
        staging_owners[template.parent] = owner
        downloads.append((url, kwargs["format_selector"], kwargs.get("child_output", False)))
        process = await registered(owner, kwargs["on_process_started"])
        try:
            if active == concurrency:
                entered.set()
            await release.wait()
            path = Path(str(template).replace("%(ext)s", "mp4"))
            path.write_bytes(b"controlled media")
            return [str(path)]
        finally:
            finished(owner, process)

    async def merge(video: Path, audio: Path, output: Path,
                    on_process_started: Any = None) -> None:
        owner = staging_owners[video.parent]
        process = await registered(owner, on_process_started)
        try:
            await checkpoint()
            output.write_bytes(video.read_bytes() + audio.read_bytes())
        finally:
            finished(owner, process)

    monkeypatch.setattr(h.adapters, "download", download)
    monkeypatch.setattr(h.adapters, "merge_streams", merge)
    now = datetime.now(UTC)
    normalized = NormalizedAnalysis(
        analysis_id="single-analysis", original_url="https://example.com/single",
        detected_platform="generic", detected_platform_label="Generic", content_type="single",
        formats=[NormalizedFormat(variant_id="mux", format_id="mux", kind=MediaFormatKind.MUXED,
                                  ext="mp4", video_codec="avc1", audio_codec="mp4a.40.2")],
    )
    try:
        async with h.sessions() as session:
            session.add(AnalysisSession(
                id="single-analysis", owner_id="owner", source_url=normalized.original_url,
                status="completed", normalized_result_json=normalized.model_dump_json(),
                created_at=now, expires_at=now + timedelta(hours=1),
            ))
            await jobs.add_job(session, parent(
                "single", job_type="single", analysis_id="single-analysis", variant_id="mux",
                source_url=normalized.original_url, title="Single", created_at=now,
            ))
            for i in range(2):
                job_id = f"collection-{i}"
                await jobs.stage_collection_job(session, parent(
                    job_id, collection_mode="video", title=job_id,
                    created_at=now + timedelta(seconds=i + 1)), [
                        child(9, job_id, id=f"last-{i}",
                              source_url=f"https://example.com/{job_id}/9"),
                        child(2, job_id, id=f"first-{i}",
                              source_url=f"https://example.com/{job_id}/2"),
                    ], owner_id="owner")
            await session.commit()
        await manager.start()
        await asyncio.wait_for(entered.wait(), 5)
        assert len(manager._tasks) == concurrency and active == concurrency
        assert manager._tasks.get("single") is not None
        if concurrency == 2:
            assert "collection-0" in manager._tasks
        for _ in range(3):
            await manager._claim_and_run()
            assert len(manager._tasks) == concurrency
        release.set()
        for job_id in ("single", "collection-0", "collection-1"):
            assert (await terminal(h, job_id)).status == "completed"
            await manager.wait_for_attempt(job_id, retrieve_outcome=True)
        assert maximum == concurrency and active == 0 and not manager._processes
        assert ("https://example.com/single", "mux", False) in downloads
        for i in range(2):
            assert [(url.rsplit("/", 1)[-1], selector, is_child)
                    for url, selector, is_child in downloads if f"/collection-{i}/" in url] == [
                        ("2", "high", True), ("2", "audio", True),
                        ("9", "high", True), ("9", "audio", True),
                    ]
        async with h.sessions() as session:
            assert len((await session.scalars(select(LibraryItem))).all()) == 5
    finally:
        release.set()
        await manager.stop()
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("race", ["marker", "lost_claim", "stop"])
async def test_selected_collection_revalidates_before_probe_and_releases_slot(
    tmp_path: Path, race: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    manager, service = runtime(h)
    entered, release = asyncio.Event(), asyncio.Event()
    attempted = asyncio.Event()
    claim_return = asyncio.Event()
    real_query = jobs.claimable_queued_jobs
    real_claim = jobs.stage_collection_claim
    paused = False
    stop = None

    async def query(session: AsyncSession, *, limit: int,
                    include_collections: bool = True):
        nonlocal paused
        rows = await real_query(session, limit=limit, include_collections=include_collections)
        if rows and not paused:
            paused = True
            entered.set()
            await release.wait()
        return rows

    async def claim(session: AsyncSession, job_id: str, owner_id: str) -> bool:
        result = await real_claim(session, job_id, owner_id)
        assert not result
        attempted.set()
        await claim_return.wait()
        return result

    monkeypatch.setattr(jobs, "claimable_queued_jobs", query)
    monkeypatch.setattr(jobs, "stage_collection_claim", claim)
    try:
        await seed(h, [child(2), child(9)])
        await manager.start()
        await asyncio.wait_for(entered.wait(), 5)
        assert not manager._tasks
        if race == "marker":
            async with h.sessions() as session:
                assert await stage_collection_cancel_request(
                    session, job_id="parent", owner_id="owner",
                ) == CollectionCancelRequestOutcome.STAGED
                await session.commit()
            # Commit-to-signal window: CAS must stop work without a runtime event.
            assert "parent" not in manager._cancel_events
        elif race == "lost_claim":
            async with h.sessions() as session:
                # Another persisted claim wins; selection is NOT the claim.
                assert await real_claim(session, "parent", "owner")
                await session.commit()
        else:
            stop = asyncio.create_task(manager.stop())
            await checkpoint()
            assert manager._stop_event.is_set()
        release.set()
        if race != "stop":
            await asyncio.wait_for(attempted.wait(), 5)
            owned = manager._tasks["parent"]
            claim_return.set()
            await manager.wait_for_attempt("parent", retrieve_outcome=True)
            assert owned.done() and not h.adapters.calls
            if race == "marker":
                cancelled = await service.cancel_job(Actor(id="owner"), "parent")
                assert cancelled.status == "cancelled"
                assert cancelled.collection_cancel_requested_at
            await manager._claim_and_run()
            assert "parent" not in manager._tasks
        else:
            assert stop is not None
            await asyncio.wait_for(stop, 5)
            assert not attempted.is_set() and not manager._tasks
        assert not h.adapters.calls and not manager._processes
        async with h.sessions() as session:
            record = await session.get(DownloadJob, "parent")
            assert record and record.status == {
                "marker": "cancelled", "lost_claim": "analyzing", "stop": "queued",
            }[race]
            assert not (await session.scalars(select(LibraryItem))).all()
    finally:
        release.set()
        claim_return.set()
        if stop is not None:
            await asyncio.gather(stop, return_exceptions=True)
        await manager.stop()
        await h.engine.dispose()


@pytest.mark.anyio
async def test_retry_commit_visible_lock_skips_old_runtime_until_post_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    manager, service = runtime(h)
    entered, release = asyncio.Event(), asyncio.Event()
    real_stage = service_module.stage_collection_retry
    real_close = AsyncSession.close
    reset_session: AsyncSession | None = None
    retried = None
    h.adapters.block = "probe"

    async def stage(session: AsyncSession, **kwargs: Any) -> bool:
        nonlocal reset_session
        result = await real_stage(session, **kwargs)
        if result:
            reset_session = session
        return result

    async def close(session: AsyncSession) -> None:
        await real_close(session)
        if session is reset_session:
            entered.set()
            await release.wait()

    monkeypatch.setattr(service_module, "stage_collection_retry", stage)
    monkeypatch.setattr(AsyncSession, "close", close)
    try:
        await seed(h, [child(2, status="cancelled"), child(9, status="completed")],
                   status="cancelled", collection_cancel_requested_at=datetime.now(UTC))
        old_event = manager.cancel_event_for("parent")
        old_event.set()
        old_gate = manager._cancel_gates.setdefault("parent", h.gate)
        await manager.start()
        retried = asyncio.create_task(service.retry_job(Actor(id="owner"), "parent"))
        await asyncio.wait_for(entered.wait(), 5)
        async with h.sessions() as session:
            queued = await session.get(DownloadJob, "parent")
            assert queued and queued.status == "queued"
            assert queued.collection_cancel_requested_at is None
        for _ in range(3):
            await manager._claim_and_run()
            assert "parent" not in manager._tasks and not h.adapters.calls
            assert manager._cancel_events["parent"] is old_event
            assert manager._cancel_gates["parent"] is old_gate
            assert manager.lifecycle_lock("parent").locked()
        release.set()
        assert await asyncio.wait_for(retried, 5) is not None
        await asyncio.wait_for(h.adapters.started.wait(), 5)
        assert manager.cancel_event_for("parent") is not old_event
        assert not manager.cancel_event_for("parent").is_set()
        assert manager._cancel_gates["parent"] is not old_gate
        assert h.adapters.urls == ["https://example.com/2"]  # Completed row never probed.
    finally:
        release.set()
        h.adapters.release.set()
        if retried is not None:
            await asyncio.gather(retried, return_exceptions=True)
        await manager.stop()
        await h.engine.dispose()
