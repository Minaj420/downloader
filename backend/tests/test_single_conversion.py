"""M6.4d-d2: single runner conversion wiring.

The runner is driven directly with stub adapters (no real yt-dlp/ffmpeg); the
API create and retry/recovery tests go through the app with fake binaries.
Runner-level rows are written directly as controlled records.
"""

import asyncio
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.domain.analysis import MediaFormatKind, NormalizedAnalysis, NormalizedFormat
from app.domain.jobs import job_error_code_retryable
from app.integrations.ffmpeg import ConversionProfile, FFmpegError, FFmpegFailureKind
from app.models.analysis import AnalysisSession
from app.models.jobs import ActivityEvent, DownloadJob
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs as job_repo
from app.services.job_runner import RunnerDeps, run_download_job
from app.services.storage_service import StorageService
from tests.conftest import FakeAnalyzer, build_application
from tests.fake_binaries import fake_ffmpeg_fail, fake_ffmpeg_record, fake_ytdlp_ok
from tests.test_collection_repository import factory
from tests.test_jobs import wait_for_job
from tests.test_output_profiles import (
    AUDIO_VARIANT,
    RecordingBus,
    analyze,
    app_client,
    execute,
    scalar,
    single_request,
)

TITLE = "Title"
AUDIO = MediaFormatKind.AUDIO_ONLY
MUXED = MediaFormatKind.MUXED
VIDEO_ONLY = MediaFormatKind.VIDEO_ONLY


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def fmt(
    variant: str, kind: MediaFormatKind, *, video: str | None = None,
    audio: str | None = None, bitrate: float | None = None,
) -> NormalizedFormat:
    return NormalizedFormat(
        variant_id=variant, format_id=f"raw-{variant}", kind=kind,
        video_codec=video, audio_codec=audio, bitrate_kbps=bitrate,
    )


class StubMedia:
    """Stands in for both the yt-dlp downloader and the FFmpeg processor."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.conversions: list[tuple[str, str, bool, bool]] = []
        self.convert_error: FFmpegError | None = None
        self.in_convert: Callable[[], None] | None = None

    async def download(self, **kwargs: Any) -> list[str]:
        path = Path(kwargs["output_template"].replace("%(ext)s", "mp4"))
        self.calls.append(f"download:{kwargs['format_selector']}")
        path.write_bytes(b"source")
        return [str(path)]

    async def merge_streams(
        self, video_path: Path, audio_path: Path, output_path: Path,
        on_process_started: Any = None,
    ) -> None:
        self.calls.append("merge")
        output_path.write_bytes(b"merged")

    async def convert(
        self, input_path: Path, output_path: Path, profile: ConversionProfile, *,
        copy_audio: bool = False, copy_video: bool = False, on_process_started: Any = None,
    ) -> None:
        self.calls.append(f"convert:{profile}")
        self.conversions.append((input_path.name, output_path.name, copy_audio, copy_video))
        output_path.write_bytes(b"converted")
        if self.in_convert is not None:
            self.in_convert()
        if self.convert_error is not None:
            raise self.convert_error


class Facts:
    def __init__(
        self, record: DownloadJob, messages: list[str],
        items: list[LibraryItem], files: list[MediaFile],
    ) -> None:
        self.record = record
        self.messages = messages
        self.items = items
        self.files = files


class SingleRun:
    def __init__(self, tmp_path: Path) -> None:
        self.engine, self.sessions = factory(tmp_path)
        self.staging = tmp_path / "staging"
        self.library = tmp_path / "library"
        self.storage = StorageService(self.staging, self.library)
        self.storage.ensure_layout()
        self.storage.initialize_delete_roots()
        self.media = StubMedia()
        self.bus = RecordingBus()
        self.cancel = asyncio.Event()
        self.deps = RunnerDeps(
            self.sessions, cast(Any, self.media), cast(Any, self.media),
            self.storage, self.bus, 0.0,
        )

    async def seed(
        self, formats: list[NormalizedFormat], variant: str, profile: str | None,
    ) -> None:
        now = datetime.now(UTC)
        analysis = NormalizedAnalysis(
            analysis_id="analysis", original_url="https://example.com/v",
            detected_platform="generic", detected_platform_label="Generic",
            content_type="single", formats=formats, duration_seconds=60.0,
        )
        job = DownloadJob(
            id="job", owner_id="owner", analysis_id="analysis", source_url="https://example.com/v",
            title=TITLE, variant_id=variant, destination="app_library", conflict_policy="skip",
            keep_app_copy=False, status="queued", progress_percent=0.0, job_type="single",
            total_items=1, completed_items=0, failed_items=0, created_at=now, updated_at=now,
            output_profile=profile,
        )
        async with self.sessions() as session:
            session.add(AnalysisSession(
                id="analysis", owner_id="owner", source_url="https://example.com/v",
                status="completed", normalized_result_json=analysis.model_dump_json(),
                created_at=now, expires_at=now + timedelta(hours=1),
            ))
            await job_repo.add_job(session, job)
            await session.commit()

    async def run(self) -> None:
        await run_download_job(
            "job", deps=self.deps, cancel_event=self.cancel, processes={}, cancel_gates={},
        )

    async def facts(self) -> Facts:
        async with self.sessions() as session:
            record = await session.get(DownloadJob, "job")
            assert record is not None
            messages = list((await session.scalars(
                select(ActivityEvent.message).order_by(ActivityEvent.id))).all())
            items = list((await session.scalars(select(LibraryItem))).all())
            files = list((await session.scalars(select(MediaFile))).all())
        return Facts(record, messages, items, files)

    def published_files(self) -> list[Path]:
        return sorted(path for path in self.library.rglob("*") if path.is_file())


async def execute_run(
    tmp_path: Path, formats: list[NormalizedFormat], variant: str, profile: str | None,
    *, prepare: Callable[[SingleRun], None] | None = None,
) -> tuple[SingleRun, Facts]:
    run = SingleRun(tmp_path)
    try:
        await run.seed(formats, variant, profile)
        if prepare is not None:
            prepare(run)
        await run.run()
        return run, await run.facts()
    finally:
        await run.engine.dispose()


# --- success ---------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("profile, source_codec, source_bitrate, copy, codec, bitrate", [
    ("mp3", "mp3", 192.0, True, "mp3", 192.0),
    ("mp3", "opus", 160.0, False, "mp3", None),
    ("opus", "opus", 160.0, True, "opus", 160.0),
    ("m4a", "opus", 160.0, False, "aac", None),
    ("wav", "aac", 128.0, False, "pcm_s16le", None),
    ("flac", "opus", 160.0, False, "flac", None),
])
async def test_audio_conversion_copy_or_transcode_publishes_converted_file(
    tmp_path: Path, profile: str, source_codec: str, source_bitrate: float, copy: bool,
    codec: str, bitrate: float | None,
) -> None:
    formats = [fmt("a", AUDIO, audio=source_codec, bitrate=source_bitrate)]
    run, facts = await execute_run(tmp_path, formats, "a", profile)
    record = facts.record
    assert (record.status, record.error_code, record.output_profile) == (
        "completed", None, profile), record.error_detail
    assert record.final_path == f"audio/{TITLE}.{profile}"
    assert record.staging_path == "job"
    assert (run.library / f"audio/{TITLE}.{profile}").read_bytes() == b"converted"
    assert run.media.calls == ["download:raw-a", f"convert:{profile}"]
    assert run.media.conversions == [("media.mp4", f"converted.{profile}", copy, False)]
    assert facts.messages == [
        "Download started", f"Converting to {profile.upper()}", "Finalizing",
        "Saved to App Library",
    ]
    [item] = facts.items
    [media_file] = facts.files
    assert (item.media_type, item.format, item.codec, item.bitrate) == (
        "audio", profile, codec, bitrate)
    assert media_file.path == record.final_path
    assert not (run.staging / "job").exists()
    assert [event["event"] for event in run.bus.events][-1] == "job.completed"


@pytest.mark.anyio
@pytest.mark.parametrize("video, audio, copy_audio, copy_video, bitrate", [
    ("h264", "aac", True, True, 1500.0),
    ("h264", "opus", False, True, None),
    ("vp9", "aac", True, False, None),
    ("vp9", "opus", False, False, None),
])
async def test_mp4_muxed_conversion_publishes_video_with_plan_catalog(
    tmp_path: Path, video: str, audio: str, copy_audio: bool, copy_video: bool,
    bitrate: float | None,
) -> None:
    formats = [fmt("m", MUXED, video=video, audio=audio, bitrate=1500.0)]
    run, facts = await execute_run(tmp_path, formats, "m", "mp4")
    record = facts.record
    assert (record.status, record.final_path) == ("completed", f"video/{TITLE}.mp4")
    assert (run.library / "video" / f"{TITLE}.mp4").read_bytes() == b"converted"
    assert run.media.calls == ["download:raw-m", "convert:mp4"]
    assert run.media.conversions == [("media.mp4", "converted.mp4", copy_audio, copy_video)]
    assert facts.messages == [
        "Download started", "Converting to MP4", "Finalizing", "Saved to App Library"]
    [item] = facts.items
    assert (item.media_type, item.format, item.codec, item.bitrate) == (
        "video", "mp4", "h264", bitrate)


@pytest.mark.anyio
@pytest.mark.parametrize("companion_codec, merged, copy_audio, bitrate", [
    ("opus", "merged.mkv", False, None),
    ("aac", "merged.mp4", True, 4000.0),
])
async def test_mp4_video_only_merge_then_convert_with_single_post_processing_step(
    tmp_path: Path, companion_codec: str, merged: str, copy_audio: bool,
    bitrate: float | None,
) -> None:
    formats = [
        fmt("v", VIDEO_ONLY, video="h264", bitrate=4000.0),
        fmt("a", AUDIO, audio=companion_codec, bitrate=128.0),
    ]
    run, facts = await execute_run(tmp_path, formats, "v", "mp4")
    assert (facts.record.status, facts.record.final_path) == (
        "completed", f"video/{TITLE}.mp4")
    assert run.media.calls == ["download:raw-v", "download:raw-a", "merge", "convert:mp4"]
    assert run.media.conversions == [(merged, "converted.mp4", copy_audio, True)]
    assert facts.messages == [
        "Download started",
        "Post-processing (merging video and audio, converting to MP4)",
        "Finalizing", "Saved to App Library",
    ]
    [item] = facts.items
    assert (item.codec, item.bitrate) == ("h264", bitrate)


# --- Original regression ------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("profile", [None, "original"])
async def test_original_audio_is_unchanged(tmp_path: Path, profile: str | None) -> None:
    formats = [fmt("a", AUDIO, audio="opus", bitrate=160.0)]
    run, facts = await execute_run(tmp_path, formats, "a", profile)
    assert (facts.record.status, facts.record.final_path) == ("completed", f"audio/{TITLE}.mp4")
    assert (run.library / "audio" / f"{TITLE}.mp4").read_bytes() == b"source"
    assert run.media.calls == ["download:raw-a"] and run.media.conversions == []
    assert facts.messages == ["Download started", "Finalizing", "Saved to App Library"]
    [item] = facts.items
    assert (item.format, item.codec, item.bitrate) == ("mp4", "opus", 160.0)


@pytest.mark.anyio
async def test_original_merge_keeps_merge_only_message_and_source_codec(
    tmp_path: Path,
) -> None:
    formats = [
        fmt("v", VIDEO_ONLY, video="vp9", bitrate=2800.0),
        fmt("a", AUDIO, audio="opus"),
    ]
    run, facts = await execute_run(tmp_path, formats, "v", None)
    assert facts.record.status == "completed"
    assert run.media.calls == ["download:raw-v", "download:raw-a", "merge"]
    assert facts.messages == [
        "Download started", "Post-processing (merging video and audio)",
        "Finalizing", "Saved to App Library",
    ]
    [item] = facts.items
    assert (item.codec, item.bitrate) == ("vp9", 2800.0)


# --- plan validation before any media I/O -----------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("profile, formats, variant, code", [
    ("mp3", [fmt("m", MUXED, video="h264", audio="aac")], "m", "invalid_profile"),
    ("flac", [fmt("v", VIDEO_ONLY, video="h264"), fmt("a", AUDIO, audio="aac")], "v",
     "invalid_profile"),
    ("mp4", [fmt("a", AUDIO, audio="aac")], "a", "invalid_profile"),
    ("mp4", [fmt("v", VIDEO_ONLY, video="h264")], "v", "invalid_variant"),
    ("mp3", [fmt("a", AUDIO, audio="aac")], "missing", "invalid_variant"),
])
async def test_incompatible_plan_fails_before_staging_update_or_media_io(
    tmp_path: Path, profile: str, formats: list[NormalizedFormat], variant: str, code: str,
) -> None:
    run, facts = await execute_run(tmp_path, formats, variant, profile)
    record = facts.record
    assert (record.status, record.error_code, record.output_profile) == (
        "failed", code, profile)
    assert job_error_code_retryable(code) is False
    assert record.staging_path is None and record.final_path is None
    assert run.media.calls == []
    assert not (run.staging / "job").exists()
    assert run.published_files() == []
    assert (facts.items, facts.files) == ([], [])
    assert facts.messages[0] == "Download started" and len(facts.messages) == 2
    assert facts.messages[1].startswith("Failed: ")
    assert [event["event"] for event in run.bus.events][-1] == "job.failed"


# --- FFmpeg failures and cancellation ------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("kind, code, retryable", [
    (FFmpegFailureKind.FAILED, "post_processing_failed", True),
    (FFmpegFailureKind.TIMEOUT, "post_processing_failed", True),
    (FFmpegFailureKind.BINARY_MISSING, "ffmpeg_missing", False),
])
async def test_ffmpeg_failure_maps_code_and_never_publishes_the_original(
    tmp_path: Path, kind: FFmpegFailureKind, code: str, retryable: bool,
) -> None:
    def prepare(run: SingleRun) -> None:
        run.media.convert_error = FFmpegError(kind, "boom")

    formats = [fmt("a", AUDIO, audio="opus", bitrate=160.0)]
    run, facts = await execute_run(tmp_path, formats, "a", "mp3", prepare=prepare)
    record = facts.record
    assert (record.status, record.error_code, record.output_profile) == ("failed", code, "mp3")
    assert job_error_code_retryable(code) is retryable
    assert record.final_path is None and run.media.calls == ["download:raw-a", "convert:mp3"]
    assert run.published_files() == []
    assert (facts.items, facts.files) == ([], [])
    assert facts.messages[-1].startswith("Failed: ")
    assert "Saved to App Library" not in facts.messages
    dto_event = run.bus.events[-1]
    assert dto_event["event"] == "job.failed"
    assert dto_event["data"]["error"]["retryable"] is retryable


@pytest.mark.anyio
async def test_cancel_during_conversion_cleans_staging_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    def prepare(run: SingleRun) -> None:
        def cancel_and_terminate() -> None:
            run.cancel.set()
            run.media.convert_error = FFmpegError(FFmpegFailureKind.FAILED, "terminated")

        run.media.in_convert = cancel_and_terminate

    formats = [fmt("a", AUDIO, audio="opus")]
    run, facts = await execute_run(tmp_path, formats, "a", "mp3", prepare=prepare)
    record = facts.record
    assert (record.status, record.error_code, record.output_profile) == (
        "cancelled", "cancelled", "mp3")
    assert run.media.calls == ["download:raw-a", "convert:mp3"]
    assert not (run.staging / "job").exists()
    assert run.published_files() == []
    assert (facts.items, facts.files) == ([], [])
    assert facts.messages[-1] == "Cancelled"
    assert [event["event"] for event in run.bus.events][-1] == "job.cancelled"


# --- Skip Existing --------------------------------------------------------------------


@pytest.mark.anyio
async def test_skip_existing_with_conversion_keeps_file_and_nulls_codec_and_bitrate(
    tmp_path: Path,
) -> None:
    def prepare(run: SingleRun) -> None:
        (run.library / "audio" / f"{TITLE}.mp3").write_bytes(b"existing")

    formats = [fmt("a", AUDIO, audio="mp3", bitrate=192.0)]
    run, facts = await execute_run(tmp_path, formats, "a", "mp3", prepare=prepare)
    assert (facts.record.status, facts.record.final_path) == ("completed", f"audio/{TITLE}.mp3")
    assert (run.library / "audio" / f"{TITLE}.mp3").read_bytes() == b"existing"
    assert run.media.calls == ["download:raw-a", "convert:mp3"]
    assert facts.messages == [
        "Download started", "Converting to MP3", "Finalizing",
        "A file with this name already exists — kept the existing copy",
    ]
    [item] = facts.items
    [media_file] = facts.files
    assert (item.media_type, item.format, item.codec, item.bitrate) == (
        "audio", "mp3", None, None)
    assert (media_file.path, media_file.size_bytes) == (f"audio/{TITLE}.mp3", len(b"existing"))
    assert not (run.staging / "job").exists()


@pytest.mark.anyio
async def test_skip_existing_original_keeps_source_codec_and_bitrate(tmp_path: Path) -> None:
    def prepare(run: SingleRun) -> None:
        (run.library / "audio" / f"{TITLE}.mp4").write_bytes(b"existing")

    formats = [fmt("a", AUDIO, audio="opus", bitrate=160.0)]
    run, facts = await execute_run(tmp_path, formats, "a", None, prepare=prepare)
    assert facts.record.status == "completed"
    assert (run.library / "audio" / f"{TITLE}.mp4").read_bytes() == b"existing"
    [item] = facts.items
    assert (item.codec, item.bitrate) == ("opus", 160.0)


# --- failed / restart-recovery retry keeps the stored profile --------------------------


@contextmanager
def running_client(
    settings: Settings, analyzer: FakeAnalyzer, *, ytdlp: str, ffmpeg: str,
) -> Generator[TestClient, None, None]:
    overrides: dict[str, Any] = {
        "yt_dlp_binary": ytdlp, "ffmpeg_binary": ffmpeg,
        "queue_poll_interval_seconds": 0.05, "progress_min_interval_seconds": 0.0,
    }
    with TestClient(build_application(settings, analyzer, overrides)) as client:
        yield client


def library_row(settings: Settings) -> tuple[Any, ...]:
    with sqlite3.connect(settings.state_dir / "downloader.db") as connection:
        return connection.execute(
            "SELECT media_type, format, codec, bitrate FROM library_items").fetchone()


def test_api_created_mp3_job_completes_converted(
    settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path,
) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    argv_log = tmp_path / "ffmpeg-argv.log"
    with running_client(
        settings, analyzer, ytdlp=fake_ytdlp_ok(binaries),
        ffmpeg=fake_ffmpeg_record(binaries, argv_log),
    ) as client:
        response = client.post("/api/v1/jobs", json=single_request(
            analyze(client, analyzer, collection=False), AUDIO_VARIANT, output_profile="mp3"))
        assert response.status_code == 200
        job = response.json()
        assert job["output_profile"] == "mp3"
        final = wait_for_job(client, job["id"], statuses={"completed", "failed", "cancelled"})
        assert (final["status"], final["error"]) == ("completed", None)
        assert final["output_profile"] == "mp3"
        assert final["final_path"] == "audio/Test Video.mp3"
    assert (settings.library_dir / "audio" / "Test Video.mp3").read_text() == "converted-media"
    assert "libmp3lame" in argv_log.read_text()
    assert library_row(settings) == ("audio", "mp3", "mp3", None)
    assert scalar(settings, "SELECT output_profile FROM download_jobs") == "mp3"


def test_failed_conversion_retry_completes_with_same_profile(
    settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path,
) -> None:
    good, bad = tmp_path / "bin-good", tmp_path / "bin-bad"
    good.mkdir()
    bad.mkdir()
    argv_log = tmp_path / "ffmpeg-argv.log"
    ytdlp = fake_ytdlp_ok(good)
    with app_client(settings, analyzer, paused=True) as client:
        job = client.post("/api/v1/jobs", json=single_request(
            analyze(client, analyzer, collection=False), AUDIO_VARIANT)).json()
    execute(settings, "UPDATE download_jobs SET output_profile = 'mp3'")

    with running_client(settings, analyzer, ytdlp=ytdlp, ffmpeg=fake_ffmpeg_fail(bad)) as client:
        failed = wait_for_job(client, job["id"], statuses={"completed", "failed", "cancelled"})
        assert (failed["status"], failed["error"]["code"]) == ("failed", "post_processing_failed")
        assert failed["error"]["retryable"] is True and failed["output_profile"] == "mp3"
        assert failed["final_path"] is None
    assert scalar(settings, "SELECT COUNT(*) FROM library_items") == 0
    assert not list(settings.library_dir.rglob("*.*"))

    with running_client(
        settings, analyzer, ytdlp=ytdlp, ffmpeg=fake_ffmpeg_record(good, argv_log),
    ) as client:
        response = client.post(f"/api/v1/jobs/{job['id']}/retry")
        assert response.status_code == 200
        assert response.json()["output_profile"] == "mp3"
        final = wait_for_job(client, job["id"], statuses={"completed", "failed", "cancelled"})
        assert (final["status"], final["error"]) == ("completed", None)
        assert final["output_profile"] == "mp3"
        assert final["final_path"] == "audio/Test Video.mp3"
    assert (settings.library_dir / "audio" / "Test Video.mp3").read_text() == "converted-media"
    assert "libmp3lame" in argv_log.read_text()
    assert library_row(settings) == ("audio", "mp3", "mp3", None)
    assert scalar(settings, "SELECT output_profile FROM download_jobs") == "mp3"


def test_restart_recovery_retry_completes_with_same_profile(
    settings: Settings, analyzer: FakeAnalyzer, tmp_path: Path,
) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    argv_log = tmp_path / "ffmpeg-argv.log"
    with app_client(settings, analyzer, paused=True) as client:
        job = client.post("/api/v1/jobs", json=single_request(
            analyze(client, analyzer, collection=False), AUDIO_VARIANT)).json()
    execute(settings, "UPDATE download_jobs SET status = 'downloading', output_profile = 'flac'")

    with running_client(
        settings, analyzer, ytdlp=fake_ytdlp_ok(binaries),
        ffmpeg=fake_ffmpeg_record(binaries, argv_log),
    ) as client:
        recovered = client.get(f"/api/v1/jobs/{job['id']}").json()
        assert recovered["error"]["code"] == "interrupted_by_restart"
        assert recovered["error"]["retryable"] is True
        assert recovered["output_profile"] == "flac"
        response = client.post(f"/api/v1/jobs/{job['id']}/retry")
        assert response.status_code == 200
        assert response.json()["output_profile"] == "flac"
        final = wait_for_job(client, job["id"], statuses={"completed", "failed", "cancelled"})
        assert (final["status"], final["error"]) == ("completed", None)
        assert final["output_profile"] == "flac"
        assert final["final_path"] == "audio/Test Video.flac"
    assert (settings.library_dir / "audio" / "Test Video.flac").read_text() == "converted-media"
    assert "flac" in argv_log.read_text().split()
    assert library_row(settings) == ("audio", "flac", "flac", None)
