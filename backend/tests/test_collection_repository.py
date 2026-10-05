import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.database import create_engine_and_sessionmaker, run_migrations
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.repositories.activity import add_activity_event
from app.repositories.jobs import add_job, get_job, list_job_items, stage_collection_job
from app.services.collection_persistence import (
    CollectionCancelRequestOutcome,
    stage_collection_cancel_request,
    stage_collection_failure_settlement,
)


def parent(job_id: str = "parent", owner: str = "owner", **values: Any) -> DownloadJob:
    now = datetime.now(UTC)
    fields: dict[str, Any] = dict(
        id=job_id, owner_id=owner, analysis_id="analysis", source_url="https://example.com/list",
        title="Collection", variant_id="", destination="app_library", conflict_policy="keep_both",
        keep_app_copy=True, status="queued", progress_percent=0.0, created_at=now, updated_at=now,
        job_type="collection",
    )
    fields.update(values)
    return DownloadJob(**fields)


def child(order: int = 1, job_id: str = "parent", **values: Any) -> JobItem:
    now = datetime.now(UTC)
    fields: dict[str, Any] = dict(
        id=f"child-{order}", job_id=job_id, item_id=f"analysis:item:{order}", source_order=order,
        source_url=f"https://example.com/{order}", created_at=now, updated_at=now,
    )
    fields.update(values)
    return JobItem(**fields)


def factory(tmp_path: Path):
    database = tmp_path / "collections.sqlite"
    run_migrations(f"sqlite:///{database}")
    return create_engine_and_sessionmaker(f"sqlite+aiosqlite:///{database}")


@pytest.mark.parametrize("status", ["queued", "analyzing", "downloading", "post_processing",
                                    "finalizing", "failed", "completed", "cancelled"])
@pytest.mark.parametrize("mismatch", [None, "owner", "kind", "missing"])
def test_cancel_request_and_settlement_owned_nonterminal_boundary(
    tmp_path: Path, status: str, mismatch: str | None,
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                await stage_collection_job(
                    session, parent(status=status), [child()], owner_id="owner")
                if mismatch == "kind":
                    await session.execute(update(DownloadJob).values(job_type="single"))
                await session.commit()
            args = dict(job_id="missing" if mismatch == "missing" else "parent",
                        owner_id="other" if mismatch == "owner" else "owner")
            allowed = mismatch is None and status not in {"failed", "completed", "cancelled"}
            async with sessions() as session:
                outcome = await stage_collection_cancel_request(session, **args)
                assert outcome == (CollectionCancelRequestOutcome.STAGED if allowed
                                   else CollectionCancelRequestOutcome.REJECTED)
                if allowed:
                    await session.commit()
                else:
                    await session.rollback()
            async with sessions() as session:
                original = await get_job(session, "parent", "owner")
                assert original is not None
                stamp, updated = original.collection_cancel_requested_at, original.updated_at
                await session.commit()
                outcome = await stage_collection_cancel_request(session, **args)
                assert outcome == (CollectionCancelRequestOutcome.ALREADY_REQUESTED if allowed
                                   else CollectionCancelRequestOutcome.REJECTED)
                if allowed:
                    await session.commit()
                else:
                    await session.rollback()
                # queued child excludes settlement even though request acceptance succeeds.
                assert not await stage_collection_failure_settlement(session, **args)
                await session.rollback()
            async with sessions() as session:
                loaded = await get_job(session, "parent", "owner")
                assert loaded is not None
                assert (loaded.collection_cancel_requested_at, loaded.updated_at) == (
                    stamp, updated)
                assert loaded.status == status
                assert await session.scalar(select(func.count()).select_from(ActivityEvent)) == (
                    1 if allowed else 0)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("profile", [None, {"preference": "original_best_available"},
                                     {"preference": "future-explicit", "ceiling": 1080}])
def test_roundtrip_owner_isolation_order_and_caller_activity(tmp_path: Path, profile: Any) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                record = parent(collection_mode="audio", collection_profile_json=(
                    json.dumps(profile) if profile is not None else None
                ))
                now = datetime.now(UTC)
                entries = [
                    child(9, source_id="duplicate", title="Track", playlist_index=2,
                          track_number=3, disc_number=1, status="completed", progress_percent=100,
                          final_path="stored-output", started_at=now, completed_at=now),
                    child(2, source_id="duplicate", status="failed", progress_percent=42,
                          downloaded_bytes=42, total_bytes=100, speed_bytes_per_second=4,
                          eta_seconds=9, staging_path="stored-staging", error_code="network_error",
                          error_message="Failed", error_detail="Technical reason"),
                    child(5),
                ]
                record.completed_items = 1
                record.failed_items = 1
                await stage_collection_job(session, record, entries, owner_id="owner")
                await add_activity_event(session, job_id=record.id, level="info",
                                         event_type="queued", message="Queued")
                # A separate reader cannot observe an internally committed parent or children.
                async with sessions() as observer:
                    assert await get_job(observer, record.id, "owner") is None
                    assert await list_job_items(observer, record.id, "owner") == []
                    assert await observer.scalar(
                        select(func.count()).select_from(ActivityEvent)
                    ) == 0
                await session.commit()
            async with sessions() as session:
                loaded = await get_job(session, "parent", "owner")
                assert loaded is not None
                assert (
                    loaded.total_items, loaded.completed_items, loaded.failed_items
                ) == (3, 1, 1)
                assert loaded.collection_mode == "audio"
                assert loaded.collection_profile_json == record.collection_profile_json
                rows = await list_job_items(session, "parent", "owner")
                assert [row.source_order for row in rows] == [2, 5, 9]
                assert [row.item_id for row in rows] == ["analysis:item:2", "analysis:item:5",
                                                       "analysis:item:9"]
                assert rows[0].error_detail == "Technical reason"
                assert rows[0].staging_path == "stored-staging"
                assert (rows[0].downloaded_bytes, rows[0].total_bytes,
                        rows[0].speed_bytes_per_second, rows[0].eta_seconds) == (42, 100, 4, 9)
                assert rows[1].source_id is None and rows[1].title is None
                assert rows[1].status == "queued" and rows[1].progress_percent == 0
                assert (
                    rows[2].playlist_index, rows[2].track_number, rows[2].disc_number
                ) == (2, 3, 1)
                assert rows[2].final_path == "stored-output" and rows[2].completed_at is not None
                assert await list_job_items(session, "parent", "stranger") == []
                assert await list_job_items(session, "missing", "owner") == []
                assert await get_job(session, "parent", "stranger") is None
                assert await session.scalar(select(func.count()).select_from(ActivityEvent)) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid", ["owner", "parent", "id", "item_id", "order", "negative", "single"]
)
def test_stage_rejects_structural_mismatches_before_writes(tmp_path: Path, invalid: str) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                record, entries = parent(), [child(1), child(2)]
                if invalid == "owner":
                    record.owner_id = "stranger"
                elif invalid == "parent":
                    entries[1].job_id = "other"
                elif invalid == "id":
                    entries[1].id = entries[0].id
                elif invalid == "item_id":
                    entries[1].item_id = entries[0].item_id
                elif invalid == "order":
                    entries[1].source_order = entries[0].source_order
                elif invalid == "single":
                    record.job_type = "single"
                else:
                    entries[1].source_order = 0
                with pytest.raises(ValueError):
                    await stage_collection_job(session, record, entries, owner_id="owner")
                assert not session.new
                await session.commit()
                assert await session.scalar(select(func.count()).select_from(DownloadJob)) == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_snapshot_identity_is_parent_scoped_and_existing_children_cannot_be_staged(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                await stage_collection_job(session, parent(), [child()], owner_id="owner")
                # Reusing the snapshot item/order in a different parent is valid,
                # while its internal persistent ID remains globally unique.
                await stage_collection_job(
                    session, parent("other", "other-owner"),
                    [child(job_id="other", id="other-child")], owner_id="other-owner",
                )
                await session.commit()
            async with sessions() as session:
                existing = (await list_job_items(session, "parent", "owner"))[0]
                with pytest.raises(ValueError):
                    await stage_collection_job(
                        session, parent(), [existing], owner_id="owner"
                    )
                assert not session.new
                assert await list_job_items(session, "parent", "other-owner") == []
                assert await list_job_items(session, "other", "owner") == []
                assert len(await list_job_items(session, "other", "other-owner")) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["child_flush", "activity_flush", "caller_rollback"])
def test_parent_child_and_activity_roll_back_together(tmp_path: Path, failure: str) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                if failure == "child_flush":
                    await add_activity_event(session, job_id="parent", level="info",
                                             event_type="queued", message="Queued")
                    with pytest.raises(IntegrityError):
                        await stage_collection_job(session, parent(),
                                                   [child(source_url=None)], owner_id="owner")
                else:
                    await stage_collection_job(session, parent(), [child()], owner_id="owner")
                    if failure == "activity_flush":
                        with pytest.raises(IntegrityError):
                            await add_activity_event(
                                session, job_id="parent", level=cast(str, None),
                                event_type="queued", message="Queued"
                            )
                    else:
                        await add_activity_event(session, job_id="parent", level="info",
                                                 event_type="queued", message="Queued")
                await session.rollback()
            async with sessions() as session:
                for model in (DownloadJob, JobItem, ActivityEvent):
                    assert await session.scalar(select(func.count()).select_from(model)) == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("violation", ["item_id", "order", "id", "fk", "positive_order"])
def test_database_enforces_child_constraints(tmp_path: Path, violation: str) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                await stage_collection_job(session, parent(), [child()], owner_id="owner")
                await session.commit()
            async with sessions() as session:
                duplicate = child(2)
                if violation == "item_id":
                    duplicate.item_id = "analysis:item:1"
                elif violation == "order":
                    duplicate.source_order = 1
                elif violation == "id":
                    duplicate.id = "child-1"
                elif violation == "fk":
                    duplicate.job_id = "missing"
                else:
                    duplicate.source_order = 0
                session.add(duplicate)
                with pytest.raises(IntegrityError):
                    await session.flush()
                await session.rollback()
            async with sessions() as session:
                assert len(await list_job_items(session, "parent", "owner")) == 1
                await session.execute(delete(DownloadJob).where(DownloadJob.id == "parent"))
                await session.commit()
                assert await session.scalar(select(func.count()).select_from(JobItem)) == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_existing_parent_cannot_be_mutated_or_reused_by_stage(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                await stage_collection_job(session, parent(), [child()], owner_id="owner")
                await session.commit()
            async with sessions() as session:
                existing = await get_job(session, "parent", "owner")
                assert existing is not None
                with pytest.raises(ValueError):
                    await stage_collection_job(session, existing, [child(2)], owner_id="owner")
                await session.rollback()
            # A forged transient parent cannot overwrite a foreign-owned stored parent.
            async with sessions() as session:
                with pytest.raises(IntegrityError):
                    await stage_collection_job(session, parent(owner="stranger"), [child(2)],
                                               owner_id="stranger")
                await session.rollback()
            async with sessions() as session:
                assert len(await list_job_items(session, "parent", "owner")) == 1
                assert await list_job_items(session, "parent", "stranger") == []
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_legacy_single_construction_without_new_fields(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine, sessions = factory(tmp_path)
        try:
            async with sessions() as session:
                record = parent(variant_id="audio-original")
                del record.job_type
                await add_job(session, record)
                await session.commit()
            async with sessions() as session:
                loaded = await get_job(session, "parent", "owner")
                assert loaded is not None
                assert loaded.job_type == "single" and loaded.variant_id == "audio-original"
                assert (
                    loaded.total_items, loaded.completed_items, loaded.failed_items
                ) == (1, 0, 0)
                assert loaded.collection_mode is None and loaded.collection_profile_json is None
                assert await list_job_items(session, "parent", "owner") == []
        finally:
            await engine.dispose()
    asyncio.run(scenario())
