import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.repositories import activity, jobs
from tests.conftest import FakeAnalyzer


@pytest.fixture(autouse=True)
def observe_persistence_without_execution(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    # These API tests inspect persisted creation/reset facts, not network work.
    async def paused() -> None:
        pass

    monkeypatch.setattr(cast(FastAPI, client.app).state.queue_manager, "_claim_and_run", paused)


def analyze(client: TestClient, analyzer: FakeAnalyzer) -> dict[str, Any]:
    analyzer.result = {
        "_type": "playlist", "title": "Collection", "entries": [
            {"id": "same", "title": "First", "url": "https://example.com/first"},
            {"id": "same", "title": "Second", "url": "https://example.com/second"},
            {"id": "gone", "title": "Gone", "url": "https://example.com/gone",
             "availability": "private"},
        ],
    }
    response = client.post("/api/v1/analysis", json={"url": "https://example.com/list"})
    assert response.status_code == 200
    return response.json()


def request_for(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_id": snapshot["analysis_id"],
        "item_ids": [item["item_id"] for item in snapshot["collection"]["items"][:2]][::-1],
        "collection_mode": "audio",
    }


def mutate(settings: Settings, sql: str, args: tuple[Any, ...]) -> None:
    with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
        connection.execute(sql, args)


def test_persisted_create_read_list_and_queue_candidate(
    client: TestClient, analyzer: FakeAnalyzer, settings: Settings,
) -> None:
    application = cast(FastAPI, client.app)
    snapshot = analyze(client, analyzer)
    request = request_for(snapshot)
    response = client.post("/api/v1/jobs", json=request)
    assert response.status_code == 200
    job = response.json()
    assert job["job_type"] == "collection"
    assert job["variant_id"] == ""
    assert job["collection_mode"] == "audio"
    assert job["collection_profile"] == {"kind": "original_best_available", "max_height": None}
    assert (job["total_items"], job["completed_items"], job["failed_items"]) == (2, 0, 0)
    assert [item["item_id"] for item in job["items"]] == request["item_ids"][::-1]
    assert [item["source_order"] for item in job["items"]] == [1, 2]
    assert all(item["status"] == "queued" and item["progress"]["percent"] == 0
               for item in job["items"])
    assert client.get(f"/api/v1/jobs/{job['id']}").json() == job
    assert client.get("/api/v1/jobs").json()["jobs"] == [job]
    events = client.get(f"/api/v1/jobs/{job['id']}/activity").json()["events"]
    assert events[0]["message"] == "Queued"

    async def claim() -> list[str]:
        async with application.state.sessionmaker() as session:
            return [record.id for record in await jobs.claimable_queued_jobs(session, limit=1)]

    assert client.portal is not None
    assert client.portal.call(claim) == [job["id"]]
    # Actual execution and lifecycle ownership are covered by dispatch tests.
    with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
        row = connection.execute("SELECT collection_profile_json FROM download_jobs").fetchone()
        assert row == (None,)
    mutate(settings, "UPDATE download_jobs SET owner_id = ? WHERE id = ?", ("other", job["id"]))
    assert client.get(f"/api/v1/jobs/{job['id']}").status_code == 404
    assert client.get("/api/v1/jobs").json() == {"jobs": []}
    async def foreign_items() -> list[Any]:
        async with application.state.sessionmaker() as session:
            return await jobs.list_job_items(session, job["id"], "local")
    assert client.portal.call(foreign_items) == []


@pytest.mark.parametrize("status,code,eligible", [
    ("failed", "network_error", True), ("failed", "unsupported_url", False),
    ("failed", None, False), ("failed", "unknown", False),
    ("cancelled", "unsupported_url", True), ("queued", None, True),
    ("completed", "network_error", False),
])
def test_collection_retry_flags_match_read_list_and_postcommit_snapshot(
    client: TestClient, analyzer: FakeAnalyzer, settings: Settings,
    status: str, code: str | None, eligible: bool,
) -> None:
    application = cast(FastAPI, client.app)
    record = client.post("/api/v1/jobs", json=request_for(analyze(client, analyzer))).json()
    mutate(settings, "UPDATE download_jobs SET status = 'failed', "
           "error_code = 'all_children_failed', error_message = 'Summary' WHERE id = ?",
           (record["id"],))
    mutate(settings, "UPDATE job_items SET status = ?, error_code = ? WHERE job_id = ?",
           (status, code, record["id"]))
    read = client.get(f"/api/v1/jobs/{record['id']}").json()
    assert read["error"]["retryable"] == eligible
    assert client.get("/api/v1/jobs").json()["jobs"] == [read]
    for item in read["items"]:
        if code:
            assert item["error"]["retryable"] == eligible
        else:
            assert item["error"] is None
    subscriber = application.state.event_bus.subscribe()
    try:
        async def publish() -> None:
            from app.domain.actors import Actor

            fresh = await application.state.job_service.get_job(Actor(), record["id"])
            application.state.queue_manager.publish_snapshot("job.failed", fresh)
        assert client.portal is not None
        client.portal.call(publish)
        snapshot = subscriber.get_nowait()["data"]
        assert snapshot["error"] == read["error"]
        assert set(snapshot) == set(read) and snapshot["items"] is None
    finally:
        application.state.event_bus.unsubscribe(subscriber)


@pytest.mark.parametrize(
    "selection", ["empty", "duplicate", "unknown", "unavailable", "wrong_analysis"]
)
def test_selection_rejection(client: TestClient, analyzer: FakeAnalyzer, selection: str) -> None:
    snapshot = analyze(client, analyzer)
    request = request_for(snapshot)
    if selection == "empty":
        request["item_ids"] = []
    elif selection == "duplicate":
        request["item_ids"] *= 2
    elif selection == "unknown":
        request["item_ids"] = ["unknown"]
    elif selection == "unavailable":
        request["item_ids"] = [snapshot["collection"]["items"][2]["item_id"]]
    else:
        request["item_ids"] = request_for(analyze(client, analyzer))["item_ids"]
    assert client.post("/api/v1/jobs", json=request).status_code == 422
    assert client.get("/api/v1/jobs").json() == {"jobs": []}


@pytest.mark.parametrize("case,code", [("foreign", 404), ("expired", 422), ("legacy", 422)])
def test_snapshot_rejection(
    client: TestClient, analyzer: FakeAnalyzer, settings: Settings, case: str, code: int,
) -> None:
    snapshot = analyze(client, analyzer)
    request = request_for(snapshot)
    if case == "foreign":
        mutate(settings, "UPDATE analysis_sessions SET owner_id = ? WHERE id = ?",
               ("other", snapshot["analysis_id"]))
    elif case == "expired":
        mutate(settings, "UPDATE analysis_sessions SET expires_at = ? WHERE id = ?",
               (datetime.now(UTC) - timedelta(hours=1), snapshot["analysis_id"]))
    else:
        snapshot["collection"].pop("items")
        mutate(settings, "UPDATE analysis_sessions SET normalized_result_json = ? WHERE id = ?",
               (json.dumps(snapshot), snapshot["analysis_id"]))
    assert client.post("/api/v1/jobs", json=request).status_code == code


@pytest.mark.parametrize("count,status", [(200, 200), (201, 422)])
def test_backend_selected_cap_on_synthetic_persisted_snapshot(
    client: TestClient, analyzer: FakeAnalyzer, settings: Settings, count: int, status: int,
) -> None:
    application = cast(FastAPI, client.app)
    snapshot = analyze(client, analyzer)
    template = snapshot["collection"]["items"][0]
    snapshot["collection"]["items"] = [
        {**template, "item_id": f"{snapshot['analysis_id']}:slot:{i}", "source_order": i}
        for i in range(1, 202)
    ]
    mutate(settings, "UPDATE analysis_sessions SET normalized_result_json = ? WHERE id = ?",
           (json.dumps(snapshot), snapshot["analysis_id"]))
    request = request_for(snapshot)
    request["item_ids"] = [
        item["item_id"] for item in snapshot["collection"]["items"][:count]
    ][::-1]
    response = client.post("/api/v1/jobs", json=request)
    assert response.status_code == status
    if status == 200:
        assert response.json()["total_items"] == 200
        assert [item["source_order"] for item in response.json()["items"]] == list(range(1, 201))
    # Service also enforces the cap independently of request validation.
    if count == 201:
        from app.domain.actors import Actor
        from app.domain.jobs import JobError

        async def create_overcap() -> None:
            await application.state.job_service.create_job(
                Actor(id="local"), analysis_id=snapshot["analysis_id"],
                item_ids=request["item_ids"], collection_mode="audio",
                destination="app_library",
            )

        with pytest.raises(JobError):
            assert client.portal is not None
            client.portal.call(create_overcap)


@pytest.mark.parametrize("mode,profile,status", [
    ("audio", None, 200),
    ("audio", {"kind": "original_best_available"}, 200),
    ("video", {"kind": "original_best_available", "max_height": 1080}, 200),
    ("audio", {"max_height": 1080}, 422),
    ("video", {"max_height": True}, 422),
    ("video", {"max_height": "1080"}, 422),
    ("video", {"max_height": 0}, 422),
    ("video", {"max_height": 1080.5}, 422),
    ("audio", {"kind": "mp3"}, 422),
    ("audio", {"kind": "wav"}, 422),
    ("audio", {"args": "--exec evil"}, 422),
    ("invalid", None, 422),
])
def test_profile_contract(
    client: TestClient, analyzer: FakeAnalyzer, mode: str, profile: Any, status: int,
) -> None:
    request = request_for(analyze(client, analyzer))
    request.update(collection_mode=mode, collection_profile=profile)
    response = client.post("/api/v1/jobs", json=request)
    assert response.status_code == status
    if status == 200:
        job = response.json()
        stored = client.get(f"/api/v1/jobs/{job['id']}").json()
        assert stored["collection_profile"] == job["collection_profile"]


@pytest.mark.parametrize("extra", [
    {"variant_id": "audio-best"}, {"source_url": "https://evil.example"},
    {"format_id": "137"}, {"raw_args": ["--exec", "evil"]}, {"item_profiles": []},
])
def test_reject_mixed_or_arbitrary_input(
    client: TestClient, analyzer: FakeAnalyzer, extra: dict[str, Any],
) -> None:
    request = request_for(analyze(client, analyzer))
    request.update(extra)
    assert client.post("/api/v1/jobs", json=request).status_code == 422


@pytest.mark.parametrize("failure", ["children", "activity", "commit"])
def test_transaction_failure_has_no_rows_publication_or_notification(
    client: TestClient, analyzer: FakeAnalyzer, settings: Settings,
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    application = cast(FastAPI, client.app)
    request = request_for(analyze(client, analyzer))
    published: list[Any] = []
    notified: list[bool] = []
    monkeypatch.setattr(application.state.event_bus, "publish", published.append)
    monkeypatch.setattr(application.state.queue_manager, "notify", lambda: notified.append(True))
    original = jobs.stage_collection_job

    async def fail(*args: Any, **kwargs: Any) -> None:
        if failure == "children":
            await original(*args, **kwargs)
        raise RuntimeError("database failure")

    if failure == "children":
        monkeypatch.setattr(jobs, "stage_collection_job", fail)
    elif failure == "activity":
        monkeypatch.setattr(activity, "add_activity_event", fail)
    else:
        monkeypatch.setattr(AsyncSession, "commit", fail)
    with pytest.raises(RuntimeError, match="database failure"):
        client.post("/api/v1/jobs", json=request)
    with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
        for table in ("download_jobs", "job_items", "activity_events"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert published == []
    assert notified == []
