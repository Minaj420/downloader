import asyncio
import gc
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from weakref import ref

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.domain.actors import Actor
from app.domain.jobs import JobError, JobErrorCode, job_error_code_retryable
from app.integrations.ytdlp import DownloadProgress
from app.models.jobs import DownloadJob
from app.repositories import activity as activity_repo
from app.repositories import jobs as job_repo
from app.services.job_mapping import job_to_dto
from app.services.job_runner import _JobExecution
from tests.fake_binaries import fake_ytdlp_fail, fake_ytdlp_ok, fake_ytdlp_slow
from tests.test_jobs import (
    _activity_messages,
    _expire_analysis,
    create_analysis,
    create_job,
    wait_for_job,
)

# Redundant alias on purpose: pytest names collected fixtures by their
# module attribute, so this keeps the shared integration fixture
# registered in this module under its canonical ``app_factory`` name
# without its import binding shadowing the test parameters (F811).
from tests.test_jobs import app_factory as app_factory


def mutate(settings: Any, job_id: str, assignment: str) -> None:
    with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
        connection.execute(
            f"UPDATE download_jobs SET {assignment} WHERE id = ?", (job_id,)
        )
        connection.commit()


def completed_job(client: Any) -> dict:
    job = create_job(client, create_analysis(client)).json()
    final = wait_for_job(client, job["id"], statuses={"completed"})
    assert client.portal is not None
    client.portal.call(client.app.state.queue_manager.wait_for_attempt, job["id"])
    return final


def test_failed_retry_completes_same_record_with_stored_expired_analysis(
    app_factory: Any, settings: Any
) -> None:
    failing = fake_ytdlp_fail(app_factory.binaries, "Network error: connection reset")
    with app_factory(yt_dlp=failing) as client:
        analysis = create_analysis(client)
        job = create_job(client, analysis).json()
        failed = wait_for_job(client, job["id"], statuses={"failed"})
        assert failed["error"]["retryable"]
        _expire_analysis(settings, analysis["analysis_id"])
        # Replace the fake executable, not any persisted user selection.
        fake_ytdlp_ok(app_factory.binaries)
        response = client.post(f"/api/v1/jobs/{job['id']}/retry")
        assert response.status_code == 200
        queued = response.json()
        assert queued["status"] == "queued"
        assert queued["error"] is None
        assert queued["started_at"] is None
        assert queued["completed_at"] is None
        assert queued["final_path"] is None
        assert queued["progress"] == {
            "percent": 0.0,
            "downloaded_bytes": None,
            "total_bytes": None,
            "speed_bytes_per_second": None,
            "eta_seconds": None,
        }
        final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
        assert final["status"] == "completed", final
        for key in ("id", "source_url", "variant_id", "destination", "created_at"):
            assert final[key] == job[key]
        messages = _activity_messages(settings, job["id"])
        assert any(message.startswith("Failed:") for message in messages)
        assert messages.count("Retry queued") == 1


def test_restart_interrupted_retry_completes(app_factory: Any, settings: Any) -> None:
    binary = fake_ytdlp_fail(app_factory.binaries, "Network error: connection reset")
    with app_factory(yt_dlp=binary) as client:
        job = create_job(client, create_analysis(client)).json()
        wait_for_job(client, job["id"], statuses={"failed"})
    mutate(settings, job["id"], "status = 'downloading', progress_percent = 40")
    binary = fake_ytdlp_ok(app_factory.binaries)
    with app_factory(yt_dlp=binary) as client:
        failed = client.get(f"/api/v1/jobs/{job['id']}").json()
        assert failed["error"]["code"] == "interrupted_by_restart"
        response = client.post(f"/api/v1/jobs/{job['id']}/retry")
        assert response.status_code == 200
        assert response.json()["final_path"] is None
        final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
        assert final["status"] == "completed"


@pytest.mark.parametrize("foreign", [False, True])
def test_retry_not_found_owner_envelope(
    app_factory: Any, settings: Any, foreign: bool
) -> None:
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job_id = "unknown"
        if foreign:
            job_id = completed_job(client)["id"]
            mutate(settings, job_id, "owner_id = 'other', status = 'failed'")
        response = client.post(f"/api/v1/jobs/{job_id}/retry")
        assert response.status_code == 404
        assert response.json()["error"] == {
            "code": "job_not_found",
            "message": "This job does not exist.",
            "retryable": False,
            "job_id": None,
        }


def test_unknown_and_foreign_lifecycle_requests_do_not_grow_registry(
    app_factory: Any, settings: Any
) -> None:
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        manager = client.app.state.queue_manager
        assert client.portal is not None
        client.portal.call(manager.stop)
        foreign_ids = [f"foreign-{index}" for index in range(32)]
        with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
            connection.executemany(
                "INSERT INTO download_jobs (id, owner_id, analysis_id, source_url, title, "
                "variant_id, destination, conflict_policy, keep_app_copy, status, "
                "progress_percent, created_at, updated_at) "
                "SELECT ?, 'other', analysis_id, source_url, title, variant_id, destination, "
                "conflict_policy, keep_app_copy, status, progress_percent, created_at, "
                "updated_at FROM download_jobs WHERE id = ?",
                [(job_id, job["id"]) for job_id in foreign_ids],
            )
            connection.commit()

        async def registry_size() -> int:
            gc.collect()
            return len(manager._lifecycle_locks)

        baseline = client.portal.call(registry_size)
        for job_id in [f"unknown-{index}" for index in range(32)] + foreign_ids:
            for action, code in [("retry", "job_not_found"), ("cancel", "analysis_not_found")]:
                response = client.post(f"/api/v1/jobs/{job_id}/{action}")
                assert response.status_code == 404
                assert response.json()["error"]["code"] == code
            assert client.portal.call(registry_size) == baseline


@pytest.mark.parametrize("outcome", ["success", "exception", "cancellation"])
def test_lifecycle_lock_waiters_share_identity_and_reclaim(
    app_factory: Any, outcome: str
) -> None:
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        manager = client.app.state.queue_manager
        assert client.portal is not None
        client.portal.call(manager.stop)

        async def scenario() -> None:
            job_id = "lock-identity-probe"
            first = manager.lifecycle_lock(job_id)
            weak_lock = ref(first)
            await first.acquire()
            waiting = asyncio.Event()
            entered = asyncio.Event()
            release = asyncio.Event()

            async def waiter() -> None:
                lock = manager.lifecycle_lock(job_id)
                assert lock is weak_lock()
                assert lock is not None
                waiting.set()
                async with lock:
                    entered.set()
                    await release.wait()
                    if outcome == "exception":
                        raise RuntimeError("probe failed")

            task = asyncio.create_task(waiter())
            try:
                await asyncio.wait_for(waiting.wait(), timeout=5)
                assert not entered.is_set()
                first.release()
                del first
                # The scheduled waiter alone retains the original lock here.
                gc.collect()
                assert manager.lifecycle_lock(job_id) is weak_lock()
                await asyncio.wait_for(entered.wait(), timeout=5)
                assert manager.lifecycle_lock(job_id).locked()
                if outcome == "cancellation":
                    task.cancel()
                else:
                    release.set()
                if outcome == "exception":
                    with pytest.raises(RuntimeError, match="probe failed"):
                        await task
                elif outcome == "cancellation":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await task
                remaining = weak_lock()
                assert remaining is None or not remaining.locked()
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        client.portal.call(scenario)

        async def assert_reclaimed() -> None:
            gc.collect()
            assert "lock-identity-probe" not in manager._lifecycle_locks

        client.portal.call(assert_reclaimed)


@pytest.mark.parametrize("action", ["cancel", "retry"])
@pytest.mark.parametrize("change", ["owner_id = 'other'", "status = 'completed'"])
def test_lifecycle_waiter_revalidates_owner_and_state(
    app_factory: Any, settings: Any, action: str, change: str
) -> None:
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        manager = client.app.state.queue_manager
        service = client.app.state.job_service
        assert client.portal is not None
        client.portal.call(manager.stop)
        mutate(settings, job["id"], "status = 'failed', error_code = 'network_error'"
               if action == "retry" else "status = 'queued'")

        async def scenario() -> None:
            lock = manager.lifecycle_lock(job["id"])
            old_event = manager._cancel_events.get(job["id"])
            await lock.acquire()
            operation = service.retry_job if action == "retry" else service.cancel_job
            request = asyncio.create_task(operation(Actor(), job["id"]))
            try:
                await asyncio.sleep(0)
                assert not request.done()
                assert manager.lifecycle_lock(job["id"]) is lock
                mutate(settings, job["id"], change)
                lock.release()
                if action == "retry" and "owner_id" in change:
                    assert await request is None
                else:
                    expected = (JobErrorCode.ANALYSIS_NOT_FOUND if "owner_id" in change
                                else JobErrorCode.INVALID_STATE)
                    with pytest.raises(JobError) as caught:
                        await request
                    assert caught.value.code == expected
                assert manager._cancel_events.get(job["id"]) is old_event
            finally:
                if lock.locked():
                    lock.release()
                await asyncio.gather(request, return_exceptions=True)

        client.portal.call(scenario)
        assert "Retry queued" not in _activity_messages(settings, job["id"])


@pytest.mark.parametrize(
    ("status", "code"),
    [(s, "network_error") for s in (
        "queued", "analyzing", "downloading", "post_processing", "finalizing",
        "completed",
    )] + [("failed", c) for c in (
        "unsupported_url", "disk_write_failed", "unknown", None, "cancelled",
    )],
)
def test_retry_invalid_states(
    app_factory: Any, settings: Any, status: str, code: str | None
) -> None:
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        # Stop claiming while testing otherwise transient queued states.
        assert client.portal is not None
        client.portal.call(client.app.state.queue_manager.stop)
        with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
            connection.execute(
                "UPDATE download_jobs SET status = ?, error_code = ? WHERE id = ?",
                (status, code, job["id"]),
            )
            connection.commit()
        response = client.post(f"/api/v1/jobs/{job['id']}/retry")
        assert response.status_code == 409
        # Error envelope is unchanged by the cancelled single Retry decision.
        assert response.json()["error"] == {
            "code": "invalid_state",
            "message": "This job has already finished.",
            "retryable": False,
            "job_id": None,
        }


@pytest.mark.parametrize("terminal", [
    "status = 'failed', error_code = 'network_error'",
    "status = 'cancelled', error_code = 'cancelled'",
])
def test_concurrent_retry_one_committed_event(
    app_factory: Any, settings: Any, terminal: str
) -> None:
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        assert client.portal is not None
        client.portal.call(client.app.state.queue_manager.stop)
        mutate(settings, job["id"], terminal)
        bus = client.app.state.event_bus
        subscriber = bus.subscribe()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(client.post, f"/api/v1/jobs/{job['id']}/retry")
                           for _ in range(2)]
                assert sorted(f.result().status_code for f in futures) == [200, 409]
            events = []
            while not subscriber.empty():
                events.append(subscriber.get_nowait())
            assert [event["event"] for event in events] == [
                "activity.created", "job.updated"
            ]
            assert events[0]["data"]["message"] == "Retry queued"
            assert events[1]["data"]["status"] == "queued"
        finally:
            bus.unsubscribe(subscriber)
        assert _activity_messages(settings, job["id"]).count("Retry queued") == 1


def test_retry_activity_rollback_no_notify_or_emission(
    app_factory: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        mutate(settings, job["id"], "status = 'failed', error_code = 'network_error'")
        notifications = []
        monkeypatch.setattr(client.app.state.queue_manager, "notify",
                            lambda: notifications.append(True))

        async def fail_activity(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("activity failed")

        monkeypatch.setattr(activity_repo, "add_activity_event", fail_activity)
        bus = client.app.state.event_bus
        subscriber = bus.subscribe()
        try:
            with pytest.raises(RuntimeError, match="activity failed"):
                client.post(f"/api/v1/jobs/{job['id']}/retry")
            assert subscriber.empty()
            assert notifications == []
            current = client.get(f"/api/v1/jobs/{job['id']}").json()
            assert current["status"] == "failed"
            assert current["final_path"] == job["final_path"]
            assert "Retry queued" not in _activity_messages(settings, job["id"])
        finally:
            bus.unsubscribe(subscriber)


@pytest.mark.parametrize("change", ["owner_id = 'other'", "status = 'completed'",
                                    "error_code = 'unsupported_url'"])
def test_retry_write_revalidates_stale_snapshot(
    app_factory: Any, settings: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    real_stage = job_repo.stage_retry_job
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        mutate(settings, job["id"], "status = 'failed', error_code = 'network_error'")

        async def stale_write(session: Any, job_id: str, owner_id: str) -> bool:
            mutate(settings, job_id, change)
            return await real_stage(session, job_id, owner_id)

        monkeypatch.setattr(job_repo, "stage_retry_job", stale_write)
        response = client.post(f"/api/v1/jobs/{job['id']}/retry")
        assert response.status_code == (404 if "owner_id" in change else 409)
        assert "Retry queued" not in _activity_messages(settings, job["id"])


def test_retry_waits_old_unwind_and_progress_and_resets_cancel_runtime(
    app_factory: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed_activity = threading.Event()
    release_activity = threading.Event()
    progress_started = threading.Event()
    release_progress = threading.Event()
    drain_started = threading.Event()
    executions = []
    retry_waiting = threading.Event()
    real_execute = _JobExecution._execute
    real_activity = _JobExecution._activity
    real_progress = _JobExecution._update_progress
    real_drain = _JobExecution.drain_helpers

    async def execute(execution: Any, record: Any) -> None:
        executions.append(execution)
        if len(executions) == 1:
            callback = execution._progress_forwarder(0, 1)
            execution.saved_callback = callback
            callback(DownloadProgress(99, 100, None, None, None))
            raise JobError(JobErrorCode.NETWORK_ERROR)
        await real_execute(execution, record)

    async def activity(execution: Any, message: str, **kwargs: Any) -> None:
        if message.startswith("Failed:"):
            failed_activity.set()
            while not release_activity.is_set():
                await asyncio.sleep(0.01)
        await real_activity(execution, message, **kwargs)

    async def progress(execution: Any, **kwargs: Any) -> None:
        if kwargs.get("downloaded_bytes") == 99:
            progress_started.set()
            while not release_progress.is_set():
                await asyncio.sleep(0.01)
        await real_progress(execution, **kwargs)

    async def drain(execution: Any) -> None:
        if execution is executions[0]:
            drain_started.set()
        await real_drain(execution)

    monkeypatch.setattr(_JobExecution, "_execute", execute)
    monkeypatch.setattr(_JobExecution, "_activity", activity)
    monkeypatch.setattr(_JobExecution, "_update_progress", progress)
    monkeypatch.setattr(_JobExecution, "drain_helpers", drain)
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = create_job(client, create_analysis(client)).json()
        assert failed_activity.wait(5)
        assert progress_started.wait(5)
        manager = client.app.state.queue_manager
        real_wait = manager.wait_for_attempt

        async def wait_attempt(job_id: str) -> None:
            retry_waiting.set()
            await real_wait(job_id)

        monkeypatch.setattr(manager, "wait_for_attempt", wait_attempt)
        old_event = manager.cancel_event_for(job["id"])
        old_event.set()
        old_gate = manager._cancel_gates[job["id"]]
        with ThreadPoolExecutor(max_workers=1) as pool:
            retry = pool.submit(client.post, f"/api/v1/jobs/{job['id']}/retry")
            try:
                assert retry_waiting.wait(5)
                assert not retry.done()
                release_activity.set()
                # Old progress is still pending after the failure publication.
                assert drain_started.wait(5)
                assert not retry.done()
                assert len(executions) == 1
            finally:
                release_activity.set()
                release_progress.set()
            response = retry.result(timeout=5)
        assert response.status_code == 200
        final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
        assert final["status"] == "completed", final
        assert len(executions) == 2
        assert executions[1]._cancel_event is not old_event
        assert not executions[1]._cancel_event.is_set()
        assert executions[1]._cancel_gate is not old_gate
        assert executions[0]._closed
        assert all(task.done() for task in executions[0]._helpers)
        assert client.portal is not None
        client.portal.call(executions[0].saved_callback,
                           DownloadProgress(99, 100, None, None, None))
        assert client.get(f"/api/v1/jobs/{job['id']}").json()["progress"]["percent"] == 100
        messages = _activity_messages(settings, job["id"])
        assert next(i for i, m in enumerate(messages) if m.startswith("Failed:")) < (
            messages.index("Retry queued")
        )


def test_claim_cannot_capture_old_cancel_event_during_retry_commit(
    app_factory: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_stage = job_repo.stage_retry_job
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        mutate(settings, job["id"], "status = 'failed', error_code = 'network_error'")
        manager = client.app.state.queue_manager
        manager.cancel_event_for(job["id"]).set()
        checked = []

        async def stage(session: Any, job_id: str, owner_id: str) -> bool:
            won = await real_stage(session, job_id, owner_id)
            real_commit = session.commit

            async def commit() -> None:
                await real_commit()
                # Force the queue to see queued before service commit returns.
                await manager._claim_and_run()
                assert job_id not in manager._tasks
                checked.append(True)

            session.commit = commit
            return won

        monkeypatch.setattr(job_repo, "stage_retry_job", stage)
        response = client.post(f"/api/v1/jobs/{job['id']}/retry")
        assert response.status_code == 200
        assert checked == [True]
        final = wait_for_job(client, job["id"], statuses={"completed", "failed", "cancelled"})
        assert final["status"] == "completed", final


def test_inflight_old_cancel_finishes_before_retry_runtime_reset(
    app_factory: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel_entered = threading.Event()
    release_cancel = threading.Event()
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        manager = client.app.state.queue_manager
        mutate(settings, job["id"], "status = 'downloading'")
        old_event = manager.cancel_event_for(job["id"])
        real_cancel = manager.request_cancel

        async def cancel(job_id: str) -> None:
            cancel_entered.set()
            while not release_cancel.is_set():
                await asyncio.sleep(0.01)
            await real_cancel(job_id)

        monkeypatch.setattr(manager, "request_cancel", cancel)
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel_request = pool.submit(client.post, f"/api/v1/jobs/{job['id']}/cancel")
            try:
                assert cancel_entered.wait(5)
                mutate(settings, job["id"], "status = 'failed', error_code = 'network_error'")
                retry_request = pool.submit(client.post, f"/api/v1/jobs/{job['id']}/retry")
                assert not retry_request.done()
            finally:
                release_cancel.set()
            assert cancel_request.result(timeout=5).status_code == 200
            assert retry_request.result(timeout=5).status_code == 200
        assert old_event.is_set()
        assert manager.cancel_event_for(job["id"]) is not old_event
        final = wait_for_job(client, job["id"], statuses={"completed", "failed", "cancelled"})
        assert final["status"] == "completed", final


@pytest.mark.parametrize("phase", ["post_commit", "commit_return", "session_close"])
def test_retry_finalizes_before_repeated_request_cancellation_releases_lock(
    app_factory: Any, settings: Any, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        manager = client.app.state.queue_manager
        assert client.portal is not None
        client.portal.call(manager.stop)
        mutate(settings, job["id"], "status = 'failed', error_code = 'network_error'")
        bus = client.app.state.event_bus
        subscriber = bus.subscribe()
        order = []
        real_reset = manager.reset_attempt
        real_publish = bus.publish
        real_stage = job_repo.stage_retry_job

        def reset(job_id: str) -> None:
            order.append("reset")
            real_reset(job_id)

        def publish(event: Any) -> None:
            order.append(event["event"])
            real_publish(event)

        monkeypatch.setattr(manager, "reset_attempt", reset)
        monkeypatch.setattr(bus, "publish", publish)
        monkeypatch.setattr(manager, "notify", lambda: order.append("notify"))

        async def scenario() -> None:
            entered = asyncio.Event()
            release = asyncio.Event()
            waiter_started = asyncio.Event()
            acquired = asyncio.Event()
            old_event = manager.cancel_event_for(job["id"])
            old_event.set()
            old_gate = threading.Lock()
            manager._cancel_gates[job["id"]] = old_gate
            lock = manager.lifecycle_lock(job["id"])

            async def stage(session: Any, job_id: str, owner_id: str) -> bool:
                won = await real_stage(session, job_id, owner_id)
                real_commit = session.commit
                real_close = session.close

                async def commit() -> None:
                    await real_commit()
                    if phase == "post_commit":
                        entered.set()
                        await release.wait()
                    elif phase == "commit_return":
                        # Cancel as the real commit await returns, before the
                        # request can receive the operation's result.
                        request.cancel()

                async def close() -> None:
                    if phase != "post_commit":
                        entered.set()
                        await release.wait()
                    await real_close()

                session.commit = commit
                session.close = close
                return won

            async def competing_lifecycle_operation() -> None:
                waiter_started.set()
                async with lock:
                    acquired.set()
                    assert order == [
                        "reset", "activity.created", "job.updated", "notify"
                    ]

            monkeypatch.setattr(job_repo, "stage_retry_job", stage)
            request = asyncio.create_task(
                client.app.state.job_service.retry_job(Actor(), job["id"])
            )
            waiter = None
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                # The queued state is already durable at all three barriers.
                current = await client.app.state.job_service.get_job(Actor(), job["id"])
                assert current is not None and current.status == "queued"
                waiter = asyncio.create_task(competing_lifecycle_operation())
                await waiter_started.wait()
                for _ in range(3):
                    request.cancel()
                    # Let the request consume each cancellation separately.
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    assert not request.done()
                    assert lock.locked()
                    assert not acquired.is_set()
                    assert manager.cancel_event_for(job["id"]) is old_event
                    assert manager._cancel_gates[job["id"]] is old_gate
                    assert subscriber.empty()
                    assert order == []
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await request
                await asyncio.wait_for(waiter, timeout=5)
                assert not lock.locked()
                assert old_event.is_set()
                fresh_event = manager.cancel_event_for(job["id"])
                assert fresh_event is not old_event and not fresh_event.is_set()
                assert job["id"] not in manager._cancel_gates
                # Collect the committed retry pair at this barrier, before
                # the new attempt below can publish: the subscriber drains
                # buffered activities before transient snapshots, so a
                # post-run drain would interleave the new runner's
                # activities ahead of the queued snapshot.
                retry_events = []
                while not subscriber.empty():
                    retry_events.append(subscriber.get_nowait())
                assert [event["event"] for event in retry_events] == [
                    "activity.created", "job.updated"
                ]
                assert retry_events[0]["data"]["job_id"] == job["id"]
                assert retry_events[0]["data"]["message"] == "Retry queued"
                assert retry_events[1]["data"]["id"] == job["id"]
                assert retry_events[1]["data"]["status"] == "queued"
                # Start a real new attempt and verify its gate is also fresh.
                checked = []
                real_execute = _JobExecution._execute

                async def execute(execution: Any, record: Any) -> None:
                    assert execution._cancel_event is fresh_event
                    assert not execution._cancel_event.is_set()
                    assert execution._cancel_gate is not old_gate
                    checked.append(True)
                    await real_execute(execution, record)

                monkeypatch.setattr(_JobExecution, "_execute", execute)
                manager._stop_event.clear()
                await manager._claim_and_run()
                await manager.wait_for_attempt(job["id"])
                assert checked == [True]
                final = await client.app.state.job_service.get_job(Actor(), job["id"])
                assert final is not None and final.status == "completed"
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)
                if waiter is not None:
                    await asyncio.gather(waiter, return_exceptions=True)

        try:
            client.portal.call(scenario)
            assert _activity_messages(settings, job["id"]).count("Retry queued") == 1
        finally:
            bus.unsubscribe(subscriber)


def test_retry_real_commit_failure_rolls_back_without_runtime_finalization(
    app_factory: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        manager = client.app.state.queue_manager
        assert client.portal is not None
        client.portal.call(manager.stop)
        mutate(settings, job["id"], "status = 'failed', error_code = 'network_error'")
        before = client.get(f"/api/v1/jobs/{job['id']}").json()
        # A deferred FK violation fails SQLite COMMIT itself, not staging or
        # an artificial replacement of session.commit.
        with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
            connection.execute(
                "CREATE TABLE retry_commit_probe (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES retry_commit_probe(id) "
                "DEFERRABLE INITIALLY DEFERRED)"
            )
        real_stage = job_repo.stage_retry_job

        async def stage(session: Any, job_id: str, owner_id: str) -> bool:
            won = await real_stage(session, job_id, owner_id)
            await session.execute(text(
                "INSERT INTO retry_commit_probe (id, parent_id) VALUES (1, 2)"
            ))
            return won

        old_event = manager.cancel_event_for(job["id"])
        old_event.set()
        old_gate = threading.Lock()
        manager._cancel_gates[job["id"]] = old_gate
        calls = []
        monkeypatch.setattr(job_repo, "stage_retry_job", stage)
        monkeypatch.setattr(manager, "reset_attempt", lambda _: calls.append("reset"))
        monkeypatch.setattr(manager, "notify", lambda: calls.append("notify"))
        subscriber = client.app.state.event_bus.subscribe()
        try:
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                client.post(f"/api/v1/jobs/{job['id']}/retry")
            assert calls == []
            assert subscriber.empty()
            assert manager.cancel_event_for(job["id"]) is old_event
            assert old_event.is_set()
            assert manager._cancel_gates[job["id"]] is old_gate
            assert not manager.lifecycle_lock(job["id"]).locked()
            assert client.get(f"/api/v1/jobs/{job['id']}").json() == before
            assert "Retry queued" not in _activity_messages(settings, job["id"])
            with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
                assert connection.execute("SELECT * FROM retry_commit_probe").fetchall() == []
        finally:
            client.app.state.event_bus.unsubscribe(subscriber)


# --- Q2: explicit cancelled single Retry (PRODUCT_SPEC §17, ARCHITECTURE §20) -----

_YTDLP_HEAD = r"""#!/bin/sh
out=""
next=0
for a in "$@"; do
  if [ "$next" = "1" ]; then out="$a"; next=0; fi
  [ "$a" = "-o" ] && next=1
done
out=$(printf '%s' "$out" | sed 's/%(ext)s/mp4/g')
"""

# Leaves a source-style ``.part`` partial and then hangs until cancelled.
_YTDLP_PARTIAL = _YTDLP_HEAD + r"""printf 'partial' > "$out.part"
printf 'PROG 1 100 NA 500000 99\n'
exec sleep 30
"""

# Continues a valid partial when present (like yt-dlp's default continue),
# otherwise starts from scratch; records which path ran.
_YTDLP_RESUME = _YTDLP_HEAD + r"""if [ -f "$out.part" ]; then
  printf 'resumed\n' >> 'LOG'
  cat "$out.part" > "$out"
  printf ' rest' >> "$out"
  rm -f "$out.part"
else
  printf 'fresh\n' >> 'LOG'
  printf 'fresh media' > "$out"
fi
printf '%s\n' "$out"
"""


def swap_ytdlp(binaries: Path, body: str) -> str:
    """Atomically replace the fake binary; a running script keeps its old inode."""
    staged = binaries / "yt-dlp.next"
    staged.write_text(body)
    staged.chmod(0o755)
    target = binaries / "yt-dlp"
    os.replace(staged, target)
    return str(target)


def swap_in_fast_ytdlp(binaries: Path) -> str:
    """Atomically install the existing ``fake_ytdlp_ok`` (writes file, then path)."""
    staged_dir = binaries / "next"
    staged_dir.mkdir(exist_ok=True)
    target = binaries / "yt-dlp"
    os.replace(fake_ytdlp_ok(staged_dir), target)
    return str(target)


def wait_path(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), path


def library_files(settings: Any) -> list[Path]:
    return [path for path in settings.library_dir.rglob("*") if path.is_file()]


def test_cancelled_single_retry_resumes_retained_partial_and_cleans_on_success(
    app_factory: Any, settings: Any, tmp_path: Path
) -> None:
    log = tmp_path / "ytdlp.log"
    binaries = app_factory.binaries
    with app_factory(yt_dlp=swap_ytdlp(binaries, _YTDLP_PARTIAL)) as client:
        manager = client.app.state.queue_manager
        assert client.portal is not None
        job = create_job(client, create_analysis(client)).json()
        staging = settings.staging_dir / job["id"]
        partial = staging / "media.mp4.part"
        wait_path(partial)
        assert client.post(f"/api/v1/jobs/{job['id']}/cancel").status_code == 200
        cancelled = wait_for_job(client, job["id"], statuses={"cancelled"})
        # Same error body shape (message stays the runner's existing text);
        # only the backend retry decision changed.
        assert set(cancelled["error"]) == {"code", "message", "retryable"}
        assert (cancelled["error"]["code"], cancelled["error"]["retryable"]) == (
            "cancelled", True)
        client.portal.call(manager.wait_for_attempt, job["id"])
        assert partial.read_text() == "partial"
        assert library_files(settings) == []

        swap_ytdlp(binaries, _YTDLP_RESUME.replace("LOG", str(log)))
        response = client.post(f"/api/v1/jobs/{job['id']}/retry")
        assert response.status_code == 200
        assert (response.json()["status"], response.json()["error"]) == ("queued", None)
        final = wait_for_job(client, job["id"], statuses={"completed", "failed", "cancelled"})
        assert final["status"] == "completed", final
        client.portal.call(manager.wait_for_attempt, job["id"])
        assert (settings.library_dir / "video" / "Test Video.mp4").read_text() == "partial rest"
        assert not staging.exists()
    assert log.read_text().splitlines() == ["resumed"]
    messages = _activity_messages(settings, job["id"])
    assert messages.index("Cancelled") < messages.index("Retry queued")
    assert messages.count("Retry queued") == 1


@pytest.mark.parametrize("code", ["cancelled", None])
def test_old_cancelled_single_without_staging_retries_from_scratch(
    app_factory: Any, settings: Any, tmp_path: Path, code: str | None
) -> None:
    log = tmp_path / "ytdlp.log"
    binaries = app_factory.binaries
    with app_factory(yt_dlp=fake_ytdlp_ok(binaries)) as client:
        job = completed_job(client)
        assert client.portal is not None
        client.portal.call(client.app.state.queue_manager.stop)
    # A cancelled row from before Q2: its staging was already removed.
    with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
        connection.execute(
            "UPDATE download_jobs SET status = 'cancelled', error_code = ?, "
            "error_message = NULL, final_path = NULL WHERE id = ?", (code, job["id"]))
        connection.execute("DELETE FROM media_files")
        connection.execute("DELETE FROM library_items")
        connection.commit()
    for path in library_files(settings):
        path.unlink()
    assert not (settings.staging_dir / job["id"]).exists()
    with app_factory(yt_dlp=swap_ytdlp(binaries, _YTDLP_RESUME.replace("LOG", str(log)))) as client:
        # Before Retry, detail and history expose the existing body shape.
        detail = client.get(f"/api/v1/jobs/{job['id']}").json()
        [history] = [entry for entry in client.get("/api/v1/jobs?scope=history").json()["jobs"]
                     if entry["id"] == job["id"]]
        for snapshot in (detail, history):
            assert snapshot["status"] == "cancelled"
            assert set(snapshot["error"]) == {"code", "message", "retryable"}
            assert (snapshot["error"]["code"], snapshot["error"]["retryable"]) == (
                "cancelled", True)
            if code is None:
                assert snapshot["error"]["message"] == "The download was cancelled."
        response = client.post(f"/api/v1/jobs/{job['id']}/retry")
        assert response.status_code == 200
        final = wait_for_job(client, job["id"], statuses={"completed", "failed", "cancelled"})
        assert final["status"] == "completed", final
        assert client.portal is not None
        client.portal.call(client.app.state.queue_manager.wait_for_attempt, job["id"])
        assert (settings.library_dir / "video" / "Test Video.mp4").read_text() == "fresh media"
        assert not (settings.staging_dir / job["id"]).exists()
    assert log.read_text().splitlines() == ["fresh"]


def test_queued_cancel_retry_does_not_carry_stale_cancel_signal(
    app_factory: Any, settings: Any
) -> None:
    binaries = app_factory.binaries
    with app_factory(yt_dlp=fake_ytdlp_slow(binaries), concurrency=1) as client:
        manager = client.app.state.queue_manager
        analysis = create_analysis(client)
        first = create_job(client, analysis).json()
        wait_for_job(client, first["id"], statuses={"downloading"})
        # Progress proves the first process already runs its slow script.
        deadline = time.monotonic() + 5
        while client.get(f"/api/v1/jobs/{first['id']}").json()["progress"][
                "downloaded_bytes"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        swap_in_fast_ytdlp(binaries)
        second = create_job(client, analysis).json()
        assert client.post(f"/api/v1/jobs/{second['id']}/cancel").status_code == 200
        cancelled = wait_for_job(client, second["id"], statuses={"cancelled"})
        assert cancelled["error"]["retryable"] is True
        # The queued Cancel signalled an event no attempt ever consumed.
        assert manager._cancel_events[second["id"]].is_set()

        assert client.post(f"/api/v1/jobs/{second['id']}/retry").status_code == 200
        stale = manager._cancel_events.get(second["id"])
        assert stale is None or not stale.is_set()

        response = client.post(f"/api/v1/jobs/{first['id']}/cancel")
        assert response.status_code == 200, (
            response.json(), client.get(f"/api/v1/jobs/{first['id']}").json())
        wait_for_job(client, first["id"], statuses={"cancelled"})
        final = wait_for_job(client, second["id"], statuses={"completed", "failed", "cancelled"})
        assert final["status"] == "completed", final
    messages = _activity_messages(settings, second["id"])
    assert messages[:3] == ["Queued", "Cancelled", "Retry queued"]
    assert "Download started" in messages[3:]


def test_cancelled_single_retry_waits_for_old_attempt_drain(
    app_factory: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled_activity = threading.Event()
    release = threading.Event()
    retry_waiting = threading.Event()
    executions: list[Any] = []
    real_activity = _JobExecution._activity
    real_execute = _JobExecution._execute

    async def execute(execution: Any, record: Any) -> None:
        executions.append(execution)
        await real_execute(execution, record)

    async def activity(execution: Any, message: str, **kwargs: Any) -> None:
        if message == "Cancelled" and execution is executions[0]:
            cancelled_activity.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        await real_activity(execution, message, **kwargs)

    monkeypatch.setattr(_JobExecution, "_execute", execute)
    monkeypatch.setattr(_JobExecution, "_activity", activity)
    binaries = app_factory.binaries
    with app_factory(yt_dlp=fake_ytdlp_slow(binaries)) as client:
        manager = client.app.state.queue_manager
        job = create_job(client, create_analysis(client)).json()
        wait_for_job(client, job["id"], statuses={"downloading"})
        old_event = manager.cancel_event_for(job["id"])
        assert client.post(f"/api/v1/jobs/{job['id']}/cancel").status_code == 200
        assert cancelled_activity.wait(5)
        swap_in_fast_ytdlp(binaries)
        real_wait = manager.wait_for_attempt

        async def wait_attempt(job_id: str, **kwargs: Any) -> None:
            retry_waiting.set()
            await real_wait(job_id, **kwargs)

        monkeypatch.setattr(manager, "wait_for_attempt", wait_attempt)
        with ThreadPoolExecutor(max_workers=1) as pool:
            retry = pool.submit(client.post, f"/api/v1/jobs/{job['id']}/retry")
            try:
                assert retry_waiting.wait(5)
                time.sleep(0.1)
                assert not retry.done()
                assert len(executions) == 1
            finally:
                release.set()
            response = retry.result(timeout=5)
        assert response.status_code == 200
        final = wait_for_job(client, job["id"], statuses={"completed", "failed"})
        assert final["status"] == "completed", final
        assert len(executions) == 2
        assert old_event.is_set()
        assert executions[1]._cancel_event is not old_event
        assert not executions[1]._cancel_event.is_set()
    messages = _activity_messages(settings, job["id"])
    assert messages.index("Cancelled") < messages.index("Retry queued")


def test_cancelled_retry_cas_is_single_only(app_factory: Any, settings: Any) -> None:
    with app_factory(yt_dlp=fake_ytdlp_ok(app_factory.binaries)) as client:
        job = completed_job(client)
        assert client.portal is not None
        client.portal.call(client.app.state.queue_manager.stop)
        mutate(settings, job["id"],
               "job_type = 'collection', status = 'cancelled', error_code = 'cancelled'")
        sessions = client.app.state.job_service._session_factory

        async def stage() -> bool:
            async with sessions() as session:
                won = await job_repo.stage_retry_job(session, job["id"], "local")
                await session.rollback()
                return won

        assert client.portal.call(stage) is False
        mutate(settings, job["id"], "job_type = 'single'")
        assert client.portal.call(stage) is True


def _dto_record(job_type: str, status: str, code: str | None) -> DownloadJob:
    now = datetime.now(UTC)
    return DownloadJob(
        id="j" * 32, owner_id="local", analysis_id="a" * 32, source_url="https://x.test",
        title="t", variant_id="", destination="app_library", conflict_policy="skip",
        keep_app_copy=False, status=status, error_code=code, error_message="m",
        progress_percent=0.0, job_type=job_type,
        collection_mode="audio" if job_type == "collection" else None,
        total_items=1, completed_items=0, failed_items=0, created_at=now, updated_at=now,
    )


@pytest.mark.parametrize(("job_type", "status", "code", "children", "expected"), [
    ("single", "cancelled", "cancelled", None, True),
    ("single", "failed", "cancelled", None, False),
    ("single", "failed", "network_error", None, True),
    ("collection", "cancelled", "cancelled", [("completed", None)], False),
    ("collection", "cancelled", "cancelled", [("cancelled", "cancelled")], True),
    ("collection", "failed", "partial_failure", [("failed", "cancelled")], False),
    ("collection", "failed", "partial_failure", [("failed", "network_error")], True),
])
def test_retryable_dto_single_status_aware_collection_unchanged(
    job_type: str, status: str, code: str, children: Any, expected: bool
) -> None:
    record = _dto_record(job_type, status, code)
    if children is not None:
        record._collection_retry_children = children  # type: ignore[attr-defined]
    error = job_to_dto(record).error
    assert error is not None
    assert (error.code, error.retryable) == (code, expected)
    assert job_error_code_retryable("cancelled") is False


def test_null_code_cancelled_single_dto_is_retryable_and_collection_unchanged() -> None:
    single = _dto_record("single", "cancelled", None)
    single.error_message = None
    error = job_to_dto(single).error
    assert error is not None
    assert error.model_dump() == {
        "code": "cancelled", "message": "The download was cancelled.", "retryable": True,
    }
    kept = _dto_record("single", "cancelled", None)
    assert job_to_dto(kept).error.model_dump() == {  # type: ignore[union-attr]
        "code": "cancelled", "message": "m", "retryable": True,
    }
    # Collection snapshots and other NULL-code rows keep their existing body.
    collection = _dto_record("collection", "cancelled", None)
    collection._collection_retry_children = [("cancelled", "cancelled")]  # type: ignore[attr-defined]
    assert job_to_dto(collection).error is None
    assert job_to_dto(_dto_record("single", "failed", None)).error is None
    assert job_to_dto(_dto_record("single", "queued", None)).error is None
