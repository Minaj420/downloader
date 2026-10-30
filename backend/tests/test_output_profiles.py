"""M6.4b output profile schema/persistence/API and the fail-closed gates.

The public create path accepts a known non-Original profile in the request's
audio/video set; a single job also validates the runner's pure conversion plan
first, so an incompatible choice is rejected without side effects. The single
runner converts known profiles (see test_single_conversion) and the collection
child converts known profiles that fit its mode (see test_collection_conversion);
both fail closed on unknown stored values, the child also on a known profile
outside its mode's set. Such out-of-set or corrupt rows here are written
directly (controlled records), never through the public create path.
"""

import asyncio
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.api.v1.jobs import CreateJobRequest
from app.config import Settings
from app.domain.jobs import (
    AUDIO_OUTPUT_PROFILES,
    VIDEO_OUTPUT_PROFILES,
    JobDto,
    JobError,
    JobErrorCode,
    OutputProfile,
    output_profile_allowed,
)
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs as job_repo
from app.services import collection_parent_runner as parent_runner
from app.services import job_runner
from app.services.collection_child_runner import run_collection_child
from app.services.event_bus import EventBus
from app.services.job_mapping import job_to_dto
from app.services.job_runner import RunnerDeps, run_download_job
from app.services.storage_service import StorageService
from tests.conftest import FakeAnalyzer, build_application
from tests.test_collection_child_runner import Harness
from tests.test_collection_repository import child, factory, parent
from tests.test_jobs import YOUTUBE_URL, wait_for_job

KNOWN = list(get_args(OutputProfile))
NON_ORIGINAL = [profile for profile in KNOWN if profile != "original"]
AUDIO_VARIANT = "audio-opus-160"
VIDEO_VARIANT = "video-720"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def publish(self, value: dict[str, Any]) -> None:
        self.events.append(value)


def collection_payload() -> dict[str, Any]:
    return {"_type": "playlist", "title": "Collection", "entries": [
        {"id": "one", "title": "First", "url": "https://example.com/first"},
        {"id": "two", "title": "Second", "url": "https://example.com/second"},
    ]}


def db(settings: Settings) -> sqlite3.Connection:
    return sqlite3.connect(settings.state_dir / "downloader.db")


def execute(settings: Settings, sql: str, args: tuple[Any, ...] = ()) -> None:
    with db(settings) as connection:
        connection.execute(sql, args)


def scalar(settings: Settings, sql: str, args: tuple[Any, ...] = ()) -> Any:
    with db(settings) as connection:
        return connection.execute(sql, args).fetchone()[0]


def row_counts(settings: Settings) -> tuple[int, ...]:
    return tuple(scalar(settings, f"SELECT COUNT(*) FROM {table}")
                 for table in ("download_jobs", "job_items", "activity_events"))


@contextmanager
def app_client(
    settings: Settings, analyzer: FakeAnalyzer, *, paused: bool,
) -> Generator[TestClient, None, None]:
    with TestClient(build_application(settings, analyzer)) as client:
        if paused:
            async def no_claims() -> None:
                pass

            cast(FastAPI, client.app).state.queue_manager._claim_and_run = no_claims
        yield client


def analyze(client: TestClient, analyzer: FakeAnalyzer, *, collection: bool) -> dict[str, Any]:
    if collection:
        analyzer.result = collection_payload()
    url = "https://example.com/list" if collection else YOUTUBE_URL
    response = client.post("/api/v1/analysis", json={"url": url})
    assert response.status_code == 200
    return response.json()


def single_request(analysis: dict[str, Any], variant: str, **extra: Any) -> dict[str, Any]:
    return {"analysis_id": analysis["analysis_id"], "variant_id": variant, **extra}


def collection_request(analysis: dict[str, Any], mode: str, **extra: Any) -> dict[str, Any]:
    return {"analysis_id": analysis["analysis_id"], "collection_mode": mode,
            "item_ids": [item["item_id"] for item in analysis["collection"]["items"]],
            **extra}


# --- domain / schema ---------------------------------------------------------


def test_known_profiles_sets_and_shared_original_default() -> None:
    assert KNOWN == ["original", "mp3", "wav", "m4a", "opus", "flac", "mp4"]
    assert AUDIO_OUTPUT_PROFILES == {"original", "mp3", "wav", "m4a", "opus", "flac"}
    assert VIDEO_OUTPUT_PROFILES == {"original", "mp4"}
    for profile in KNOWN:
        assert output_profile_allowed("audio", profile) == (profile in AUDIO_OUTPUT_PROFILES)
        assert output_profile_allowed("video", profile) == (profile in VIDEO_OUTPUT_PROFILES)
    assert JobDto.model_fields["output_profile"].default == "original"
    request = CreateJobRequest(analysis_id="a", variant_id="v")
    assert request.output_profile is None
    for profile in KNOWN:
        # Common field: never part of the single/collection exclusion.
        assert CreateJobRequest(analysis_id="a", variant_id="v",
                                output_profile=profile).output_profile == profile
        assert CreateJobRequest(analysis_id="a", item_ids=["x"], collection_mode="audio",
                                output_profile=profile).output_profile == profile
    assert CreateJobRequest(analysis_id="a", variant_id="v",
                            output_profile=None).output_profile is None


@pytest.mark.parametrize("value", ["", "MP3", "Original", "aac", "mkv", 3, True, ["mp3"]])
def test_request_rejects_unknown_or_mistyped_profile(value: Any) -> None:
    with pytest.raises(ValidationError):
        CreateJobRequest.model_validate(
            {"analysis_id": "a", "variant_id": "v", "output_profile": value})


def _record(**values: Any) -> DownloadJob:
    now = datetime.now(UTC)
    fields: dict[str, Any] = dict(
        id="job", owner_id="local", analysis_id="analysis", source_url="url", title="t",
        variant_id="v", destination="app_library", conflict_policy="skip",
        keep_app_copy=False, status="queued", progress_percent=0.0, job_type="single",
        total_items=1, completed_items=0, failed_items=0, created_at=now, updated_at=now,
    )
    fields.update(values)
    return DownloadJob(**fields)


@pytest.mark.parametrize("stored,expected", [(None, "original"),
                                             *[(p, p) for p in KNOWN]])
def test_mapping_reads_null_as_original_and_known_values_unchanged(
    stored: str | None, expected: str,
) -> None:
    assert job_to_dto(_record(output_profile=stored)).output_profile == expected


@pytest.mark.parametrize("stored", ["", "bogus", "MP3", "Original"])
def test_mapping_unknown_value_never_falls_back_to_original(stored: str) -> None:
    with pytest.raises(ValueError):
        job_to_dto(_record(output_profile=stored))


# --- API create / read --------------------------------------------------------


@pytest.mark.parametrize("collection", [False, True])
@pytest.mark.parametrize("sent", ["omitted", None, "original"])
def test_original_create_get_list_reports_original_and_stores_null(
    settings: Settings, analyzer: FakeAnalyzer, collection: bool, sent: str | None,
) -> None:
    with app_client(settings, analyzer, paused=True) as client:
        analysis = analyze(client, analyzer, collection=collection)
        extra = {} if sent == "omitted" else {"output_profile": sent}
        request = (collection_request(analysis, "audio", **extra) if collection
                   else single_request(analysis, AUDIO_VARIANT, **extra))
        response = client.post("/api/v1/jobs", json=request)
        assert response.status_code == 200
        job = response.json()
        assert job["output_profile"] == "original"
        assert client.get(f"/api/v1/jobs/{job['id']}").json() == job
        assert client.get("/api/v1/jobs").json()["jobs"] == [job]
        assert scalar(settings, "SELECT output_profile FROM download_jobs") is None


@pytest.mark.parametrize("sent,stored", [("original", None), ("mp4", "mp4")])
def test_collection_profile_max_height_is_independent_of_output_profile(
    settings: Settings, analyzer: FakeAnalyzer, sent: str, stored: str | None,
) -> None:
    with app_client(settings, analyzer, paused=True) as client:
        analysis = analyze(client, analyzer, collection=True)
        job = client.post("/api/v1/jobs", json=collection_request(
            analysis, "video", collection_profile={"max_height": 1080},
            output_profile=sent)).json()
        assert job["collection_profile"] == {"kind": "original_best_available",
                                             "max_height": 1080}
        assert job["output_profile"] == sent
        with db(settings) as connection:
            assert connection.execute(
                "SELECT collection_profile_json, output_profile FROM download_jobs"
            ).fetchone() == ('{"kind":"original_best_available","max_height":1080}', stored)


def _compatible_cases() -> list[tuple[bool, str, str]]:
    audio = sorted(AUDIO_OUTPUT_PROFILES - {"original"})
    assert len(audio) == 5
    return [*[(False, AUDIO_VARIANT, profile) for profile in audio],
            (False, VIDEO_VARIANT, "mp4"),
            *[(True, "audio", profile) for profile in audio],
            (True, "video", "mp4")]


def _incompatible_cases() -> list[tuple[bool, str, str]]:
    cases = []
    for profile in NON_ORIGINAL:
        if not output_profile_allowed("audio", profile):
            cases += [(False, AUDIO_VARIANT, profile), (True, "audio", profile)]
        if not output_profile_allowed("video", profile):
            cases += [(False, VIDEO_VARIANT, profile), (True, "video", profile)]
    return cases


def _record_side_effects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Any], list[bool]]:
    application = cast(FastAPI, client.app)
    published: list[Any] = []
    notified: list[bool] = []
    monkeypatch.setattr(application.state.event_bus, "publish", published.append)
    monkeypatch.setattr(application.state.queue_manager, "notify",
                        lambda: notified.append(True))
    return published, notified


@pytest.mark.parametrize("collection,choice,profile", _compatible_cases())
def test_compatible_non_original_create_is_stored_and_read_back(
    settings: Settings, analyzer: FakeAnalyzer, monkeypatch: pytest.MonkeyPatch,
    collection: bool, choice: str, profile: str,
) -> None:
    with app_client(settings, analyzer, paused=True) as client:
        analysis = analyze(client, analyzer, collection=collection)
        published, notified = _record_side_effects(client, monkeypatch)
        request = (collection_request(analysis, choice, output_profile=profile) if collection
                   else single_request(analysis, choice, output_profile=profile))
        response = client.post("/api/v1/jobs", json=request)
        assert response.status_code == 200
        job = response.json()
        assert job["output_profile"] == profile
        assert job["job_type"] == ("collection" if collection else "single")
        assert client.get(f"/api/v1/jobs/{job['id']}").json() == job
        assert client.get("/api/v1/jobs").json()["jobs"] == [job]
        assert scalar(settings, "SELECT output_profile FROM download_jobs") == profile
        assert row_counts(settings) == ((1, 2, 1) if collection else (1, 0, 1))
        assert [(event["event"], event["data"]["job_id"]) for event in published] == [
            ("activity.created", job["id"])]
        assert notified == [True]
        # The queued job's published lifecycle snapshot carries the same profile.
        assert client.post(f"/api/v1/jobs/{job['id']}/cancel").status_code == 200
        snapshots = [event["data"] for event in published if event["event"].startswith("job.")]
        assert snapshots and all(
            (data["id"], data["output_profile"]) == (job["id"], profile) for data in snapshots)


@pytest.mark.parametrize("collection,choice,profile", _incompatible_cases())
def test_incompatible_non_original_create_rejected_without_side_effects(
    settings: Settings, analyzer: FakeAnalyzer, monkeypatch: pytest.MonkeyPatch,
    collection: bool, choice: str, profile: str,
) -> None:
    with app_client(settings, analyzer, paused=True) as client:
        analysis = analyze(client, analyzer, collection=collection)
        published, notified = _record_side_effects(client, monkeypatch)
        request = (collection_request(analysis, choice, output_profile=profile) if collection
                   else single_request(analysis, choice, output_profile=profile))
        response = client.post("/api/v1/jobs", json=request)
        assert response.status_code == 422
        assert response.json()["error"] == {
            "code": "invalid_profile", "message": "This output preference is not supported.",
            "retryable": False, "job_id": None,
        }
        assert row_counts(settings) == (0, 0, 0)
        assert published == [] and notified == []
        assert client.get("/api/v1/jobs").json() == {"jobs": []}


def test_video_only_mp4_without_audio_companion_is_invalid_variant_without_side_effects(
    settings: Settings, analyzer: FakeAnalyzer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer.result = {"id": "silent", "_type": "video", "title": "Silent",
                       "webpage_url": YOUTUBE_URL, "formats": [
                           {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028",
                            "acodec": "none", "width": 1920, "height": 1080}]}
    with app_client(settings, analyzer, paused=True) as client:
        analysis = analyze(client, analyzer, collection=False)
        kinds = {fmt["variant_id"]: fmt["kind"] for fmt in analysis["formats"]}
        assert set(kinds.values()) == {"video_only"}
        published, notified = _record_side_effects(client, monkeypatch)
        for variant in kinds:
            response = client.post("/api/v1/jobs", json=single_request(
                analysis, variant, output_profile="mp4"))
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_variant"
        assert row_counts(settings) == (0, 0, 0)
        assert published == [] and notified == []
        assert client.get("/api/v1/jobs").json() == {"jobs": []}


@pytest.mark.parametrize("collection", [False, True])
@pytest.mark.parametrize("value", ["", "aac", "MP4", 1, False, {"kind": "mp3"}])
def test_unknown_or_mistyped_api_value_is_422_without_side_effects(
    settings: Settings, analyzer: FakeAnalyzer, monkeypatch: pytest.MonkeyPatch,
    collection: bool, value: Any,
) -> None:
    with app_client(settings, analyzer, paused=True) as client:
        application = cast(FastAPI, client.app)
        analysis = analyze(client, analyzer, collection=collection)
        published: list[Any] = []
        notified: list[bool] = []
        monkeypatch.setattr(application.state.event_bus, "publish", published.append)
        monkeypatch.setattr(application.state.queue_manager, "notify",
                            lambda: notified.append(True))
        request = (collection_request(analysis, "audio", output_profile=value) if collection
                   else single_request(analysis, AUDIO_VARIANT, output_profile=value))
        assert client.post("/api/v1/jobs", json=request).status_code == 422
        assert row_counts(settings) == (0, 0, 0)
        assert published == [] and notified == []


def test_foreign_analysis_is_not_found_before_profile_checks(
    settings: Settings, analyzer: FakeAnalyzer,
) -> None:
    with app_client(settings, analyzer, paused=True) as client:
        analysis = analyze(client, analyzer, collection=False)
        execute(settings, "UPDATE analysis_sessions SET owner_id = 'other'")
        response = client.post("/api/v1/jobs", json=single_request(
            analysis, AUDIO_VARIANT, output_profile="mp3"))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "analysis_not_found"
        assert row_counts(settings) == (0, 0, 0)


@pytest.mark.parametrize("collection,stored", [
    (False, "mp3"), (False, "mp4"), (True, "flac"), (True, "mp4"),
])
def test_controlled_known_record_reads_back_unchanged_and_owner_scoped(
    settings: Settings, analyzer: FakeAnalyzer, collection: bool, stored: str,
) -> None:
    with app_client(settings, analyzer, paused=True) as client:
        analysis = analyze(client, analyzer, collection=collection)
        request = (collection_request(analysis, "video") if collection
                   else single_request(analysis, VIDEO_VARIANT))
        job = client.post("/api/v1/jobs", json=request).json()
        execute(settings, "UPDATE download_jobs SET output_profile = ?", (stored,))
        read = client.get(f"/api/v1/jobs/{job['id']}").json()
        assert read["output_profile"] == stored
        assert client.get("/api/v1/jobs").json()["jobs"] == [read]
        assert {k: v for k, v in read.items() if k != "output_profile"} == {
            k: v for k, v in job.items() if k != "output_profile"}
        execute(settings, "UPDATE download_jobs SET owner_id = 'other'")
        assert client.get(f"/api/v1/jobs/{job['id']}").status_code == 404
        assert client.get("/api/v1/jobs").json() == {"jobs": []}


# --- runtime gate: single -------------------------------------------------------


class _SingleRun:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine, self.sessions = factory(tmp_path)
        self.roots = (tmp_path / "staging", tmp_path / "library")
        self.storage = StorageService(*self.roots)
        self.storage.ensure_layout()
        self.bus = RecordingBus()
        self.io: list[str] = []

        async def no_analysis(*args: Any, **kwargs: Any) -> Any:
            self.io.append("analysis")
            raise AssertionError("analysis must not be read")

        async def no_media(*args: Any, **kwargs: Any) -> Any:
            self.io.append("prepare_media")
            raise AssertionError("media must not be prepared")

        monkeypatch.setattr(job_runner, "_get_analysis", no_analysis)
        monkeypatch.setattr(job_runner, "prepare_media", no_media)
        # Any adapter use would surface as INTERNAL_ERROR, not INVALID_PROFILE.
        self.deps = RunnerDeps(self.sessions, cast(Any, object()), cast(Any, object()),
                               self.storage, self.bus, 0.0)

    async def seed(self, profile: str | None) -> None:
        async with self.sessions() as session:
            await job_repo.add_job(session, _record(output_profile=profile, owner_id="owner"))
            await session.commit()

    async def run(self, cancel: bool = False) -> None:
        event = asyncio.Event()
        if cancel:
            event.set()
        await run_download_job("job", deps=self.deps, cancel_event=event,
                               processes={}, cancel_gates={})

    async def facts(self) -> tuple[DownloadJob, list[str], int, int]:
        async with self.sessions() as session:
            record = await session.get(DownloadJob, "job")
            assert record is not None
            messages = list((await session.scalars(
                select(ActivityEvent.message).order_by(ActivityEvent.id))).all())
            library = len((await session.scalars(select(LibraryItem))).all())
            files = len((await session.scalars(select(MediaFile))).all())
        return record, messages, library, files

    def tree(self) -> list[Path]:
        return sorted(path for root in self.roots for path in root.rglob("*"))


@pytest.mark.anyio
async def test_single_runner_cancel_precedence_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _SingleRun(tmp_path, monkeypatch)
    try:
        await run.seed("bogus")
        # Accepted risk: the corrupt value is not mappable to a DTO snapshot.
        await asyncio.gather(run.run(cancel=True), return_exceptions=True)
        record, messages, library, _ = await run.facts()
        assert run.io == []
        assert (record.status, record.error_code, record.output_profile) == (
            "cancelled", "cancelled", "bogus")
        assert messages[-1] == "Cancelled" and library == 0
    finally:
        await run.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("profile", ["", "bogus"])
async def test_single_runner_unknown_value_does_no_io_and_never_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str,
) -> None:
    run = _SingleRun(tmp_path, monkeypatch)
    try:
        await run.seed(profile)
        before = run.tree()
        # Accepted risk: the corrupt value is not mappable to a DTO snapshot.
        await asyncio.gather(run.run(), return_exceptions=True)
        record, _, library, files = await run.facts()
        assert run.io == []
        assert record.status != "completed" and record.final_path is None
        assert record.output_profile == profile
        assert (library, files) == (0, 0) and run.tree() == before
    finally:
        await run.engine.dispose()


# --- runtime gate: collection --------------------------------------------------


async def _seed_collection(h: Harness, profile: str | None, children: list[JobItem],
                           mode: str = "video", **values: Any) -> None:
    async with h.sessions() as session:
        await job_repo.stage_collection_job(session, parent(
            collection_mode=mode, output_profile=profile, **values), children,
            owner_id="owner")
        await session.commit()


def _unrunnable_child_cases() -> list[tuple[str, str]]:
    # Known profiles outside the mode's output set, plus unknown stored values.
    mismatched = [("video", p) for p in NON_ORIGINAL if not output_profile_allowed("video", p)]
    mismatched += [("audio", p) for p in NON_ORIGINAL if not output_profile_allowed("audio", p)]
    assert ("audio", "mp4") in mismatched and ("video", "mp3") in mismatched
    return [*mismatched, *[(mode, value) for mode in ("audio", "video")
                           for value in ("", "bogus", "MP3", "original ")]]


@pytest.mark.anyio
@pytest.mark.parametrize("mode,profile", _unrunnable_child_cases())
async def test_child_fails_closed_before_probe(tmp_path: Path, mode: str, profile: str) -> None:
    h = Harness(tmp_path)
    try:
        await _seed_collection(h, profile, [child(2)], mode=mode, status="analyzing")
        with pytest.raises(JobError) as caught:
            await run_collection_child(
                "parent", "owner", "child-2", deps=h.deps, cancel_event=h.adapters.cancel,
                processes=h.processes, cancel_gate=h.gate,
            )
        assert caught.value.code == JobErrorCode.INVALID_PROFILE
        assert h.adapters.calls == [] and h.adapters.urls == [] and not h.processes
        async with h.sessions() as session:
            row = await session.get(JobItem, "child-2")
            assert row is not None and row.status == "analyzing"  # caller records failure
            assert (await session.scalars(select(LibraryItem))).all() == []
        assert not (tmp_path / "staging" / "child-2").exists()
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("with_completed", [False, True])
async def test_parent_settles_gated_children_and_keeps_completed_immutable(
    tmp_path: Path, with_completed: bool,
) -> None:
    h = Harness(tmp_path)
    bus = RecordingBus()
    try:
        children = [child(2), child(9)]
        if with_completed:
            children.insert(0, child(1, status="completed", final_path="video/01 - Kept.mp4",
                                     progress_percent=100.0))
        # A known profile outside the video set: every queued child fails closed.
        await _seed_collection(h, "mp3", children)
        async with h.sessions() as session:
            before = await session.get(JobItem, "child-1")
            kept = None if before is None else (before.status, before.final_path,
                                                before.error_code, before.updated_at)
        assert await parent_runner.run_collection_parent(
            "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
            processes=h.processes, cancel_gate=h.gate, bus=bus,
        ) == parent_runner.ParentRunOutcome.EXHAUSTED
        assert h.adapters.calls == []
        async with h.sessions() as session:
            record = await session.get(DownloadJob, "parent")
            assert record is not None
            assert (record.status, record.error_code, record.output_profile) == (
                "failed", "partial_failure" if with_completed else "all_children_failed", "mp3")
            for child_id in ("child-2", "child-9"):
                row = await session.get(JobItem, child_id)
                assert row is not None and (row.status, row.error_code) == (
                    "failed", "invalid_profile")
            if with_completed:
                after = await session.get(JobItem, "child-1")
                assert after is not None
                assert (after.status, after.final_path, after.error_code,
                        after.updated_at) == kept
            assert (await session.scalars(select(LibraryItem))).all() == []
            items = await job_repo.list_job_items(session, "parent", "owner")
        dto = job_to_dto(record, items)
        assert dto.output_profile == "mp3"
        assert dto.error is not None and dto.error.retryable is False
        assert [event["event"] for event in bus.events][-1] == "job.failed"
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_all_completed_parent_keeps_success_precedence(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    try:
        await _seed_collection(h, "mp3", [child(1, status="completed")])
        assert await parent_runner.run_collection_parent(
            "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
            processes=h.processes, cancel_gate=h.gate,
        ) == parent_runner.ParentRunOutcome.EXHAUSTED
        async with h.sessions() as session:
            record = await session.get(DownloadJob, "parent")
            assert record is not None and record.status == "completed"
            assert record.output_profile == "mp3"
        assert h.adapters.calls == []
    finally:
        await h.engine.dispose()


# --- retry / recovery keep the stored choice ---------------------------------------


# Single recovery/retry with the stored profile runs real conversion in the
# runner: see test_single_conversion (fake binaries, no real media commands).
# Successful converted collection retry/recovery: see test_collection_conversion.


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_collection_real_retry_keeps_parent_profile_and_completed_children(
    settings: Settings, analyzer: FakeAnalyzer, terminal: str,
) -> None:
    with app_client(settings, analyzer, paused=True) as client:
        job = client.post("/api/v1/jobs", json=collection_request(
            analyze(client, analyzer, collection=True), "audio")).json()
    first, second = (item["id"] for item in job["items"])
    # mp4 is a known profile outside the audio set: the retried child fails closed.
    execute(settings, "UPDATE download_jobs SET status = ?, error_code = ?, "
            "output_profile = 'mp4', completed_items = 1, failed_items = ?",
            (terminal, "partial_failure" if terminal == "failed" else "cancelled",
             1 if terminal == "failed" else 0))
    execute(settings, "UPDATE job_items SET status = 'completed', progress_percent = 100, "
            "final_path = 'audio/First.opus' WHERE id = ?", (first,))
    execute(settings, "UPDATE job_items SET status = ?, error_code = ? WHERE id = ?",
            (terminal, "network_error" if terminal == "failed" else "cancelled", second))
    with db(settings) as connection:
        kept = connection.execute("SELECT * FROM job_items WHERE id = ?", (first,)).fetchone()
    with app_client(settings, analyzer, paused=False) as client:
        response = client.post(f"/api/v1/jobs/{job['id']}/retry")
        assert response.status_code == 200
        assert response.json()["output_profile"] == "mp4"
        final = wait_for_job(client, job["id"], statuses={"completed", "failed", "cancelled"})
        assert (final["status"], final["error"]["code"]) == ("failed", "partial_failure")
        assert final["output_profile"] == "mp4" and final["error"]["retryable"] is False
        statuses = {item["id"]: (item["status"], item["error"] and item["error"]["code"])
                    for item in final["items"]}
        assert statuses == {first: ("completed", None), second: ("failed", "invalid_profile")}
        assert client.post(f"/api/v1/jobs/{job['id']}/retry").status_code == 409
    with db(settings) as connection:
        assert connection.execute(
            "SELECT * FROM job_items WHERE id = ?", (first,)).fetchone() == kept
        assert connection.execute("SELECT output_profile FROM download_jobs").fetchone() == (
            "mp4",)
        assert connection.execute("SELECT COUNT(*) FROM library_items").fetchone() == (0,)
