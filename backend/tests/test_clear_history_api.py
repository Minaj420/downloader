"""M6-Q3-Backend: Clear History preview + confirm (ARCHITECTURE §59 Q3, PRODUCT_SPEC §17).

Everything runs under ``tmp_path`` with the real StorageService; only the mount
table is stubbed (same synthetic helper as the storage tests). The queue claim
loop is paused, so seeded rows are never run.
"""

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import exc as sa_exc
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.actors import Actor
from app.domain.jobs import (
    CLEAR_HISTORY_WARNING_MESSAGE,
    RETRY_QUEUED_MESSAGE,
    ClearHistoryError,
    JobError,
    JobErrorCode,
)
from app.main import create_app
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.models.library import LibraryItem, MediaFile
from app.repositories import activity as activity_repo
from app.repositories import jobs as job_repo
from app.services import job_service as job_service_module
from app.services.job_service import CLEAR_PREVIEW_CAP, CLEAR_PREVIEW_TTL_SECONDS, JobService
from app.services.queue_manager import QueueManager
from tests.conftest import FakeAnalyzer
from tests.test_clear_history_storage_delete import _mountinfo, _stub_mountinfo
from tests.test_job_listing import BASE, app_of, collection, make_job, seed
from tests.test_job_listing import paused_queue as paused_queue

OWNER = Actor(id="local")
PREVIEW = "/api/v1/jobs/clear-history/preview"
CONFIRM = "/api/v1/jobs/clear-history/confirm"
EXPIRED_BODY = {
    "error": {
        "code": "clear_preview_expired",
        "message": "This Clear History preview has expired. Preview again.",
        "retryable": False,
        "job_id": None,
    }
}
FAILED_BODY = {
    "error": {
        "code": "clear_history_failed",
        "message": "Clear History could not finish. Refresh and preview again.",
        "retryable": False,
        "job_id": None,
    }
}


@pytest.fixture(autouse=True)
def stub_mounts(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_mountinfo(monkeypatch, _mountinfo(Path(settings.staging_dir).resolve()))


# --- helpers ----------------------------------------------------------------


def call[T](client: TestClient, work: Callable[[], Awaitable[T]]) -> T:
    assert client.portal is not None
    return client.portal.call(work)


def in_session[T](client: TestClient, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    async def run() -> T:
        async with app_of(client).state.sessionmaker() as session:
            return await work(session)

    return call(client, run)


def service_of(client: TestClient) -> JobService:
    return cast(JobService, app_of(client).state.job_service)


def manager_of(client: TestClient) -> QueueManager:
    return cast(QueueManager, app_of(client).state.queue_manager)


def add_event(
    client: TestClient, job_id: str, message: str = "Note", event_type: str = "job",
) -> int:
    async def run(session: AsyncSession) -> int:
        event = await activity_repo.add_activity_event(
            session, job_id=job_id, level="info", event_type=event_type, message=message,
        )
        await session.commit()
        return event.id

    return in_session(client, run)


def events_of(client: TestClient, job_id: str) -> list[tuple[int, str, str]]:
    async def run(session: AsyncSession) -> list[tuple[int, str, str]]:
        rows = await session.execute(
            select(ActivityEvent.id, ActivityEvent.level, ActivityEvent.message)
            .where(ActivityEvent.job_id == job_id).order_by(ActivityEvent.id)
        )
        return [tuple(r) for r in rows]  # type: ignore[misc]

    return in_session(client, run)


def jobs_in_db(client: TestClient) -> dict[str, str]:
    async def run(session: AsyncSession) -> dict[str, str]:
        rows = await session.execute(select(DownloadJob.id, DownloadJob.status))
        return {job_id: status for job_id, status in rows}

    return in_session(client, run)


def items_of(client: TestClient, job_id: str) -> list[str]:
    async def run(session: AsyncSession) -> list[str]:
        rows = await session.execute(select(JobItem.id).where(JobItem.job_id == job_id))
        return sorted(rows.scalars().all())

    return in_session(client, run)


def restore(client: TestClient, record: DownloadJob) -> None:
    """Put a Retried row back to its exact preview-time facts (frozen clock)."""

    async def run(session: AsyncSession) -> None:
        await session.execute(update(DownloadJob).where(DownloadJob.id == record.id).values(
            status=record.status, error_code=record.error_code,
            error_message=record.error_message, updated_at=record.updated_at,
            completed_at=record.completed_at, started_at=record.started_at,
        ))
        await session.commit()

    in_session(client, run)


def stage(settings: Settings, key: str) -> Path:
    path = Path(settings.staging_dir) / key
    (path / "sub").mkdir(parents=True, exist_ok=True)
    (path / "partial.bin").write_bytes(b"partial")
    (path / "sub" / "nested.bin").write_bytes(b"nested")
    return path


def preview(client: TestClient, include_cancelled: bool | None = None) -> dict[str, Any]:
    body = {} if include_cancelled is None else {"include_cancelled": include_cancelled}
    response = client.post(PREVIEW, json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"preview_id", "job_count"}
    return cast(dict[str, Any], payload)


def confirm(client: TestClient, preview_id: str) -> Any:
    return client.post(CONFIRM, json={"preview_id": preview_id})


def confirmed(client: TestClient, preview_id: str) -> tuple[int, int]:
    response = confirm(client, preview_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"deleted_count", "failed_count"}
    return body["deleted_count"], body["failed_count"]


async def checkpoint() -> None:
    ready = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(ready.set_result, None)
    await ready


def blocking_storage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, block_key: str,
) -> tuple[threading.Event, threading.Event]:
    """Pause the real Q3 removal (in its worker thread) at ``block_key``."""
    storage = service_of(client)._storage
    assert storage is not None
    real = storage.remove_staging_for_clear_history
    entered, release = threading.Event(), threading.Event()

    def remove(key: str) -> bool:
        if key == block_key:
            entered.set()
            assert release.wait(10)
        return real(key)

    monkeypatch.setattr(storage, "remove_staging_for_clear_history", remove)
    return entered, release


# --- selection, snapshot identity, count ------------------------------------


@pytest.mark.parametrize("include_cancelled", [False, True])
def test_selection_status_owner_and_count_from_same_list(
    client: TestClient, settings: Settings, include_cancelled: bool,
) -> None:
    statuses = ["queued", "analyzing", "downloading", "post_processing", "finalizing",
                "completed", "failed", "cancelled", "mystery"]
    singles = [make_job(s, BASE + timedelta(seconds=i)) for i, s in enumerate(statuses)]
    parents, children = [], []
    for i, status in enumerate(statuses):
        parent, kids = collection(BASE + timedelta(minutes=1, seconds=i), status,
                                  [("completed", None), ("failed", "network_error")])
        parents.append(parent)
        children += kids
    foreign = [make_job(s, BASE, owner="other") for s in ("completed", "failed", "cancelled")]
    seed(client, singles + parents + foreign, children)
    for record in singles + foreign:
        stage(settings, record.id)
    for kid in children:
        stage(settings, kid.id)

    wanted = {"completed", "failed"} | ({"cancelled"} if include_cancelled else set())
    body = preview(client, include_cancelled)
    expected = {r.id for r in singles + parents if r.status in wanted}
    assert body["job_count"] == len(expected) == (6 if include_cancelled else 4)
    assert confirmed(client, body["preview_id"]) == (len(expected), 0)

    remaining = jobs_in_db(client)
    assert expected.isdisjoint(remaining)
    for record in singles + parents + foreign:
        if record.id not in expected:
            assert remaining[record.id] == record.status
    for record in singles + foreign:
        assert (Path(settings.staging_dir) / record.id).exists() == (record.id not in expected)
    for kid in children:
        assert (Path(settings.staging_dir) / kid.id).exists() == (kid.job_id not in expected)


def test_default_body_excludes_cancelled_and_ids_are_unguessable(client: TestClient) -> None:
    seed(client, [make_job("cancelled", BASE), make_job("completed", BASE)])
    first, second = preview(client), preview(client, False)
    assert first["job_count"] == second["job_count"] == 1
    assert first["preview_id"] != second["preview_id"]
    assert len(first["preview_id"]) >= 32
    assert preview(client, True)["job_count"] == 2


def test_snapshot_is_exact_equal_count_never_adds_new_or_later_jobs(
    client: TestClient, settings: Settings,
) -> None:
    member = make_job("failed", BASE)  # network_error: retryable
    seed(client, [member])
    body = preview(client)
    assert body["job_count"] == 1
    assert client.post(f"/api/v1/jobs/{member.id}/retry").status_code == 200
    late = make_job("completed", BASE - timedelta(days=1))  # Older created_at, same count.
    seed(client, [late])
    stage(settings, late.id)
    assert preview(client)["job_count"] == 1  # A different job, equal count.
    assert confirmed(client, body["preview_id"]) == (0, 0)
    assert jobs_in_db(client) == {member.id: "queued", late.id: "completed"}
    assert (Path(settings.staging_dir) / late.id).is_dir()


def test_job_terminalized_after_preview_is_not_added(client: TestClient) -> None:
    active = make_job("downloading", BASE)
    seed(client, [active])
    body = preview(client)
    assert body["job_count"] == 0

    async def finish(session: AsyncSession) -> None:
        await session.execute(update(DownloadJob).values(status="completed"))
        await session.commit()

    in_session(client, finish)
    assert confirmed(client, body["preview_id"]) == (0, 0)
    assert jobs_in_db(client) == {active.id: "completed"}


def test_unknown_job_type_preview_fails_masked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("Unsupported job type for Clear History: 'x' /secret/path")

    monkeypatch.setattr(job_repo, "list_clear_history_snapshot", broken)
    response = client.post(PREVIEW, json={})
    assert response.status_code == 500
    assert response.json() == FAILED_BODY


# --- Retry marker -----------------------------------------------------------


@pytest.mark.parametrize("prior_marker", [False, True])
@pytest.mark.parametrize("kind", ["single", "collection"])
def test_post_preview_retry_to_same_terminal_is_skipped_by_marker(
    client: TestClient, settings: Settings, kind: str, prior_marker: bool,
) -> None:
    kids: list[JobItem] = []
    if kind == "single":
        record = make_job("failed", BASE)
    else:
        record, kids = collection(BASE, "failed", [("completed", None),
                                                   ("failed", "network_error")],
                                  error_code="partial_failure")
    seed(client, [record], kids)
    if prior_marker:  # Legacy NULL vs an existing marker row.
        add_event(client, record.id, RETRY_QUEUED_MESSAGE)
    keys = [record.id] if kind == "single" else [k.id for k in kids]
    for key in keys:
        stage(settings, key)
    before_events = len(events_of(client, record.id))

    body = preview(client)
    assert body["job_count"] == 1
    assert client.post(f"/api/v1/jobs/{record.id}/retry").status_code == 200
    # Same terminal status, same timestamps (clock frozen): only the marker moved.
    restore(client, record)
    assert confirmed(client, body["preview_id"]) == (0, 0)
    assert jobs_in_db(client) == {record.id: "failed"}
    assert len(events_of(client, record.id)) == before_events + 1
    for key in keys:
        assert (Path(settings.staging_dir) / key).is_dir()


def test_unchanged_existing_marker_matches_and_deletes(
    client: TestClient, settings: Settings,
) -> None:
    record = make_job("failed", BASE)
    seed(client, [record])
    add_event(client, record.id, RETRY_QUEUED_MESSAGE)
    add_event(client, record.id, "Other later event")
    stage(settings, record.id)
    assert confirmed(client, preview(client)["preview_id"]) == (1, 0)
    assert jobs_in_db(client) == {}
    assert events_of(client, record.id) == []
    assert not (Path(settings.staging_dir) / record.id).exists()


def test_non_marker_activity_and_other_jobs_markers_do_not_change_fingerprint(
    client: TestClient,
) -> None:
    record, other = make_job("completed", BASE), make_job("failed", BASE)
    seed(client, [record, other])
    body = preview(client)
    add_event(client, record.id, "Retry queued later", event_type="note")
    add_event(client, record.id, RETRY_QUEUED_MESSAGE, event_type="other")
    add_event(client, other.id, RETRY_QUEUED_MESSAGE)  # Changes only ``other``.
    assert confirmed(client, body["preview_id"]) == (1, 0)
    assert jobs_in_db(client) == {other.id: "failed"}


def test_timestamp_difference_is_an_extra_safe_skip(client: TestClient) -> None:
    record = make_job("completed", BASE)
    seed(client, [record])
    body = preview(client)

    async def touch(session: AsyncSession) -> None:
        await session.execute(update(DownloadJob).values(updated_at=BASE + timedelta(seconds=1)))
        await session.commit()

    in_session(client, touch)
    assert confirmed(client, body["preview_id"]) == (0, 0)
    assert record.id in jobs_in_db(client)


@pytest.mark.parametrize("prior_marker", [False, True])
def test_marker_appearing_before_delete_makes_cas_match_nothing(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
    prior_marker: bool,
) -> None:
    record, kids = collection(BASE, "completed", [("completed", None)])
    seed(client, [record], kids)
    if prior_marker:
        add_event(client, record.id, RETRY_QUEUED_MESSAGE)
    stage(settings, kids[0].id)
    body = preview(client)
    real = job_repo.stage_clear_history_delete
    sessions = app_of(client).state.sessionmaker

    async def racing(session: AsyncSession, *args: Any) -> int:
        async with sessions() as other:  # Committed by another writer first.
            await activity_repo.add_activity_event(
                other, job_id=record.id, level="info", event_type="job",
                message=RETRY_QUEUED_MESSAGE,
            )
            await other.commit()
        rowcount = await real(session, *args)
        assert rowcount == 0
        return rowcount

    monkeypatch.setattr(job_repo, "stage_clear_history_delete", racing)
    before = events_of(client, record.id)
    assert confirmed(client, body["preview_id"]) == (0, 0)
    after = events_of(client, record.id)
    assert after[:len(before)] == before and len(after) == len(before) + 1
    assert items_of(client, record.id) == [kids[0].id]
    # No FS+DB atomicity: storage ran first and is not restored.
    assert not (Path(settings.staging_dir) / kids[0].id).exists()


def test_only_the_two_retry_writers_emit_the_marker() -> None:
    root = Path(__file__).resolve().parent.parent / "app"
    literal, constant = [], []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if '"Retry queued"' in text:
            literal.append(path.relative_to(root).as_posix())
        if "RETRY_QUEUED_MESSAGE" in text:
            constant.append(path.relative_to(root).as_posix())
    assert literal == ["domain/jobs.py"]
    assert constant == ["domain/jobs.py", "repositories/activity.py",
                        "services/collection_persistence.py", "services/job_service.py"]
    service = (root / "services/job_service.py").read_text(encoding="utf-8")
    assert service.count("message=RETRY_QUEUED_MESSAGE") == 1
    persistence = (root / "services/collection_persistence.py").read_text(encoding="utf-8")
    assert persistence.count("message=RETRY_QUEUED_MESSAGE") == 1


# --- ownership, expiry, single use ------------------------------------------


def test_foreign_owner_does_not_consume_then_owner_uses_once(
    client: TestClient, settings: Settings,
) -> None:
    record = make_job("completed", BASE)
    seed(client, [record])
    stage(settings, record.id)
    body = preview(client)

    async def foreign() -> str:
        with pytest.raises(ClearHistoryError) as excinfo:
            await service_of(client).clear_history_confirm(Actor(id="other"), body["preview_id"])
        return excinfo.value.code

    assert call(client, foreign) == "clear_preview_expired"
    assert record.id in jobs_in_db(client)
    assert confirmed(client, body["preview_id"]) == (1, 0)
    second = confirm(client, body["preview_id"])
    assert (second.status_code, second.json()) == (409, EXPIRED_BODY)


def test_foreign_preview_never_lists_owner_jobs(client: TestClient) -> None:
    seed(client, [make_job("completed", BASE)])

    async def foreign() -> int:
        _, count = await service_of(client).clear_history_preview(
            Actor(id="other"), include_cancelled=True)
        return count

    assert call(client, foreign) == 0


def test_unknown_expired_and_evicted_previews_delete_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = make_job("completed", BASE)
    seed(client, [record])
    service = service_of(client)
    now = [1000.0]
    monkeypatch.setattr(service, "_clock", lambda: now[0])

    unknown = confirm(client, "not-issued")
    assert (unknown.status_code, unknown.json()) == (409, EXPIRED_BODY)

    expiring = preview(client)["preview_id"]
    now[0] += CLEAR_PREVIEW_TTL_SECONDS
    expired = confirm(client, expiring)
    assert (expired.status_code, expired.json()) == (409, EXPIRED_BODY)

    issued = [preview(client)["preview_id"] for _ in range(CLEAR_PREVIEW_CAP + 1)]
    evicted = confirm(client, issued[0])
    assert (evicted.status_code, evicted.json()) == (409, EXPIRED_BODY)
    assert jobs_in_db(client) == {record.id: "completed"}
    now[0] += CLEAR_PREVIEW_TTL_SECONDS - 1
    assert confirmed(client, issued[-1]) == (1, 0)


def test_restart_lost_preview_is_a_noop(
    client: TestClient, settings: Settings, analyzer: FakeAnalyzer,
) -> None:
    record = make_job("failed", BASE)
    seed(client, [record])
    stage(settings, record.id)
    preview_id = preview(client)["preview_id"]
    with TestClient(create_app(settings, analyzer=analyzer)) as restarted:
        response = restarted.post(CONFIRM, json={"preview_id": preview_id})
        assert (response.status_code, response.json()) == (409, EXPIRED_BODY)
    assert record.id in jobs_in_db(client)
    assert (Path(settings.staging_dir) / record.id).is_dir()


@pytest.mark.parametrize(("path", "payload"), [
    (PREVIEW, {"include_cancelled": "yes"}),
    (PREVIEW, {"include_cancelled": 1}),
    (PREVIEW, {"job_ids": ["x"]}),
    (CONFIRM, {}),
    (CONFIRM, {"preview_id": ""}),
    (CONFIRM, {"preview_id": 5}),
    (CONFIRM, {"preview_id": "x", "job_ids": ["a"]}),
    (CONFIRM, {"preview_id": "x", "count": 1}),
])
def test_validation_uses_existing_422_shape(
    client: TestClient, path: str, payload: dict[str, Any],
) -> None:
    response = client.post(path, json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_service_without_storage_fails_closed_and_keeps_preview(client: TestClient) -> None:
    seed(client, [make_job("completed", BASE)])
    service = service_of(client)
    bare = JobService(service._session_factory, manager_of(client), service._bus)

    async def run() -> tuple[str, str]:
        preview_id, _ = await bare.clear_history_preview(OWNER, include_cancelled=False)
        with pytest.raises(ClearHistoryError) as excinfo:
            await bare.clear_history_confirm(OWNER, preview_id)
        return excinfo.value.code, preview_id

    code, preview_id = call(client, run)
    assert code == "clear_history_failed"
    assert preview_id in bare._clear_previews
    assert len(jobs_in_db(client)) == 1


# --- deletion scope ---------------------------------------------------------


def test_single_and_children_keys_cascade_activity_and_library_untouched(
    client: TestClient, settings: Settings,
) -> None:
    single = make_job("failed", BASE)
    outside = Path(settings.state_dir) / "not-staging"
    outside.mkdir(parents=True)
    (outside / "keep.bin").write_bytes(b"keep")
    single.staging_path = str(outside)  # Never used as an identity.
    parent, kids = collection(BASE + timedelta(seconds=1), "completed",
                              [("completed", None), ("completed", None)])
    keeper = make_job("failed", BASE + timedelta(seconds=2), owner="other")
    seed(client, [single, parent, keeper], kids)
    for key in [single.id, keeper.id, *(k.id for k in kids)]:
        stage(settings, key)
    for job_id in (single.id, parent.id, keeper.id):
        add_event(client, job_id)
    media = Path(settings.library_dir) / "audio" / "Track.m4a"
    media.write_bytes(b"library media")

    async def library(session: AsyncSession) -> None:
        session.add(LibraryItem(id="l" * 32, owner_id="local", job_id=parent.id,
                                source_url="https://example.com/1", media_type="audio",
                                title="Track", created_at=BASE))
        await session.flush()
        session.add(MediaFile(library_item_id="l" * 32, storage_type="app_library",
                              path="audio/Track.m4a", size_bytes=13, created_at=BASE))
        await session.commit()

    in_session(client, library)

    def catalog(session: AsyncSession) -> Awaitable[Any]:
        async def read() -> Any:
            items = (await session.execute(select(LibraryItem.__table__))).all()
            files = (await session.execute(select(MediaFile.__table__))).all()
            return [tuple(r) for r in items], [tuple(r) for r in files]
        return read()

    before = in_session(client, catalog)
    assert confirmed(client, preview(client)["preview_id"]) == (2, 0)

    assert jobs_in_db(client) == {keeper.id: "failed"}
    assert items_of(client, parent.id) == []
    assert events_of(client, single.id) == events_of(client, parent.id) == []
    assert len(events_of(client, keeper.id)) == 1
    for key in [single.id, *(k.id for k in kids)]:
        assert not (Path(settings.staging_dir) / key).exists()
    assert (Path(settings.staging_dir) / keeper.id / "partial.bin").read_bytes() == b"partial"
    assert (outside / "keep.bin").read_bytes() == b"keep"
    assert media.read_bytes() == b"library media"
    assert in_session(client, catalog) == before
    assert client.get("/api/v1/library").status_code == 200

    for job_id in (single.id, parent.id):
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
        assert client.get(f"/api/v1/jobs/{job_id}/activity").status_code == 404
        assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 404
        assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 404
    history = client.get("/api/v1/jobs", params={"scope": "history"}).json()
    assert history["jobs"] == []
    assert client.get("/api/v1/jobs").json()["jobs"] == []


def test_missing_staging_is_idempotent_and_active_jobs_stay_listed(
    client: TestClient, settings: Settings,
) -> None:
    done, running = make_job("completed", BASE), make_job("downloading", BASE)
    seed(client, [done, running])
    stage(settings, running.id)
    assert confirmed(client, preview(client)["preview_id"]) == (1, 0)
    active = client.get("/api/v1/jobs", params={"scope": "active"}).json()
    assert [job["id"] for job in active["jobs"]] == [running.id]
    assert (Path(settings.staging_dir) / running.id).is_dir()


def test_successful_confirm_publishes_no_new_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(client, [make_job("completed", BASE)])
    published: list[dict[str, Any]] = []
    bus = app_of(client).state.event_bus
    monkeypatch.setattr(bus, "publish", published.append)
    assert confirmed(client, preview(client)["preview_id"]) == (1, 0)
    assert published == []


def test_activity_ids_stay_monotonic_after_deleting_the_highest(client: TestClient) -> None:
    keep, gone = make_job("queued", BASE), make_job("completed", BASE)
    seed(client, [keep, gone])
    add_event(client, keep.id)
    highest = [add_event(client, gone.id) for _ in range(3)][-1]
    assert confirmed(client, preview(client)["preview_id"]) == (1, 0)

    async def mark(session: AsyncSession) -> int:
        return await activity_repo.max_event_id(session)

    low = in_session(client, mark)
    assert low < highest
    fresh = add_event(client, keep.id)
    assert fresh > highest

    async def replay(session: AsyncSession) -> list[int]:
        return [e.id for e in await activity_repo.list_events_after(session, highest)]

    assert in_session(client, replay) == [fresh]


# --- per-job failures -------------------------------------------------------


def test_storage_error_keeps_job_with_fixed_warning_and_continues(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken, fine = make_job("failed", BASE), make_job("failed", BASE + timedelta(seconds=1))
    seed(client, [broken, fine])
    (Path(settings.staging_dir) / broken.id).write_bytes(b"not a directory")
    stage(settings, fine.id)
    published: list[dict[str, Any]] = []
    bus = app_of(client).state.event_bus
    monkeypatch.setattr(bus, "publish", published.append)

    response = confirm(client, preview(client)["preview_id"])
    assert response.json() == {"deleted_count": 1, "failed_count": 1}
    assert jobs_in_db(client) == {broken.id: "failed"}
    events = events_of(client, broken.id)
    assert events[-1][1:] == ("warning", CLEAR_HISTORY_WARNING_MESSAGE)
    for text in (response.text, *(e[2] for e in events)):
        assert str(settings.staging_dir) not in text and "Error" not in text
    assert [e["event"] for e in published] == ["activity.created"]
    assert published[0]["data"]["message"] == CLEAR_HISTORY_WARNING_MESSAGE
    assert (Path(settings.staging_dir) / broken.id).is_file()
    assert not (Path(settings.staging_dir) / fine.id).exists()


def test_activity_delete_failure_rolls_back_job_children_and_activity(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, kids = collection(BASE, "failed", [("failed", "network_error")],
                              error_code="all_children_failed")
    after = make_job("completed", BASE + timedelta(seconds=1))
    seed(client, [parent, after], kids)
    add_event(client, parent.id)
    stage(settings, kids[0].id)
    real = activity_repo.delete_events_for_job

    async def failing(session: AsyncSession, job_id: str) -> int:
        if job_id == parent.id:
            raise RuntimeError("database disk image is malformed")
        return await real(session, job_id)

    monkeypatch.setattr(activity_repo, "delete_events_for_job", failing)
    assert confirmed(client, preview(client)["preview_id"]) == (1, 1)
    assert jobs_in_db(client) == {parent.id: "failed"}
    assert items_of(client, parent.id) == [kids[0].id]
    events = events_of(client, parent.id)
    assert [e[2] for e in events] == ["Note", CLEAR_HISTORY_WARNING_MESSAGE]
    assert not (Path(settings.staging_dir) / kids[0].id).exists()  # Storage ran first.


def test_unexpected_rowcount_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = make_job("completed", BASE)
    seed(client, [record])
    add_event(client, record.id)
    real = job_repo.stage_clear_history_delete

    async def odd(*args: Any) -> int:
        assert await real(*args) == 1
        return 2

    monkeypatch.setattr(job_repo, "stage_clear_history_delete", odd)
    assert confirmed(client, preview(client)["preview_id"]) == (0, 1)
    assert jobs_in_db(client) == {record.id: "completed"}
    assert [e[2] for e in events_of(client, record.id)] == [
        "Note", CLEAR_HISTORY_WARNING_MESSAGE]


def _commit_hook(
    monkeypatch: pytest.MonkeyPatch, job_id: str,
    effect: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]],
) -> None:
    real = activity_repo.delete_events_for_job

    async def wrapped(session: AsyncSession, target: str) -> int:
        count = await real(session, target)
        if target == job_id:
            real_commit = session.commit

            async def commit() -> None:
                await effect(real_commit)

            session.commit = commit  # type: ignore[method-assign]
        return count

    monkeypatch.setattr(activity_repo, "delete_events_for_job", wrapped)


def test_commit_error_with_persisted_delete_counts_deleted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = make_job("completed", BASE)
    seed(client, [record])

    async def persisted(real_commit: Callable[[], Awaitable[None]]) -> None:
        await real_commit()
        raise RuntimeError("disk I/O error after commit")

    _commit_hook(monkeypatch, record.id, persisted)
    assert confirmed(client, preview(client)["preview_id"]) == (1, 0)
    assert jobs_in_db(client) == {}


def test_pre_commit_failure_keeps_everything_and_continues(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, kids = collection(BASE, "completed", [("completed", None)])
    after = make_job("completed", BASE + timedelta(seconds=1))
    seed(client, [record, after], kids)
    add_event(client, record.id)

    async def not_persisted(real_commit: Callable[[], Awaitable[None]]) -> None:
        raise RuntimeError("database is locked")

    _commit_hook(monkeypatch, record.id, not_persisted)
    assert confirmed(client, preview(client)["preview_id"]) == (1, 1)
    assert jobs_in_db(client) == {record.id: "completed"}
    assert items_of(client, record.id) == [kids[0].id]
    assert [e[2] for e in events_of(client, record.id)] == [
        "Note", CLEAR_HISTORY_WARNING_MESSAGE]


def test_unverifiable_commit_outcome_stops_with_masked_error(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = make_job("failed", BASE), make_job("failed", BASE + timedelta(seconds=1))
    seed(client, [first, second])
    add_event(client, first.id)
    stage(settings, second.id)
    service = service_of(client)
    real_factory = service._session_factory

    def unusable() -> Any:
        raise RuntimeError("unable to open database file /secret")

    async def lost(real_commit: Callable[[], Awaitable[None]]) -> None:
        monkeypatch.setattr(service, "_session_factory", unusable)
        raise RuntimeError("disk I/O error")

    _commit_hook(monkeypatch, first.id, lost)
    response = confirm(client, preview(client)["preview_id"])
    assert (response.status_code, response.json()) == (500, FAILED_BODY)
    monkeypatch.setattr(service, "_session_factory", real_factory)
    assert jobs_in_db(client) == {first.id: "failed", second.id: "failed"}
    assert [e[2] for e in events_of(client, first.id)] == ["Note"]  # No guessed Activity.
    assert (Path(settings.staging_dir) / second.id).is_dir()  # Next job never started.


def test_fresh_read_failure_is_systemic(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = make_job("failed", BASE)
    seed(client, [record])
    stage(settings, record.id)
    body = preview(client)

    async def broken(*args: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(job_repo, "get_clear_history_member", broken)
    response = confirm(client, body["preview_id"])
    assert (response.status_code, response.json()) == (500, FAILED_BODY)
    assert record.id in jobs_in_db(client)
    assert (Path(settings.staging_dir) / record.id).is_dir()


# --- lifecycle lock, cancellation, stop -------------------------------------


def test_retry_first_is_skipped_without_drain(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    retried, cleared = make_job("cancelled", BASE), make_job("completed", BASE)
    seed(client, [retried, cleared])
    body = preview(client, True)
    assert client.post(f"/api/v1/jobs/{retried.id}/retry").status_code == 200
    manager = manager_of(client)
    drained: list[str] = []
    real = manager.wait_for_attempt

    async def spy(job_id: str, *, retrieve_outcome: bool = False) -> None:
        drained.append(job_id)
        await real(job_id, retrieve_outcome=retrieve_outcome)

    monkeypatch.setattr(manager, "wait_for_attempt", spy)
    assert confirmed(client, body["preview_id"]) == (1, 0)
    assert drained == [cleared.id]
    assert jobs_in_db(client) == {retried.id: "queued"}


def test_clear_first_then_waiting_retry_and_cancel_get_not_found(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = make_job("failed", BASE)
    seed(client, [record])
    stage(settings, record.id)
    preview_id = preview(client)["preview_id"]
    entered, release = blocking_storage(client, monkeypatch, record.id)
    service, manager = service_of(client), manager_of(client)

    async def scenario() -> tuple[Any, Any, Any]:
        clear = asyncio.create_task(service.clear_history_confirm(OWNER, preview_id))
        assert await asyncio.to_thread(entered.wait, 10)
        retry = asyncio.create_task(service.retry_job(OWNER, record.id))
        cancel = asyncio.create_task(service.cancel_job(OWNER, record.id))
        for _ in range(5):
            await checkpoint()
        assert not retry.done() and not cancel.done()
        assert manager.lifecycle_lock(record.id).locked()
        release.set()
        result = await clear
        retried = await retry
        cancel_error = (await asyncio.gather(cancel, return_exceptions=True))[0]
        return result, retried, cancel_error

    result, retried, cancel_error = call(client, scenario)
    assert (result.deleted_count, result.failed_count) == (1, 0)
    assert retried is None
    assert isinstance(cancel_error, JobError)
    assert cancel_error.code == JobErrorCode.ANALYSIS_NOT_FOUND
    assert jobs_in_db(client) == {}


def test_repeated_request_cancellation_keeps_lock_until_outcome(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = make_job("failed", BASE), make_job("failed", BASE + timedelta(seconds=1))
    seed(client, [first, second])
    stage(settings, first.id)
    stage(settings, second.id)
    preview_id = preview(client)["preview_id"]
    entered, release = blocking_storage(client, monkeypatch, first.id)
    service, manager = service_of(client), manager_of(client)

    async def scenario() -> tuple[bool, bool]:
        clear = asyncio.create_task(service.clear_history_confirm(OWNER, preview_id))
        assert await asyncio.to_thread(entered.wait, 10)
        lock = manager.lifecycle_lock(first.id)
        for _ in range(2):
            clear.cancel()
            for _ in range(3):
                await checkpoint()
            assert lock.locked() and not clear.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await clear
        return lock.locked(), bool(manager._operations)

    locked, pending = call(client, scenario)
    assert not locked and not pending
    assert jobs_in_db(client) == {second.id: "failed"}  # Body finished; no next job.
    assert (Path(settings.staging_dir) / second.id).is_dir()


def test_stop_before_confirm_skips_everything(
    client: TestClient, settings: Settings,
) -> None:
    record = make_job("failed", BASE)
    seed(client, [record])
    stage(settings, record.id)
    preview_id = preview(client)["preview_id"]
    service, manager = service_of(client), manager_of(client)

    async def scenario() -> Any:
        await manager.stop()
        assert manager.stopping
        return await service.clear_history_confirm(OWNER, preview_id)

    result = call(client, scenario)
    assert (result.deleted_count, result.failed_count) == (0, 0)
    assert record.id in jobs_in_db(client)
    assert (Path(settings.staging_dir) / record.id).is_dir()


def test_stop_during_body_drains_it_and_starts_no_further_job(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = make_job("failed", BASE), make_job("failed", BASE + timedelta(seconds=1))
    seed(client, [first, second])
    stage(settings, first.id)
    stage(settings, second.id)
    preview_id = preview(client)["preview_id"]
    entered, release = blocking_storage(client, monkeypatch, first.id)
    service, manager = service_of(client), manager_of(client)

    async def scenario() -> Any:
        clear = asyncio.create_task(service.clear_history_confirm(OWNER, preview_id))
        assert await asyncio.to_thread(entered.wait, 10)
        stop = asyncio.create_task(manager.stop())
        for _ in range(5):
            await checkpoint()
        assert not stop.done() and not clear.done()
        release.set()
        result = await clear
        await stop
        return result

    result = call(client, scenario)
    assert (result.deleted_count, result.failed_count) == (1, 0)
    assert jobs_in_db(client) == {second.id: "failed"}
    assert (Path(settings.staging_dir) / second.id).is_dir()


# --- systemic SQLite write failures (review correction) ---------------------


def _sqlite_error(code: int) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("disk failure /secret/path")
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize(("code", "systemic"), [
    (8, True), (8 | (4 << 8), True),  # READONLY and READONLY_DBMOVED (extended).
    (10, True), (10 | (3 << 8), True), (11, True), (13, True), (14, True), (26, True),
    (5, False), (6, False), (19, False), (1, False),  # BUSY, LOCKED, CONSTRAINT, ERROR.
])
def test_systemic_classification_uses_driver_code(code: int, systemic: bool) -> None:
    wrapped = sa_exc.OperationalError("DELETE", {}, _sqlite_error(code))
    assert job_service_module._is_systemic_db_error(wrapped) is systemic
    assert job_service_module._is_systemic_db_error(_sqlite_error(code)) is systemic
    try:
        try:
            raise wrapped
        except sa_exc.OperationalError as inner:
            raise RuntimeError("rollback failed") from inner
    except RuntimeError as outer:
        assert job_service_module._is_systemic_db_error(outer) is systemic


def test_systemic_classification_never_matches_messages() -> None:
    assert not job_service_module._is_systemic_db_error(
        RuntimeError("attempt to write a readonly database"))
    assert not job_service_module._is_systemic_db_error(
        sa_exc.OperationalError("x", {}, sqlite3.OperationalError("database or disk is full")))


async def _driver(session: AsyncSession) -> Any:
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    return raw.driver_connection


def test_real_write_denial_on_delete_stops_without_guess_or_warning(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = make_job("failed", BASE), make_job("failed", BASE + timedelta(seconds=1))
    seed(client, [first, second])
    add_event(client, first.id)
    stage(settings, first.id)
    stage(settings, second.id)
    preview_id = preview(client)["preview_id"]
    reads: list[str] = []
    real_read = job_repo.get_clear_history_member

    async def counted_read(session: AsyncSession, job_id: str, owner_id: str) -> Any:
        fresh = await real_read(session, job_id, owner_id)
        reads.append(job_id)
        return fresh

    codes: list[int] = []
    real_delete = job_repo.stage_clear_history_delete

    async def denied(session: AsyncSession, *args: Any) -> int:
        driver = await _driver(session)
        await driver.execute("PRAGMA query_only = ON")  # Deterministic SQLITE_READONLY.
        try:
            return await real_delete(session, *args)
        except sa_exc.DBAPIError as exc:
            codes.append(exc.orig.sqlite_errorcode)  # type: ignore[union-attr]
            raise
        finally:
            await driver.execute("PRAGMA query_only = OFF")

    monkeypatch.setattr(job_repo, "get_clear_history_member", counted_read)
    monkeypatch.setattr(job_repo, "stage_clear_history_delete", denied)
    response = confirm(client, preview_id)
    assert (response.status_code, response.json()) == (500, FAILED_BODY)
    assert [code & 0xFF for code in codes] == [8]
    assert reads == [first.id, first.id]  # Both fresh reads succeeded; second never read.
    assert jobs_in_db(client) == {first.id: "failed", second.id: "failed"}
    assert [e[2] for e in events_of(client, first.id)] == ["Note"]  # No failure Activity.
    assert not (Path(settings.staging_dir) / first.id).exists()  # Storage ran first.
    assert (Path(settings.staging_dir) / second.id / "partial.bin").read_bytes() == b"partial"
    assert (Path(settings.staging_dir) / second.id / "sub" / "nested.bin").is_file()


def test_real_write_denial_on_warning_stops_the_run(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken, second = make_job("failed", BASE), make_job("failed", BASE + timedelta(seconds=1))
    seed(client, [broken, second])
    (Path(settings.staging_dir) / broken.id).write_bytes(b"not a directory")
    stage(settings, second.id)
    real_add = activity_repo.add_activity_event

    async def denied(session: AsyncSession, **kwargs: Any) -> Any:
        if kwargs.get("message") != CLEAR_HISTORY_WARNING_MESSAGE:
            return await real_add(session, **kwargs)
        driver = await _driver(session)
        await driver.execute("PRAGMA query_only = ON")
        try:
            return await real_add(session, **kwargs)
        finally:
            await driver.execute("PRAGMA query_only = OFF")

    monkeypatch.setattr(activity_repo, "add_activity_event", denied)
    response = confirm(client, preview(client)["preview_id"])
    assert (response.status_code, response.json()) == (500, FAILED_BODY)
    assert events_of(client, broken.id) == []
    assert jobs_in_db(client) == {broken.id: "failed", second.id: "failed"}
    assert (Path(settings.staging_dir) / second.id / "partial.bin").is_file()


def test_local_warning_failure_stays_best_effort(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken, second = make_job("failed", BASE), make_job("failed", BASE + timedelta(seconds=1))
    seed(client, [broken, second])
    (Path(settings.staging_dir) / broken.id).write_bytes(b"not a directory")
    stage(settings, second.id)
    real_add = activity_repo.add_activity_event

    async def local(session: AsyncSession, **kwargs: Any) -> Any:
        if kwargs.get("message") == CLEAR_HISTORY_WARNING_MESSAGE:
            raise sa_exc.OperationalError("INSERT", {}, _sqlite_error(5))  # BUSY: local.
        return await real_add(session, **kwargs)

    monkeypatch.setattr(activity_repo, "add_activity_event", local)
    assert confirmed(client, preview(client)["preview_id"]) == (1, 1)
    assert jobs_in_db(client) == {broken.id: "failed"}
    assert events_of(client, broken.id) == []


# --- barrier-controlled registered attempt tail (review correction) ---------


def _spies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[str]]:
    storage = service_of(client)._storage
    assert storage is not None
    removed: list[str] = []
    deleted: list[str] = []
    real_remove = storage.remove_staging_for_clear_history
    real_delete = job_repo.stage_clear_history_delete

    def remove(key: str) -> bool:
        removed.append(key)
        return real_remove(key)

    async def delete(session: AsyncSession, member: Any, owner_id: str) -> int:
        deleted.append(member.job_id)
        return await real_delete(session, member, owner_id)

    monkeypatch.setattr(storage, "remove_staging_for_clear_history", remove)
    monkeypatch.setattr(job_repo, "stage_clear_history_delete", delete)
    return removed, deleted


def _run_with_tail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, job_id: str, preview_id: str,
    removed: list[str], deleted: list[str],
    tail_write: Callable[[AsyncSession], Awaitable[None]],
) -> Any:
    service, manager = service_of(client), manager_of(client)
    sessions = app_of(client).state.sessionmaker
    real_wait = manager.wait_for_attempt

    async def scenario() -> Any:
        barrier, draining = asyncio.Event(), asyncio.Event()

        async def tail() -> None:  # The old attempt's publication tail.
            await barrier.wait()
            async with sessions() as session:
                await tail_write(session)
                await session.commit()

        manager._tasks[job_id] = asyncio.create_task(tail(), name=f"job-{job_id}")

        async def spy(target: str, *, retrieve_outcome: bool = False) -> None:
            draining.set()
            await real_wait(target, retrieve_outcome=retrieve_outcome)

        monkeypatch.setattr(manager, "wait_for_attempt", spy)
        clear = asyncio.create_task(service.clear_history_confirm(OWNER, preview_id))
        await draining.wait()
        for _ in range(10):
            await checkpoint()
        assert not clear.done()
        assert removed == [] and deleted == []  # Storage and DELETE wait for the tail.
        assert manager.lifecycle_lock(job_id).locked()
        barrier.set()
        return await clear

    return call(client, scenario)


def test_drain_waits_for_tail_and_tail_activity_is_cleared(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = make_job("completed", BASE)
    seed(client, [record])
    add_event(client, record.id)
    stage(settings, record.id)
    preview_id = preview(client)["preview_id"]
    removed, deleted = _spies(client, monkeypatch)
    written: list[int] = []

    async def last_activity(session: AsyncSession) -> None:
        event = await activity_repo.add_activity_event(
            session, job_id=record.id, level="info", event_type="job",
            message="Saved to App Library")
        written.append(event.id)

    result = _run_with_tail(client, monkeypatch, record.id, preview_id,
                            removed, deleted, last_activity)
    assert (result.deleted_count, result.failed_count) == (1, 0)
    assert len(written) == 1 and removed == [record.id] and deleted == [record.id]
    assert events_of(client, record.id) == []  # The tail's last Activity is gone too.
    assert jobs_in_db(client) == {}
    assert not (Path(settings.staging_dir) / record.id).exists()


def test_fingerprint_changed_during_drain_skips_without_storage_or_delete(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = make_job("failed", BASE)
    seed(client, [record])
    stage(settings, record.id)
    preview_id = preview(client)["preview_id"]
    removed, deleted = _spies(client, monkeypatch)

    async def change(session: AsyncSession) -> None:
        await session.execute(update(DownloadJob).where(DownloadJob.id == record.id)
                              .values(updated_at=BASE + timedelta(seconds=1)))
        await activity_repo.add_activity_event(
            session, job_id=record.id, level="error", event_type="failed",
            message="Late tail")

    result = _run_with_tail(client, monkeypatch, record.id, preview_id,
                            removed, deleted, change)
    assert (result.deleted_count, result.failed_count) == (0, 0)
    assert removed == [] and deleted == []
    assert jobs_in_db(client) == {record.id: "failed"}
    assert [e[2] for e in events_of(client, record.id)] == ["Late tail"]
    assert (Path(settings.staging_dir) / record.id / "partial.bin").is_file()


def test_module_constants_are_bounded() -> None:
    assert job_service_module.CLEAR_PREVIEW_TTL_SECONDS == 600.0
    assert 1 <= job_service_module.CLEAR_PREVIEW_CAP <= 64
