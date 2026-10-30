import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import IntegrityError

from app.domain.actors import Actor
from app.domain.analysis import MediaFormatKind, NormalizedFormat
from app.domain.jobs import JobError, JobErrorCode, JobStatus
from app.domain.library import DeleteMediaFileOutcome
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs
from app.services import collection_persistence as persistence
from app.services.conversion_plan import ConversionPlan
from app.services.library_service import LibraryService
from app.services.storage_service import (
    MediaFileMissingError,
    PlacementResult,
    PreparedMediaFile,
    StorageService,
)
from tests.test_collection_repository import child, factory, parent

SELECTED = NormalizedFormat(
    variant_id="native", format_id="251", kind=MediaFormatKind.AUDIO_ONLY,
    audio_codec="opus", bitrate_kbps=128,
)
REQUEST_STAGED = persistence.CollectionCancelRequestOutcome.STAGED


async def placed(tmp_path: Path) -> tuple[StorageService, PlacementResult]:
    storage = StorageService(tmp_path / "staging", tmp_path / "library")
    source = tmp_path / "source.opus"
    source.write_bytes(b"media bytes")
    result = await storage.place_final(source, media_kind="audio", title="Track")
    storage.initialize_delete_roots()
    return storage, result


async def complete(session: Any, storage: StorageService, placement: PlacementResult,
                   **overrides: Any) -> bool:
    args: dict[str, Any] = dict(job_id="parent", owner_id="owner", child_id="child-2",
                                selected=SELECTED, placement=placement, duration_seconds=12.5)
    args.update(overrides)
    return await persistence.stage_child_completion(session, storage, **args)


@pytest.mark.parametrize("mismatch", ["owner", "parent_id", "child_id", "child_parent",
                                      "type", "status", "child_status"])
def test_completion_checks_persisted_eligibility(tmp_path: Path, mismatch: str) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        storage, placement = await placed(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(
                    session, parent(status="analyzing"), [child(2, status="finalizing")],
                    owner_id="owner",
                )
                if mismatch == "type":
                    await session.execute(update(DownloadJob).values(job_type="single"))
                if mismatch == "status":
                    await session.execute(update(DownloadJob).values(status="completed"))
                if mismatch == "child_status":
                    await session.execute(update(JobItem).values(status="downloading"))
                if mismatch == "child_parent":
                    await jobs.stage_collection_job(session, parent("other"), [], owner_id="owner")
                    await session.execute(update(JobItem).values(job_id="other"))
                await session.commit()
            args = {"owner_id": "stranger"} if mismatch == "owner" else {}
            if mismatch == "parent_id":
                args["job_id"] = "missing"
            if mismatch == "child_id":
                args["child_id"] = "missing"
            async with sessions() as session:
                assert not await complete(session, storage, placement, **args)
                await session.commit()
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(LibraryItem)) == 0
                record = await session.get(DownloadJob, "parent")
                assert record is not None and record.completed_items == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


async def snapshot(session: Any) -> dict[str, list[dict[str, Any]]]:
    """All persisted facts, including timestamps and Activity (not ORM caches)."""
    return {model.__tablename__: [dict(row) for row in (await session.execute(
        select(model.__table__).order_by(model.__table__.c.id)
    )).mappings()] for model in (DownloadJob, JobItem, LibraryItem, MediaFile, ActivityEvent)}


async def request(session: Any, **overrides: Any) -> persistence.CollectionCancelRequestOutcome:
    args = dict(job_id="parent", owner_id="owner")
    args.update(overrides)
    return await persistence.stage_collection_cancel_request(session, **args)


async def settle(session: Any, **overrides: Any) -> bool:
    args = dict(job_id="parent", owner_id="owner")
    args.update(overrides)
    return await persistence.stage_collection_failure_settlement(session, **args)


@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize("statuses", [["completed", "completed"], [], ["failed"],
                                     ["completed", "queued"], ["completed", "unknown"]])
def test_success_settlement_actual_all_rows_fresh_marker_and_immutable_children(
    tmp_path: Path, marker: bool, statuses: list[str],
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(session, parent(status="analyzing"), [
                    child(i + 1, status=s) for i, s in enumerate(statuses)], owner_id="owner")
                await session.execute(update(DownloadJob).values(
                    total_items=99, completed_items=50, failed_items=49,
                    error_code="internal_error", error_message="old", error_detail="old"))
                await session.commit()
            async with sessions() as stale:
                cached = await stale.get(DownloadJob, "parent")
                assert cached and cached.collection_cancel_requested_at is None
                await stale.commit()
                if marker:
                    async with sessions() as writer:
                        assert await request(writer) == REQUEST_STAGED
                        await writer.commit()
                    assert cached.collection_cancel_requested_at is None
                async with sessions() as observer:
                    before = await snapshot(observer)
                expected = bool(statuses) and all(s == "completed" for s in statuses)
                assert await persistence.stage_collection_success_settlement(
                    stale, job_id="parent", owner_id="owner") == expected
                if expected:
                    await stale.commit()
                else:
                    await stale.rollback()
            async with sessions() as observer:
                after = await snapshot(observer)
            if not expected:
                assert after == before
            else:
                row = after["download_jobs"][0]
                assert (row["status"], row["total_items"], row["completed_items"],
                        row["failed_items"], row["progress_percent"]) == ("completed", 2, 2, 0, 100)
                assert (row["error_code"], row["error_message"], row["error_detail"]) == (
                    None, None, None)
                assert row["collection_cancel_requested_at"] == (
                    before["download_jobs"][0]["collection_cancel_requested_at"])
                for table in ("job_items", "library_items", "media_files"):
                    assert after[table] == before[table]
                assert len(after["activity_events"]) == len(before["activity_events"]) + 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize("statuses", [
    [], ["completed"], ["completed", "completed"],
    *[["failed", s] for s in ("queued", "analyzing", "downloading", "post_processing",
                               "finalizing")],
    *[["completed", "failed", s] for s in ("cancelled", "incomplete", "unknown")],
])
def test_failure_settlement_ineligible_rolls_back_every_fact(
    tmp_path: Path, marker: bool, statuses: list[str],
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(session, parent(status="analyzing"), [
                    child(i + 1, status=s, error_code="network_error" if s == "failed" else None)
                    for i, s in enumerate(statuses)
                ], owner_id="owner")
                # Deliberately misleading cached/persisted parent facts must not
                # make an incomplete or missing selection appear terminal.
                await session.execute(update(DownloadJob).values(
                    total_items=99, completed_items=50, failed_items=49, progress_percent=88,
                    error_code="internal_error", error_message="old", error_detail="old detail",
                ))
                await session.commit()
            if marker:
                async with sessions() as session:
                    assert await request(session) == REQUEST_STAGED
                    await session.commit()
            async with sessions() as observer:
                before = await snapshot(observer)
            async with sessions() as session:
                assert not await settle(session)
                await session.rollback()
            async with sessions() as observer:
                assert await snapshot(observer) == before
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize("mixed,heterogeneous", [(False, False), (False, True), (True, True)])
def test_failure_settlement_uses_actual_rows_and_stable_codes(
    tmp_path: Path, marker: bool, mixed: bool, heterogeneous: bool,
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        storage, placement = await placed(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(session, parent(status="analyzing"), [
                    child(1, status="failed", error_code="network_error", error_detail="reason1"),
                    child(2, status="finalizing" if mixed else "failed",
                          error_code=None if mixed else (
                              "timeout" if heterogeneous else "network_error"),
                          error_detail="reason2"),
                ], owner_id="owner")
                await session.commit()
            if mixed:
                async with sessions() as session:
                    assert await complete(session, storage, placement)
                    await session.commit()
            async with sessions() as session:
                await session.execute(update(DownloadJob).values(
                    total_items=99, completed_items=0, failed_items=0, progress_percent=3,
                ))
                await session.commit()
            async with sessions() as stale:
                cached = await stale.get(DownloadJob, "parent")
                assert cached is not None and cached.collection_cancel_requested_at is None
                await stale.commit()  # fresh transaction, same stale identity map
                if marker:
                    async with sessions() as writer:
                        assert await request(writer) == REQUEST_STAGED
                        await writer.commit()
                    assert cached.collection_cancel_requested_at is None
                async with sessions() as observer:
                    before = await snapshot(observer)
                assert await settle(stale)
                await stale.commit()
            async with sessions() as observer:
                after = await snapshot(observer)
                row = after["download_jobs"][0]
                assert (row["status"], row["error_code"]) == (
                    ("cancelled", "cancelled") if marker else (
                        "failed", "partial_failure" if mixed else "all_children_failed"))
                assert (row["total_items"], row["completed_items"], row["failed_items"],
                        row["progress_percent"]) == (
                            2, int(mixed), 2 - int(mixed), 50 if mixed else 0)
                assert row["completed_at"] is not None
                assert row["collection_cancel_requested_at"] == (
                    before["download_jobs"][0]["collection_cancel_requested_at"])
                for table in ("job_items", "library_items", "media_files"):
                    assert after[table] == before[table]
                assert len(after["activity_events"]) == len(before["activity_events"]) + 1
                assert not JobError(JobErrorCode(row["error_code"])).retryable
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("first", ["cancel", "failure"])
def test_independent_waiting_writer_observes_durable_winner(tmp_path: Path, first: str) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        entered = threading.Event()
        try:
            async with sessions() as seed:
                await jobs.stage_collection_job(seed, parent(status="analyzing"),
                                                [child(1, status="failed")], owner_id="owner")
                await seed.commit()
            async with sessions() as winner, sessions() as waiting:
                if first == "cancel":
                    assert await request(winner) == REQUEST_STAGED
                else:
                    assert await settle(winner)

                def started(conn: Any, cursor: Any, statement: str, parameters: Any,
                            context: Any, executemany: bool) -> None:
                    if statement.startswith("UPDATE download_jobs"):
                        entered.set()

                event.listen(engine.sync_engine, "before_cursor_execute", started)
                loser = asyncio.create_task(
                    settle(waiting) if first == "cancel" else request(waiting))
                assert await asyncio.to_thread(entered.wait, 5)
                assert not loser.done()  # SQL entered, winner still owns writer boundary
                await winner.commit()
                result = await loser
                if first == "cancel":
                    assert result is True
                    await waiting.commit()
                else:
                    assert result == persistence.CollectionCancelRequestOutcome.REJECTED
                    await waiting.rollback()
                event.remove(engine.sync_engine, "before_cursor_execute", started)
            async with sessions() as observer:
                row = await observer.get(DownloadJob, "parent")
                assert row is not None
                assert row.status == ("cancelled" if first == "cancel" else "failed")
                assert (row.collection_cancel_requested_at is not None) == (first == "cancel")
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["request", "settle"])
@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize(
    "failure", ["rollback", "activity_write", "parent_write", "commit", "lost_cas"]
)
def test_parent_staging_failures_roll_back_all_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, failure: str, marker: bool,
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(session, parent(status="analyzing"),
                                                [child(1, status="failed")], owner_id="owner")
                await session.commit()
                if marker:
                    assert await request(session) == REQUEST_STAGED
                    await session.commit()
                before = await snapshot(session)
            async with sessions() as session:
                if failure == "activity_write":
                    async def fail_activity(*args: Any, **kwargs: Any) -> None:
                        raise RuntimeError("Activity write failed")
                    monkeypatch.setattr(persistence, "add_activity_event", fail_activity)
                if failure == "commit":
                    async def fail_commit() -> None:
                        raise RuntimeError("commit failed")
                    monkeypatch.setattr(session, "commit", fail_commit)
                if failure == "parent_write":
                    real_execute = session.execute
                    calls = 0

                    async def fail_write(statement: Any, *args: Any, **kwargs: Any) -> Any:
                        nonlocal calls
                        if getattr(statement, "is_update", False):
                            calls += 1
                            if calls == (2 if operation == "settle" else 1):
                                raise RuntimeError("parent write failed")
                        return await real_execute(statement, *args, **kwargs)
                    monkeypatch.setattr(session, "execute", fail_write)
                if failure == "lost_cas":
                    real_execute = session.execute
                    calls = 0

                    async def lose(statement: Any, *args: Any, **kwargs: Any) -> Any:
                        nonlocal calls
                        if getattr(statement, "is_update", False):
                            calls += 1
                            if (operation == "request" and calls == 1) or (
                                operation == "settle" and calls == 2
                            ):
                                await real_execute(update(DownloadJob).values(status="completed"))
                        return await real_execute(statement, *args, **kwargs)
                    monkeypatch.setattr(session, "execute", lose)
                stage = request if operation == "request" else settle
                if failure == "activity_write" and operation == "request" and marker:
                    # Duplicate requests stage no Activity at all.
                    assert await stage(session) == (
                        persistence.CollectionCancelRequestOutcome.ALREADY_REQUESTED)
                elif failure in {"activity_write", "parent_write", "commit"}:
                    with pytest.raises(RuntimeError):
                        await stage(session)
                        await session.commit()
                else:
                    outcome = await stage(session)
                    if failure == "lost_cas":
                        assert outcome == (persistence.CollectionCancelRequestOutcome.REJECTED
                                           if operation == "request" else False)
                await session.rollback()
            async with sessions() as observer:
                assert await snapshot(observer) == before
            # A rolled-back request is not durable; a rolled-back failure is not terminal.
            monkeypatch.undo()
            async with sessions() as session:
                assert await settle(session)
                await session.commit()
            async with sessions() as session:
                row = await session.get(DownloadJob, "parent")
                assert row is not None and row.status == ("cancelled" if marker else "failed")
            async with sessions() as session:
                assert await request(session) == persistence.CollectionCancelRequestOutcome.REJECTED
                await session.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("marker", [False, True])
@pytest.mark.parametrize("before,after", [
    ("queued", "analyzing"), ("analyzing", "downloading"),
    ("downloading", "post_processing"), ("downloading", "finalizing"),
    ("post_processing", "finalizing"),
])
def test_cancel_blocks_every_native_child_forward_cas(
    tmp_path: Path, before: str, after: str, marker: bool,
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(session, parent(status="analyzing"),
                                                [child(1, status=before)], owner_id="owner")
                await session.commit()
            async with sessions() as session:
                if marker:
                    assert await request(session) == REQUEST_STAGED
                    await session.commit()
                facts = await snapshot(session)
            async with sessions() as session:
                won = await persistence.stage_child_transition(
                    session, job_id="parent", owner_id="owner", child_id="child-1",
                    expected_status=JobStatus(before), status=JobStatus(after),
                )
                assert won == (not marker)
                await session.commit()
            async with sessions() as observer:
                if marker:
                    assert await snapshot(observer) == facts
                else:
                    row = await observer.get(JobItem, "child-1")
                    assert row is not None and row.status == after
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["claim", "progress", "completion"])
def test_cancel_blocks_claim_progress_and_catalog_completion(
    tmp_path: Path, operation: str,
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        storage, placement = await placed(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(
                    session, parent(status="queued" if operation == "claim" else "analyzing"),
                    [child(2, status="downloading" if operation == "progress" else "finalizing")],
                    owner_id="owner",
                )
                await session.commit()
            async with sessions() as session:
                assert await request(session) == persistence.CollectionCancelRequestOutcome.STAGED
                await session.commit()
                facts = await snapshot(session)
            async with sessions() as session:
                if operation == "claim":
                    assert not await jobs.stage_collection_claim(session, "parent", "owner")
                elif operation == "progress":
                    assert not await persistence.stage_child_progress(
                        session, job_id="parent", owner_id="owner", child_id="child-2",
                        progress_percent=70, downloaded_bytes=7, speed_bytes_per_second=9,
                    )
                else:
                    assert not await complete(session, storage, placement)
                await session.commit()
            async with sessions() as observer:
                assert await snapshot(observer) == facts
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [None, "owner", "completed_child", "terminal_parent"])
def test_actual_failure_after_cancel_preserves_error_and_guards(
    tmp_path: Path, invalid: str | None,
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(session, parent(status="analyzing"), [
                    child(1, status="completed", final_path="kept.opus"),
                    child(2, status="completed" if invalid == "completed_child" else "downloading"),
                ], owner_id="owner")
                await session.commit()
            async with sessions() as session:
                assert await request(session) == persistence.CollectionCancelRequestOutcome.STAGED
                if invalid == "terminal_parent":
                    await session.execute(update(DownloadJob).values(status="cancelled"))
                await session.commit()
                facts = await snapshot(session)
            async with sessions() as session:
                won = await persistence.stage_child_transition(
                    session, job_id="parent", owner_id="other" if invalid == "owner" else "owner",
                    child_id="child-2", expected_status=JobStatus.DOWNLOADING,
                    status=JobStatus.FAILED,
                    error=JobError(JobErrorCode.NETWORK_ERROR, "real child reason"),
                )
                assert won == (invalid is None)
                await session.commit()
            async with sessions() as observer:
                after = await snapshot(observer)
                if invalid:
                    assert after == facts
                else:
                    assert after["job_items"][0] == facts["job_items"][0]
                    assert after["job_items"][1]["error_detail"] == "real child reason"
                    row = after["download_jobs"][0]
                    assert (row["total_items"], row["completed_items"], row["failed_items"],
                            row["progress_percent"], row["status"]) == (2, 1, 1, 50, "analyzing")
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_atomic_completion_identity_order_and_truthful_aggregates(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        storage, placement = await placed(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(session, parent(status="analyzing"), [
                    child(9, source_id="same", status="finalizing"),
                    child(2, source_id="same", title="Child title", status="finalizing",
                          track_number=7, disc_number=2),
                    child(15, status="failed"), child(20, status="cancelled"),
                ], owner_id="owner")
                await session.commit()
            async with sessions() as session:
                assert await complete(session, storage, placement)
                async with sessions() as observer:
                    row = await observer.get(JobItem, "child-2")
                    assert row is not None and row.status == "finalizing"
                    assert await observer.get(LibraryItem, "child-2") is None
                await session.commit()
            async with sessions() as session:
                assert not await complete(session, storage, placement)
                assert not await persistence.stage_child_progress(
                    session, job_id="parent", owner_id="owner", child_id="child-2",
                    progress_percent=1, total_bytes=99, eta_seconds=10,
                )
                assert not await persistence.stage_child_transition(
                    session, job_id="parent", owner_id="owner", child_id="child-2",
                    expected_status=JobStatus.FINALIZING, status=JobStatus.FAILED,
                    error=JobError(JobErrorCode.NETWORK_ERROR),
                )
                assert await complete(session, storage, placement, child_id="child-9")
                await session.commit()
            async with sessions() as session:
                record = await session.get(DownloadJob, "parent")
                assert record is not None
                assert (record.total_items, record.completed_items, record.failed_items,
                        record.progress_percent, record.status) == (4, 2, 1, 50, "analyzing")
                assert (record.total_bytes, record.downloaded_bytes, record.eta_seconds) == (
                    None, None, None,
                )
                items = await jobs.list_job_items(session, "parent", "owner")
                assert [i.source_order for i in items] == [2, 9, 15, 20]
                catalog = (await session.execute(select(LibraryItem))).scalars().all()
                assert {i.id for i in catalog} == {"child-2", "child-9"}
                assert all(i.source_id == "same" and i.owner_id == "owner"
                           and i.job_id == "parent" for i in catalog)
                entry = await session.get(LibraryItem, "child-2")
                assert entry is not None
                assert (entry.title, entry.source_url, entry.track_number, entry.disc_number,
                        entry.format, entry.codec, entry.duration) == (
                    "Child title", "https://example.com/2", 7, 2, "opus", "opus", 12.5,
                )
                copies = (await session.execute(select(MediaFile))).scalars().all()
                assert len(copies) == 2
                assert all(i.path == placement.relative_path
                           and i.size_bytes == placement.size_bytes for i in copies)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("plan,skipped,expected", [
    (None, False, ("opus", 128)),
    (None, True, ("opus", 128)),
    (ConversionPlan(True, False, "opus", True), False, ("opus", 128)),
    (ConversionPlan(False, False, "mp3", False), False, ("mp3", None)),
    (ConversionPlan(True, False, "opus", True), True, (None, None)),
    (ConversionPlan(False, False, "flac", False), True, (None, None)),
])
def test_completion_catalog_codec_bitrate_follow_output_plan(
    tmp_path: Path, plan: ConversionPlan | None, skipped: bool,
    expected: tuple[str | None, int | None],
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        storage, placement = await placed(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(
                    session, parent(status="analyzing"), [child(2, status="finalizing")],
                    owner_id="owner",
                )
                await session.commit()
            async with sessions() as session:
                assert await complete(session, storage,
                                      replace(placement, skipped_existing=skipped), plan=plan)
                await session.commit()
            async with sessions() as session:
                entry = await session.get(LibraryItem, "child-2")
                assert entry is not None
                assert (entry.codec, entry.bitrate) == expected
                assert (entry.format, entry.media_type, entry.duration) == ("opus", "audio", 12.5)
                record = await session.get(DownloadJob, "parent")
                assert record is not None and record.completed_items == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["insert", "identity_collision", "verify", "counter"])
def test_completion_rolls_back_child_catalog_and_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        storage, placement = await placed(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(
                    session, parent(status="analyzing"), [child(2, status="finalizing")],
                    owner_id="owner",
                )
                if failure == "identity_collision":
                    session.add(LibraryItem(
                        id="child-2", owner_id="other", job_id="other", source_url="other",
                        media_type="video", title="unrelated", created_at=parent().created_at,
                    ))
                await session.commit()
            if failure == "verify":
                def fail_verify(*args: Any) -> None:
                    raise MediaFileMissingError("removed")
                monkeypatch.setattr(storage, "verify_media_file", fail_verify)
            if failure == "counter":
                async def fail_counter(*args: Any) -> None:
                    raise RuntimeError("counter failure")
                monkeypatch.setattr(jobs, "stage_collection_aggregates", fail_counter)
            async with sessions() as session:
                broken = PlacementResult(placement.final_path, placement.relative_path,
                                         False, None, placement.identity)  # type: ignore[arg-type]
                expected = {"insert": IntegrityError, "identity_collision": IntegrityError,
                            "verify": MediaFileMissingError, "counter": RuntimeError}[failure]
                with pytest.raises(expected):
                    await complete(session, storage, broken if failure == "insert" else placement)
                await session.rollback()
            async with sessions() as session:
                row = await session.get(JobItem, "child-2")
                assert row is not None
                assert (row.status, row.progress_percent, row.final_path, row.completed_at) == (
                    "finalizing", 0, None, None,
                )
                record = await session.get(DownloadJob, "parent")
                assert record is not None
                assert (record.completed_items, record.progress_percent) == (0, 0)
                assert await session.scalar(select(func.count()).select_from(MediaFile)) == 0
                entry = await session.get(LibraryItem, "child-2")
                if failure == "identity_collision":
                    assert entry is not None and entry.owner_id == "other"
                else:
                    assert entry is None
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_field_scoped_claim_pipeline_progress_failure_and_empty_selection(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(session, parent(), [child(2)], owner_id="owner")
                await jobs.stage_collection_job(session, parent("empty"), [], owner_id="owner")
                await session.commit()
            async with sessions() as session:
                assert not await jobs.stage_collection_claim(session, "parent", "other")
                assert await jobs.stage_collection_claim(session, "parent", "owner")
                assert not await jobs.stage_collection_claim(session, "parent", "owner")
                for before, after in [(JobStatus.QUEUED, JobStatus.ANALYZING),
                                      (JobStatus.ANALYZING, JobStatus.DOWNLOADING)]:
                    assert await persistence.stage_child_transition(
                        session, job_id="parent", owner_id="owner", child_id="child-2",
                        expected_status=before, status=after,
                    )
                assert await persistence.stage_child_progress(
                    session, job_id="parent", owner_id="owner", child_id="child-2",
                    progress_percent=100, downloaded_bytes=10, total_bytes=None, eta_seconds=None,
                )
                record = await session.get(DownloadJob, "parent")
                assert record is not None and record.progress_percent == 0
                assert await persistence.stage_child_transition(
                    session, job_id="parent", owner_id="owner", child_id="child-2",
                    expected_status=JobStatus.DOWNLOADING, status=JobStatus.FAILED,
                    error=JobError(JobErrorCode.NETWORK_ERROR),
                )
                await session.commit()
            async with sessions() as session:
                record = await session.get(DownloadJob, "parent")
                assert record is not None and record.failed_items == 1
                assert record.status == "analyzing" and record.progress_percent == 0
                with pytest.raises(ValueError):
                    await persistence.stage_child_transition(
                        session, job_id="parent", owner_id="owner", child_id="child-2",
                        expected_status=JobStatus.FAILED, status=JobStatus.QUEUED,
                    )
                assert await jobs.stage_collection_claim(session, "empty", "owner")
                await jobs.stage_collection_aggregates(session, "empty", "owner")
                await session.commit()
            async with sessions() as session:
                empty = await session.get(DownloadJob, "empty")
                assert empty is not None and empty.total_items == 0 and empty.progress_percent == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_delete_file_lock_winner_makes_completion_reverify_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        storage, placement = await placed(tmp_path)
        deleting, release = threading.Event(), threading.Event()
        real_delete = PreparedMediaFile.delete

        def gated_delete(target: PreparedMediaFile) -> Path:
            deleting.set()
            assert release.wait(5)
            return real_delete(target)

        monkeypatch.setattr(PreparedMediaFile, "delete", gated_delete)
        try:
            async with sessions() as session:
                await jobs.stage_collection_job(
                    session, parent(status="analyzing"), [child(2, status="finalizing")],
                    owner_id="owner",
                )
                entry = LibraryItem(id="existing", owner_id=Actor().id, job_id="old",
                                    source_url="https://example.com/old", media_type="audio",
                                    title="Old", created_at=parent().created_at)
                copy = MediaFile(library_item_id=entry.id, storage_type="app_library",
                                 path=placement.relative_path, size_bytes=placement.size_bytes,
                                 created_at=entry.created_at)
                session.add(entry)
                await session.flush()
                session.add(copy)
                await session.commit()
                copy_id = copy.id
            deletion = asyncio.create_task(LibraryService(sessions, storage).delete_media_file(
                Actor(), item_id="existing", media_file_id=copy_id,
            ))
            assert await asyncio.to_thread(deleting.wait, 5)
            async with sessions() as session:
                writer = asyncio.create_task(complete(session, storage, placement))
                await asyncio.sleep(0.2)
                assert not writer.done()
                release.set()
                assert await deletion is DeleteMediaFileOutcome.DELETED
                with pytest.raises(MediaFileMissingError):
                    await writer
                await session.rollback()
            async with sessions() as session:
                row = await session.get(JobItem, "child-2")
                assert row is not None and row.status == "finalizing"
                record = await session.get(DownloadJob, "parent")
                assert record is not None and record.completed_items == 0
                assert await session.scalar(select(func.count()).select_from(LibraryItem)) == 0
                assert await session.scalar(select(func.count()).select_from(MediaFile)) == 0
        finally:
            release.set()
            await engine.dispose()
    asyncio.run(scenario())
