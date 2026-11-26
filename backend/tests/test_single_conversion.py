"""M6.4d-d2: single runner conversion wiring.

The runner is driven directly with stub adapters (no real yt-dlp/ffmpeg); the
API create and retry/recovery tests go through the app with fake binaries.
Runner-level rows are written directly as controlled records.
"""

import asyncio
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Never, cast

import pytest
from fastapi.testclient import TestClient
from mutagen.flac import FLAC
from sqlalchemy import select

from app.config import Settings
from app.domain.analysis import (
    MediaFormatKind,
    NormalizedAnalysis,
    NormalizedFormat,
    SourceMetadata,
)
from app.domain.jobs import JobError, JobErrorCode, job_error_code_retryable
from app.integrations.ffmpeg import (
    ConversionProfile,
    FFmpegError,
    FFmpegFailureCause,
    FFmpegFailureKind,
    VideoDimensions,
)
from app.models.analysis import AnalysisSession
from app.models.jobs import ActivityEvent, DownloadJob
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs as job_repo
from app.services import job_runner
from app.services.job_runner import (
    _REMUX_FALLBACK_SAVED,
    _TAG_FALLBACK_SAVED,
    RunnerDeps,
    run_download_job,
)
from app.services.storage_service import (
    MediaFileMissingError,
    MediaFileTargetChangedError,
    StagedEditResult,
    StoragePathError,
    StorageService,
)
from app.utils.filenames import conflict_media_filenames
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
from tests.test_staged_tag_copy import tiny_flac

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
        self.merges: list[str] = []
        self.convert_error: BaseException | None = None
        self.in_convert: Callable[[], None] | None = None
        self.download_ext = "mp4"
        self.download_bytes = b"source"
        self.convert_bytes = b"converted"
        self.dimensions: VideoDimensions | None = None
        self.probed: list[Path] = []
        self.probe_error: BaseException | None = None
        self.on_probe: Callable[[], None] | None = None

    async def probe_video_dimensions(
        self, media_path: Path, on_process_started: Any = None,
    ) -> VideoDimensions | None:
        self.calls.append("probe")
        self.probed.append(media_path)
        assert media_path.is_file()
        if self.on_probe is not None:
            self.on_probe()
        if self.probe_error is not None:
            raise self.probe_error
        return self.dimensions

    async def download(self, **kwargs: Any) -> list[str]:
        path = Path(kwargs["output_template"].replace("%(ext)s", self.download_ext))
        self.calls.append(f"download:{kwargs['format_selector']}")
        path.write_bytes(self.download_bytes)
        return [str(path)]

    async def merge_streams(
        self, video_path: Path, audio_path: Path, output_path: Path,
        on_process_started: Any = None,
    ) -> None:
        self.calls.append("merge")
        self.merges.append(output_path.name)
        output_path.write_bytes(b"merged")

    async def convert(
        self, input_path: Path, output_path: Path, profile: ConversionProfile, *,
        copy_audio: bool = False, copy_video: bool = False, on_process_started: Any = None,
    ) -> None:
        self.calls.append(f"convert:{profile}")
        self.conversions.append((input_path.name, output_path.name, copy_audio, copy_video))
        output_path.write_bytes(self.convert_bytes)
        if self.in_convert is not None:
            self.in_convert()
        if self.convert_error is not None:
            raise self.convert_error


def is_fresh(name: str, stem: str, extension: str) -> bool:
    """Per-call staging target: ``<stem>-<16 lowercase hex>.<extension>``."""
    return re.fullmatch(rf"{stem}-[0-9a-f]{{16}}\.{re.escape(extension)}", name) is not None


def assert_converted(
    media: StubMedia, source: str, profile: str, copy_audio: bool, copy_video: bool,
) -> None:
    [(input_name, output_name, *flags)] = media.conversions
    assert input_name == source
    assert is_fresh(output_name, "converted", profile), output_name
    assert flags == [copy_audio, copy_video]


class Facts:
    def __init__(
        self, record: DownloadJob, messages: list[str],
        items: list[LibraryItem], files: list[MediaFile],
        warnings: list[ActivityEvent],
    ) -> None:
        self.record = record
        self.messages = messages
        self.items = items
        self.files = files
        self.warnings = warnings


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
        *, legacy_formats: bool = False, source_title: str | None = None,
        metadata: SourceMetadata | None = None,
    ) -> None:
        now = datetime.now(UTC)
        analysis = NormalizedAnalysis(
            analysis_id="analysis", original_url="https://example.com/v",
            detected_platform="generic", detected_platform_label="Generic",
            content_type="single", formats=formats, duration_seconds=60.0,
            title=source_title, source_metadata=metadata,
        )
        # Snapshots persisted before ``h264_profile_idc`` existed lack the key.
        result_json = analysis.model_dump_json(
            exclude={"formats": {"__all__": {"h264_profile_idc"}}} if legacy_formats else None
        )
        assert ("h264_profile_idc" in result_json) is not legacy_formats
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
                status="completed", normalized_result_json=result_json,
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
            warnings = list((await session.scalars(select(ActivityEvent).where(
                ActivityEvent.level == "warning").order_by(ActivityEvent.id))).all())
        return Facts(record, messages, items, files, warnings)

    def published_files(self) -> list[Path]:
        return sorted(path for path in self.library.rglob("*") if path.is_file())


async def execute_run(
    tmp_path: Path, formats: list[NormalizedFormat], variant: str, profile: str | None,
    *, prepare: Callable[[SingleRun], None] | None = None, legacy_formats: bool = False,
    source_title: str | None = None, metadata: SourceMetadata | None = None,
) -> tuple[SingleRun, Facts]:
    run = SingleRun(tmp_path)
    try:
        await run.seed(
            formats, variant, profile, legacy_formats=legacy_formats,
            source_title=source_title, metadata=metadata,
        )
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
    assert_converted(run.media, "media.mp4", profile, copy, False)
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
    run, facts = await execute_run(
        tmp_path, formats, "m", "mp4", prepare=measured(VideoDimensions(1080, 1920)),
    )
    record = facts.record
    # Portrait 1080x1920: the filename carries the measured short edge.
    assert (record.status, record.final_path) == ("completed", f"video/{TITLE} (1080p).mp4")
    assert (run.library / "video" / f"{TITLE} (1080p).mp4").read_bytes() == b"converted"
    assert run.media.calls == ["download:raw-m", "convert:mp4", "probe"]
    assert_converted(run.media, "media.mp4", "mp4", copy_audio, copy_video)
    # The measured staged output (the converted file) is probed, before Finalizing.
    [probed] = run.media.probed
    assert is_fresh(probed.name, "converted", "mp4")
    assert facts.messages == [
        "Download started", "Converting to MP4", "Finalizing", "Saved to App Library"]
    [item] = facts.items
    assert (item.media_type, item.format, item.codec, item.bitrate) == (
        "video", "mp4", "h264", bitrate)
    assert item.resolution == "1080p"  # measured short edge, not the analysis label


@pytest.mark.anyio
@pytest.mark.parametrize("companion_codec, merged_ext, copy_audio, bitrate", [
    ("opus", "mkv", False, None),
    ("aac", "mp4", True, 4000.0),
])
async def test_mp4_video_only_merge_then_convert_with_single_post_processing_step(
    tmp_path: Path, companion_codec: str, merged_ext: str, copy_audio: bool,
    bitrate: float | None,
) -> None:
    formats = [
        fmt("v", VIDEO_ONLY, video="h264", bitrate=4000.0),
        fmt("a", AUDIO, audio=companion_codec, bitrate=128.0),
    ]
    run, facts = await execute_run(tmp_path, formats, "v", "mp4")
    assert (facts.record.status, facts.record.final_path) == (
        "completed", f"video/{TITLE}.mp4")
    assert run.media.calls == [
        "download:raw-v", "download:raw-a", "merge", "convert:mp4", "probe"]
    [merged] = run.media.merges
    assert is_fresh(merged, "merged", merged_ext), merged
    # The conversion reads exactly the file the merge produced.
    assert_converted(run.media, merged, "mp4", copy_audio, True)
    assert facts.messages == [
        "Download started",
        "Post-processing (merging video and audio, converting to MP4)",
        "Finalizing", "Saved to App Library",
    ]
    [item] = facts.items
    assert (item.codec, item.bitrate) == ("h264", bitrate)


@pytest.mark.anyio
@pytest.mark.parametrize("legacy_formats, copy_video, bitrate", [
    (False, False, None),
    (True, True, 1500.0),
])
async def test_mp4_stored_h264_profile_drives_video_copy_and_catalog(
    tmp_path: Path, legacy_formats: bool, copy_video: bool, bitrate: float | None,
) -> None:
    high10 = fmt("m", MUXED, video="h264", audio="aac", bitrate=1500.0).model_copy(
        update={"h264_profile_idc": 110}
    )
    run, facts = await execute_run(
        tmp_path, [high10], "m", "mp4", legacy_formats=legacy_formats,
    )
    assert (facts.record.status, facts.record.final_path) == ("completed", f"video/{TITLE}.mp4")
    assert run.media.calls == ["download:raw-m", "convert:mp4", "probe"]
    assert_converted(run.media, "media.mp4", "mp4", True, copy_video)
    [item] = facts.items
    assert (item.media_type, item.format, item.codec, item.bitrate) == (
        "video", "mp4", "h264", bitrate)


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
    assert run.media.calls == ["download:raw-v", "download:raw-a", "merge", "probe"]
    assert facts.messages == [
        "Download started", "Post-processing (merging video and audio)",
        "Finalizing", "Saved to App Library",
    ]
    [item] = facts.items
    assert (item.codec, item.bitrate) == ("vp9", 2800.0)
    assert item.resolution is None  # unknown measurement: no analysis-label fallback


# --- Measured resolution (M6-Q finding 3a) ----------------------------------------------


def measured(
    dimensions: VideoDimensions | None, **attributes: Any,
) -> Callable[[SingleRun], None]:
    def prepare(run: SingleRun) -> None:
        run.media.dimensions = dimensions
        for name, value in attributes.items():
            setattr(run.media, name, value)

    return prepare


@pytest.mark.anyio
@pytest.mark.parametrize("dimensions, label", [
    (VideoDimensions(1920, 1080), "1080p"),
    (VideoDimensions(1080, 1920), "1080p"),
    (VideoDimensions(640, 360), "360p"),
    (None, None),
])
async def test_original_video_resolution_is_measured_short_edge_or_unknown(
    tmp_path: Path, dimensions: VideoDimensions | None, label: str | None,
) -> None:
    formats = [fmt("m", MUXED, video="h264", audio="aac")]
    formats[0] = formats[0].model_copy(update={"resolution_label": "720p"})
    run, facts = await execute_run(
        tmp_path, formats, "m", None, prepare=measured(dimensions),
    )
    assert facts.record.status == "completed"
    assert run.media.calls == ["download:raw-m", "probe"]
    [probed] = run.media.probed
    assert probed.name == "media.mp4"
    [item] = facts.items
    assert item.resolution == label
    assert facts.messages == ["Download started", "Finalizing", "Saved to App Library"]


@pytest.mark.anyio
@pytest.mark.parametrize("dimensions,name", [
    (VideoDimensions(1280, 720), f"{TITLE} (720p).mp4"),
    (VideoDimensions(1080, 1920), f"{TITLE} (1080p).mp4"),  # portrait: short edge
    (None, f"{TITLE}.mp4"),  # unmeasured: no suffix
])
async def test_single_video_filename_carries_measured_short_edge(
    tmp_path: Path, dimensions: VideoDimensions | None, name: str,
) -> None:
    formats = [fmt("m", MUXED, video="h264", audio="aac")]
    run, facts = await execute_run(tmp_path, formats, "m", None, prepare=measured(dimensions))
    assert (facts.record.status, facts.record.final_path) == ("completed", f"video/{name}")
    assert (run.library / "video" / name).read_bytes() == b"source"


@pytest.mark.anyio
@pytest.mark.parametrize("existing,published", [
    ([f"{TITLE} (1080p).mp4"], f"{TITLE} (1080p) (2).mp4"),
    ([f"{TITLE} (1080p).mp4", f"{TITLE} (1080p) (2).mp4"], f"{TITLE} (1080p) (3).mp4"),
    # A historical suffix-less file is kept (no backfill) and is no conflict.
    ([f"{TITLE}.mp4"], f"{TITLE} (1080p).mp4"),
    # A different measured resolution is a different name.
    ([f"{TITLE} (720p).mp4"], f"{TITLE} (1080p).mp4"),
])
async def test_name_conflict_publishes_new_measured_file_and_keeps_existing(
    tmp_path: Path, existing: list[str], published: str,
) -> None:
    def prepare(run: SingleRun) -> None:
        measured(VideoDimensions(1920, 1080))(run)
        (run.library / "video").mkdir(parents=True, exist_ok=True)
        for name in existing:
            (run.library / "video" / name).write_bytes(b"existing")

    formats = [fmt("m", MUXED, video="h264", audio="aac")]
    run, facts = await execute_run(tmp_path, formats, "m", None, prepare=prepare)
    assert (facts.record.status, facts.record.final_path) == (
        "completed", f"video/{published}")
    assert (run.library / "video" / published).read_bytes() == b"source"
    for name in existing:
        assert (run.library / "video" / name).read_bytes() == b"existing"
    [item] = facts.items
    assert item.resolution == "1080p"  # the new file is the measured one
    assert facts.messages == ["Download started", "Finalizing", "Saved to App Library"]
    assert facts.warnings == []


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
async def test_unsafe_staged_output_is_never_probed(tmp_path: Path, kind: str) -> None:
    formats = [fmt("m", MUXED, video="h264", audio="aac")]
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")

    def prepare(run: SingleRun) -> None:
        measured(VideoDimensions(1920, 1080))(run)
        original = run.media.download

        async def download(**kwargs: Any) -> list[str]:
            [produced] = await original(**kwargs)
            Path(produced).unlink()
            if kind == "symlink":
                Path(produced).symlink_to(outside)
            else:
                os.mkfifo(produced)
            return [produced]

        async def refuse(*args: Any, **kwargs: Any) -> Never:
            raise OSError("placement is not under test")  # a FIFO must never be read

        run.media.download = download  # type: ignore[method-assign]
        run.storage.place_final = refuse  # type: ignore[method-assign]

    run, facts = await execute_run(tmp_path, formats, "m", None, prepare=prepare)
    assert run.media.probed == [] and "probe" not in run.media.calls
    assert facts.record.status == "failed"  # placement behaviour/errors are unchanged
    assert facts.items == [] and outside.read_bytes() == b"outside"


@pytest.mark.anyio
async def test_cancel_during_probe_is_respected_and_keeps_staged_media(
    tmp_path: Path,
) -> None:
    formats = [fmt("m", MUXED, video="h264", audio="aac")]

    def prepare(run: SingleRun) -> None:
        measured(VideoDimensions(1920, 1080), on_probe=run.cancel.set)(run)

    run, facts = await execute_run(tmp_path, formats, "m", None, prepare=prepare)
    assert (facts.record.status, facts.record.error_code) == ("cancelled", "cancelled")
    assert "Finalizing" not in facts.messages
    assert_nothing_published(run, facts)
    assert (run.staging / "job" / "media.mp4").read_bytes() == b"source"


@pytest.mark.anyio
async def test_probe_cancellation_error_propagates_and_keeps_staged_media(
    tmp_path: Path,
) -> None:
    formats = [fmt("m", MUXED, video="h264", audio="aac")]
    run = SingleRun(tmp_path)
    try:
        await run.seed(formats, "m", None)
        run.media.probe_error = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await run.run()
        facts = await run.facts()
    finally:
        await run.engine.dispose()
    assert facts.record.error_code == "interrupted_by_restart"
    assert_nothing_published(run, facts)
    assert (run.staging / "job" / "media.mp4").read_bytes() == b"source"


# --- Original WebM/Opus remux (M6.5b) ----------------------------------------------

REMUX_STATUS = "Post-processing (remuxing audio to .opus)"
OPUS_AUDIO = [fmt("a", AUDIO, audio="opus", bitrate=160.0)]


def exit_status() -> FFmpegError:
    return FFmpegError(
        FFmpegFailureKind.FAILED, "raw stderr /private/secret",
        cause=FFmpegFailureCause.EXIT_STATUS,
    )


def remux_fails(error: BaseException | None) -> Callable[[SingleRun], None]:
    def prepare(run: SingleRun) -> None:
        run.media.download_ext = "webm"
        run.media.convert_error = error

    return prepare


def warning_events(run: SingleRun) -> list[str]:
    return [event["data"]["message"] for event in run.bus.events
            if event["event"] == "activity.created" and event["data"]["level"] == "warning"]


def assert_nothing_published(run: SingleRun, facts: Facts) -> None:
    assert run.published_files() == []
    assert (facts.items, facts.files, facts.warnings) == ([], [], [])
    assert warning_events(run) == []
    assert "Saved to App Library" not in facts.messages


@pytest.mark.anyio
@pytest.mark.parametrize("profile", [None, "original"])
async def test_original_webm_opus_remux_publishes_opus_with_source_catalog(
    tmp_path: Path, profile: str | None,
) -> None:
    sources: list[bytes] = []

    def prepare(run: SingleRun) -> None:
        remux_fails(None)(run)
        run.media.in_convert = lambda: sources.append(
            (run.staging / "job" / "media.webm").read_bytes())

    run, facts = await execute_run(tmp_path, OPUS_AUDIO, "a", profile, prepare=prepare)
    record = facts.record
    assert (record.status, record.error_code, record.final_path) == (
        "completed", None, f"audio/{TITLE}.opus"), record.error_detail
    assert run.published_files() == [run.library / "audio" / f"{TITLE}.opus"]
    assert (run.library / "audio" / f"{TITLE}.opus").read_bytes() == b"converted"
    assert sources == [b"source"]
    assert run.media.calls == ["download:raw-a", "convert:opus"]
    [(input_name, output_name, copy_audio, copy_video)] = run.media.conversions
    assert input_name == "media.webm" and is_fresh(output_name, "remuxed", "opus")
    assert (copy_audio, copy_video) == (True, False)
    assert facts.messages == [
        "Download started", REMUX_STATUS, "Finalizing", "Saved to App Library"]
    assert facts.warnings == [] and warning_events(run) == []
    [item] = facts.items
    [media_file] = facts.files
    assert (item.media_type, item.format, item.codec, item.bitrate) == (
        "audio", "opus", "opus", 160.0)
    assert media_file.path == record.final_path
    assert not (run.staging / "job").exists()
    assert [event["event"] for event in run.bus.events][-1] == "job.completed"


@pytest.mark.anyio
async def test_remux_exit_status_publishes_original_webm_with_one_warning(
    tmp_path: Path,
) -> None:
    run, facts = await execute_run(
        tmp_path, OPUS_AUDIO, "a", None, prepare=remux_fails(exit_status()))
    record = facts.record
    assert (record.status, record.error_code, record.final_path) == (
        "completed", None, f"audio/{TITLE}.webm"), record.error_detail
    assert run.published_files() == [run.library / "audio" / f"{TITLE}.webm"]
    assert (run.library / "audio" / f"{TITLE}.webm").read_bytes() == b"source"
    assert facts.messages == [
        "Download started", REMUX_STATUS, "Finalizing", "Saved to App Library",
        _REMUX_FALLBACK_SAVED,
    ]
    assert "saved the original WebM" in _REMUX_FALLBACK_SAVED
    assert "disk or permission problem inside FFmpeg" in _REMUX_FALLBACK_SAVED
    [warning] = facts.warnings
    assert (warning.message, warning.details_json) == (_REMUX_FALLBACK_SAVED, None)
    assert all("stderr" not in message and "/private" not in message
               for message in facts.messages)
    [item] = facts.items
    assert (item.media_type, item.format, item.codec, item.bitrate) == (
        "audio", "webm", "opus", 160.0)
    # Published once, after commit, before the completion event.
    assert warning_events(run) == [_REMUX_FALLBACK_SAVED]
    names = [event["event"] for event in run.bus.events]
    assert names[-2:] == ["activity.created", "job.completed"]
    assert run.bus.events[-2]["id"] == warning.id
    assert not (run.staging / "job").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("error, code", [
    (FFmpegError(FFmpegFailureKind.BINARY_MISSING), "ffmpeg_missing"),
    (FFmpegError(FFmpegFailureKind.TIMEOUT), "post_processing_failed"),
    (FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.OUTPUT_GUARD),
     "post_processing_failed"),
    (FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.SPAWN),
     "post_processing_failed"),
    (FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.NO_OUTPUT),
     "post_processing_failed"),
    (FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.OUTPUT_UNVERIFIABLE),
     "post_processing_failed"),
    (FFmpegError(FFmpegFailureKind.FAILED), "post_processing_failed"),
    (OSError(28, "No space left on device"), "disk_write_failed"),
    (JobError(JobErrorCode.INTERNAL_ERROR), "internal_error"),
], ids=[
    "binary_missing", "timeout", "output_guard", "spawn", "no_output",
    "output_unverifiable", "unclassified", "oserror", "joberror",
])
async def test_ineligible_remux_failure_fails_job_without_warning(
    tmp_path: Path, error: BaseException, code: str,
) -> None:
    run, facts = await execute_run(tmp_path, OPUS_AUDIO, "a", None, prepare=remux_fails(error))
    assert (facts.record.status, facts.record.error_code) == ("failed", code)
    assert facts.record.final_path is None
    assert_nothing_published(run, facts)
    assert [event["event"] for event in run.bus.events][-1] == "job.failed"


@pytest.mark.anyio
async def test_remux_task_cancellation_is_not_swallowed(tmp_path: Path) -> None:
    run = SingleRun(tmp_path)
    try:
        await run.seed(OPUS_AUDIO, "a", None)
        remux_fails(asyncio.CancelledError())(run)
        with pytest.raises(asyncio.CancelledError):
            await run.run()
        facts = await run.facts()
    finally:
        await run.engine.dispose()
    assert (facts.record.status, facts.record.error_code) == ("failed", "interrupted_by_restart")
    assert_nothing_published(run, facts)


@pytest.mark.anyio
async def test_cancel_during_remux_exit_status_cancels_without_warning(tmp_path: Path) -> None:
    def prepare(run: SingleRun) -> None:
        run.media.download_ext = "webm"

        def cancel_and_terminate() -> None:
            run.cancel.set()
            run.media.convert_error = exit_status()

        run.media.in_convert = cancel_and_terminate

    run, facts = await execute_run(tmp_path, OPUS_AUDIO, "a", None, prepare=prepare)
    assert (facts.record.status, facts.record.error_code) == ("cancelled", "cancelled")
    assert run.media.calls == ["download:raw-a", "convert:opus"]
    assert_nothing_published(run, facts)
    assert facts.messages[-1] == "Cancelled"
    assert [event["event"] for event in run.bus.events][-1] == "job.cancelled"
    # Q2: the downloaded source stays in job staging for an explicit Retry.
    assert (run.staging / "job" / "media.webm").read_bytes() == b"source"


@pytest.mark.anyio
async def test_explicit_opus_exit_status_fails_without_remux_fallback(tmp_path: Path) -> None:
    run, facts = await execute_run(
        tmp_path, OPUS_AUDIO, "a", "opus", prepare=remux_fails(exit_status()))
    assert (facts.record.status, facts.record.error_code) == ("failed", "post_processing_failed")
    assert_converted(run.media, "media.webm", "opus", True, False)
    assert "Converting to OPUS" in facts.messages and REMUX_STATUS not in facts.messages
    assert_nothing_published(run, facts)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["verification", "commit", "placement", "lost_claim"])
async def test_remux_fallback_warning_requires_committed_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    stage_catalog = job_runner.stage_media_catalog_reference
    stage_update = job_repo.stage_job_update_if_status

    async def missing(*args: Any, **kwargs: Any) -> None:
        raise MediaFileMissingError("gone")

    async def stage_then_break_commit(session: Any, *args: Any, **kwargs: Any) -> None:
        await stage_catalog(session, *args, **kwargs)

        async def broken_commit() -> None:
            raise RuntimeError("commit failed")

        monkeypatch.setattr(session, "commit", broken_commit)

    async def lost(session: Any, job_id: str, statuses: Any, values: dict[str, Any]) -> bool:
        # Only the completion claim loses the race; earlier transitions are real.
        if values.get("status") == "completed":
            return False
        return await stage_update(session, job_id, statuses, values)

    async def broken_place(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    def prepare(run: SingleRun) -> None:
        remux_fails(exit_status())(run)
        if failure == "placement":
            monkeypatch.setattr(run.storage, "place_final", broken_place)

    if failure == "verification":
        monkeypatch.setattr(job_runner, "stage_media_catalog_reference", missing)
    elif failure == "commit":
        monkeypatch.setattr(job_runner, "stage_media_catalog_reference", stage_then_break_commit)
    elif failure == "lost_claim":
        monkeypatch.setattr(job_repo, "stage_job_update_if_status", lost)
    run, facts = await execute_run(tmp_path, OPUS_AUDIO, "a", None, prepare=prepare)
    expected = {
        "verification": ("failed", "internal_error"),
        "commit": ("failed", "internal_error"),
        "placement": ("failed", "disk_write_failed"),
        "lost_claim": ("finalizing", None),
    }[failure]
    assert (facts.record.status, facts.record.error_code) == expected
    assert (facts.items, facts.files, facts.warnings) == ([], [], [])
    assert warning_events(run) == []
    assert _REMUX_FALLBACK_SAVED not in facts.messages
    assert "job.completed" not in [event["event"] for event in run.bus.events]


@pytest.mark.anyio
async def test_remux_fallback_name_conflict_saves_numbered_original_with_warning(
    tmp_path: Path,
) -> None:
    def prepare(run: SingleRun) -> None:
        remux_fails(exit_status())(run)
        (run.library / "audio" / f"{TITLE}.webm").write_bytes(b"existing")

    run, facts = await execute_run(tmp_path, OPUS_AUDIO, "a", None, prepare=prepare)
    assert (facts.record.status, facts.record.final_path) == (
        "completed", f"audio/{TITLE} (2).webm")
    assert run.published_files() == sorted([
        run.library / "audio" / f"{TITLE}.webm", run.library / "audio" / f"{TITLE} (2).webm",
    ])
    assert (run.library / "audio" / f"{TITLE}.webm").read_bytes() == b"existing"
    assert (run.library / "audio" / f"{TITLE} (2).webm").read_bytes() != b"existing"
    # The new original really was saved: the remux warning is kept unchanged.
    assert facts.messages == [
        "Download started", REMUX_STATUS, "Finalizing", "Saved to App Library",
        _REMUX_FALLBACK_SAVED]
    assert "disk or permission problem inside FFmpeg" in _REMUX_FALLBACK_SAVED
    assert [warning.message for warning in facts.warnings] == [_REMUX_FALLBACK_SAVED]
    assert warning_events(run) == [_REMUX_FALLBACK_SAVED]
    [item] = facts.items
    assert (item.format, item.codec, item.bitrate) == ("webm", "opus", 160.0)
    assert not (run.staging / "job").exists()


# --- M6.5 text tags: single audio downloads (M6.5-T3) ----------------------------------

METADATA = SourceMetadata(track="Şarkı", artists=["A", "B"], album="Albüm", track_number=3)
FLAC_AUDIO = [fmt("a", AUDIO, audio="flac", bitrate=900.0)]
BROKEN_MP3 = b"not audio at all " * 40
STAGED_FLAC = "media.flac"


def flac_download(data: bytes | None = None) -> Callable[[SingleRun], None]:
    def prepare(run: SingleRun) -> None:
        run.media.download_ext = "flac"
        run.media.download_bytes = tiny_flac() if data is None else data

    return prepare


def spy_tagging(
    calls: list[Path],
    after: Callable[[SingleRun, StagedEditResult | None], None] | None = None,
    results: list[tuple[bool, tuple[int, int]]] | None = None,
) -> Callable[[SingleRun], None]:
    """Wrap ``stage_edited_copy``; ``after`` runs on the loop once it returned/raised.

    ``results`` records each returned selection as ``(edited, identity)``.
    """

    def install(run: SingleRun) -> None:
        original = run.storage.stage_edited_copy

        async def spy(staging_key: str, source: Path, edit: Any) -> StagedEditResult:
            calls.append(source)
            result: StagedEditResult | None = None
            try:
                result = await original(staging_key, source, edit)
                if results is not None:
                    results.append((result.edited, result.identity))
                return result
            finally:
                if after is not None:
                    after(run, result)

        run.storage.stage_edited_copy = spy  # type: ignore[method-assign]

    return install


def both(*steps: Callable[[SingleRun], None]) -> Callable[[SingleRun], None]:
    def prepare(run: SingleRun) -> None:
        for step in steps:
            step(run)

    return prepare


def staged_names(run: SingleRun) -> list[str]:
    return sorted(path.name for path in (run.staging / "job").iterdir())


def inode(path: Path) -> tuple[int, int]:
    entry = os.stat(path, follow_symlinks=False)
    return (entry.st_dev, entry.st_ino)


def assert_failed_unpublished(run: SingleRun, facts: Facts, code: str) -> None:
    """No file, no completion/catalog rows, no warning row or event."""
    assert (facts.record.status, facts.record.error_code) == ("failed", code)
    assert facts.record.final_path is None
    assert_nothing_published(run, facts)
    assert _TAG_FALLBACK_SAVED not in facts.messages
    events = [event["event"] for event in run.bus.events]
    assert "job.completed" not in events and events[-1] == "job.failed"


@pytest.mark.anyio
@pytest.mark.parametrize("metadata, source_title, title", [
    (METADATA, "Source Title", "Şarkı"),
    (None, "  Source Title  ", "Source Title"),  # trimmed source title fallback
])
async def test_single_audio_publishes_verified_tagged_copy(
    tmp_path: Path, metadata: SourceMetadata | None, source_title: str, title: str,
) -> None:
    placed: list[tuple[str, bytes]] = []
    results: list[tuple[bool, tuple[int, int]]] = []

    def prepare(run: SingleRun) -> None:
        flac_download()(run)
        spy_tagging([], results=results)(run)
        original = run.storage.place_final

        async def place(source: Path, **kwargs: Any) -> Any:
            # Placement gets the copy; the prepared file is still untouched.
            placed.append((source.name, (run.staging / "job" / STAGED_FLAC).read_bytes()))
            assert kwargs["source_identity"] is not None
            return await original(source, **kwargs)

        run.storage.place_final = place  # type: ignore[method-assign]

    run, facts = await execute_run(
        tmp_path, FLAC_AUDIO, "a", None, prepare=prepare,
        source_title=source_title, metadata=metadata,
    )
    record = facts.record
    assert (record.status, record.error_code, record.final_path) == (
        "completed", None, f"audio/{TITLE}.flac"), record.error_detail
    published = run.library / "audio" / f"{TITLE}.flac"
    assert run.published_files() == [published]
    [(placed_name, prepared)] = placed
    assert placed_name != STAGED_FLAC and placed_name.endswith(".flac")
    assert prepared == tiny_flac()
    # The published inode is exactly the verified, pinned tagged copy.
    [(edited, identity)] = results
    assert edited and inode(published) == identity
    tagged = FLAC(published)
    assert tagged["TITLE"] == [title]
    if metadata is not None:
        assert (tagged["ARTIST"], tagged["ALBUM"], tagged["TRACKNUMBER"]) == (
            ["A", "B"], ["Albüm"], ["3"])
    assert facts.messages == ["Download started", "Finalizing", "Saved to App Library"]
    assert facts.warnings == [] and warning_events(run) == []
    [item] = facts.items
    [media_file] = facts.files
    assert (item.media_type, item.format, item.codec) == ("audio", "flac", "flac")
    assert (media_file.path, media_file.size_bytes) == (
        record.final_path, published.stat().st_size)
    assert not (run.staging / "job").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("profile, formats, prepare, name, data, codec", [
    # Explicit target: the converted MP3 is kept, never the pre-conversion source.
    ("mp3", [fmt("a", AUDIO, audio="opus", bitrate=160.0)], "broken_mp3", "mp3",
     BROKEN_MP3, "mp3"),
    (None, FLAC_AUDIO, "broken_flac", "flac", b"fLaC", "flac"),
])
async def test_typed_tag_failure_publishes_intact_prepared_file_with_warning(
    tmp_path: Path, profile: str | None, formats: list[NormalizedFormat], prepare: str,
    name: str, data: bytes, codec: str,
) -> None:
    results: list[tuple[bool, tuple[int, int]]] = []
    prepared: list[tuple[int, int]] = []

    def capture(run: SingleRun, result: StagedEditResult | None) -> None:
        assert result is not None
        prepared.append(inode(result.path))

    def setup(run: SingleRun) -> None:
        if prepare == "broken_mp3":
            run.media.convert_bytes = BROKEN_MP3
        else:
            flac_download(b"fLaC")(run)
        spy_tagging([], capture, results)(run)

    run, facts = await execute_run(
        tmp_path, formats, "a", profile, prepare=setup, metadata=METADATA,
    )
    record = facts.record
    assert (record.status, record.error_code, record.final_path) == (
        "completed", None, f"audio/{TITLE}.{name}"), record.error_detail
    published = run.library / "audio" / f"{TITLE}.{name}"
    assert run.published_files() == [published]
    assert published.read_bytes() == data
    # The rechecked prepared inode itself is published, with its own size.
    [(edited, identity)] = results
    assert not edited and prepared == [identity] and inode(published) == identity
    [media_file] = facts.files
    assert media_file.size_bytes == len(data)
    assert facts.messages[-2:] == ["Saved to App Library", _TAG_FALLBACK_SAVED]
    [warning] = facts.warnings
    assert (warning.message, warning.details_json) == (_TAG_FALLBACK_SAVED, None)
    assert "Şarkı" not in _TAG_FALLBACK_SAVED and "/" not in _TAG_FALLBACK_SAVED
    [item] = facts.items
    assert (item.format, item.codec) == (name, codec)
    # Published once, after commit, before the completion event.
    assert warning_events(run) == [_TAG_FALLBACK_SAVED]
    assert [event["event"] for event in run.bus.events][-2:] == [
        "activity.created", "job.completed"]
    assert run.bus.events[-2]["id"] == warning.id
    assert not (run.staging / "job").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("case", [
    "no_tags", "blank_title", "video", "wav", "remux_fallback", "webm_vorbis",
])
async def test_ineligible_downloads_bypass_tagging_without_copy(
    tmp_path: Path, case: str,
) -> None:
    calls: list[Path] = []
    metadata: SourceMetadata | None = METADATA
    source_title: str | None = None
    profile: str | None = None
    formats = FLAC_AUDIO
    steps: list[Callable[[SingleRun], None]] = [spy_tagging(calls), flac_download()]
    if case == "no_tags":
        metadata = None
    elif case == "blank_title":
        metadata, source_title = None, "   "
    elif case == "video":
        formats = [fmt("a", MUXED, video="h264", audio="aac")]
        steps = [spy_tagging(calls)]
    elif case == "wav":
        formats, profile = [fmt("a", AUDIO, audio="aac")], "wav"
        steps = [spy_tagging(calls)]
    elif case == "remux_fallback":
        formats = OPUS_AUDIO
        steps = [spy_tagging(calls), remux_fails(exit_status())]
    else:
        formats = [fmt("a", AUDIO, audio="vorbis")]

        def webm(run: SingleRun) -> None:
            run.media.download_ext = "webm"

        steps = [spy_tagging(calls), webm]
    run, facts = await execute_run(
        tmp_path, formats, "a", profile, prepare=both(*steps),
        source_title=source_title, metadata=metadata,
    )
    assert facts.record.status == "completed", facts.record.error_detail
    assert calls == []
    expected = [_REMUX_FALLBACK_SAVED] if case == "remux_fallback" else []
    assert [warning.message for warning in facts.warnings] == expected
    assert _TAG_FALLBACK_SAVED not in facts.messages
    [published] = run.published_files()
    if case in {"no_tags", "blank_title"}:
        assert published.read_bytes() == tiny_flac()


@pytest.mark.anyio
async def test_unsupported_content_is_published_unchanged_and_silent(tmp_path: Path) -> None:
    calls: list[Path] = []
    foreign = b"OggS\x00\x02 not an m4a"

    def m4a(run: SingleRun) -> None:
        run.media.download_ext = "m4a"
        run.media.download_bytes = foreign

    run, facts = await execute_run(
        tmp_path, [fmt("a", AUDIO, audio="aac")], "a", None,
        prepare=both(spy_tagging(calls), m4a), metadata=METADATA,
    )
    assert (facts.record.status, facts.record.final_path) == (
        "completed", f"audio/{TITLE}.m4a")
    assert len(calls) == 1
    assert (run.library / "audio" / f"{TITLE}.m4a").read_bytes() == foreign
    assert facts.messages == ["Download started", "Finalizing", "Saved to App Library"]
    assert facts.warnings == [] and warning_events(run) == []


@pytest.mark.anyio
@pytest.mark.parametrize("error, code", [
    (OSError(28, "No space left on device"), "disk_write_failed"),
    (MediaFileTargetChangedError("copy"), "internal_error"),
    (StoragePathError("escape"), "internal_error"),
], ids=["oserror", "target_changed", "path"])
async def test_tagging_storage_errors_fail_without_fallback(
    tmp_path: Path, error: Exception, code: str,
) -> None:
    def prepare(run: SingleRun) -> None:
        flac_download()(run)

        async def broken(staging_key: str, source: Path, edit: Any) -> Never:
            raise error

        run.storage.stage_edited_copy = broken  # type: ignore[method-assign]

    run, facts = await execute_run(tmp_path, FLAC_AUDIO, "a", None, prepare=prepare,
                                   metadata=METADATA)
    assert (facts.record.status, facts.record.error_code) == ("failed", code)
    assert_nothing_published(run, facts)
    assert "Finalizing" not in facts.messages
    assert (run.staging / "job" / STAGED_FLAC).read_bytes() == tiny_flac()


@pytest.mark.anyio
async def test_copy_cleanup_error_is_not_an_optional_tag_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(dir_fd: int, name: str, identity: tuple[int, int]) -> None:
        raise PermissionError(13, "cleanup denied")

    monkeypatch.setattr(StorageService, "_remove_own_copy", staticmethod(refuse))
    run, facts = await execute_run(
        tmp_path, FLAC_AUDIO, "a", None, prepare=flac_download(b"fLaC"), metadata=METADATA,
    )
    assert (facts.record.status, facts.record.error_code) == ("failed", "disk_write_failed")
    assert_nothing_published(run, facts)
    assert _TAG_FALLBACK_SAVED not in facts.messages


@pytest.mark.anyio
@pytest.mark.parametrize("data", [None, b"fLaC"], ids=["written", "tag_error"])
async def test_user_cancel_during_tagging_publishes_nothing(
    tmp_path: Path, data: bytes | None,
) -> None:
    calls: list[Path] = []

    def cancel(run: SingleRun, result: StagedEditResult | None) -> None:
        run.cancel.set()

    run, facts = await execute_run(
        tmp_path, FLAC_AUDIO, "a", None,
        prepare=both(flac_download(data), spy_tagging(calls, cancel)), metadata=METADATA,
    )
    assert (facts.record.status, facts.record.error_code) == ("cancelled", "cancelled")
    assert len(calls) == 1
    assert_nothing_published(run, facts)
    assert "Finalizing" not in facts.messages
    assert _TAG_FALLBACK_SAVED not in facts.messages
    # Only the intact prepared file stays for an explicit Retry; no copy is left.
    assert staged_names(run) == [STAGED_FLAC]
    assert (run.staging / "job" / STAGED_FLAC).read_bytes() == (data or tiny_flac())


@pytest.mark.anyio
@pytest.mark.parametrize("data", [None, b"fLaC"], ids=["edited", "original"])
async def test_repeated_task_cancellation_drains_tagging_then_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes | None,
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_editor = job_runner._text_tag_editor

    def blocking_editor(suffix: str, tags: Any, failures: Any) -> Callable[[Any], bool]:
        edit = real_editor(suffix, tags, failures)

        def wrapped(handle: Any) -> bool:
            entered.set()
            release.wait(5)
            try:
                return edit(handle)
            finally:
                finished.set()

        return wrapped

    monkeypatch.setattr(job_runner, "_text_tag_editor", blocking_editor)
    run = SingleRun(tmp_path)
    try:
        await run.seed(FLAC_AUDIO, "a", None, metadata=METADATA)
        flac_download(data)(run)
        task = asyncio.ensure_future(run.run())
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        facts = await run.facts()
    finally:
        release.set()
        await run.engine.dispose()
    assert (facts.record.status, facts.record.error_code) == ("failed", "interrupted_by_restart")
    assert_nothing_published(run, facts)
    assert _TAG_FALLBACK_SAVED not in facts.messages
    assert staged_names(run) == [STAGED_FLAC]
    assert (run.staging / "job" / STAGED_FLAC).read_bytes() == (data or tiny_flac())


@pytest.mark.anyio
@pytest.mark.parametrize("data, edited", [
    (None, True),  # tagged copy replaced
    (b"OggS\x00\x02 not flac", False),  # unsupported: the original replaced
    (b"fLaC", False),  # typed fallback: the original replaced
], ids=["edited_copy", "unsupported", "tag_error"])
async def test_selection_swapped_before_placement_fails_closed(
    tmp_path: Path, data: bytes | None, edited: bool,
) -> None:
    swapped: list[tuple[Path, tuple[int, int]]] = []

    def swap(run: SingleRun, result: StagedEditResult | None) -> None:
        assert result is not None and result.edited is edited
        result.path.unlink()
        result.path.write_bytes(b"impostor")
        # The selected inode is still pinned: the replacement gets another one.
        assert inode(result.path) != result.identity
        swapped.append((result.path, result.identity))

    run, facts = await execute_run(
        tmp_path, FLAC_AUDIO, "a", None,
        prepare=both(flac_download(data), spy_tagging([], swap)), metadata=METADATA,
    )
    assert_failed_unpublished(run, facts, "internal_error")
    [(impostor, _)] = swapped
    assert impostor.read_bytes() == b"impostor"  # unowned: neither published nor unlinked
    if edited:
        assert (run.staging / "job" / STAGED_FLAC).read_bytes() == tiny_flac()


@pytest.mark.anyio
@pytest.mark.parametrize("data", [None, b"fLaC"], ids=["written", "tag_error"])
async def test_source_swapped_during_tagging_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: bytes | None,
) -> None:
    real_editor = job_runner._text_tag_editor
    holder: list[SingleRun] = []

    def swapping_editor(suffix: str, tags: Any, failures: Any) -> Callable[[Any], bool]:
        edit = real_editor(suffix, tags, failures)

        def wrapped(handle: Any) -> bool:
            staged = holder[0].staging / "job" / STAGED_FLAC
            staged.unlink()
            staged.write_bytes(b"impostor")
            return edit(handle)

        return wrapped

    monkeypatch.setattr(job_runner, "_text_tag_editor", swapping_editor)
    run, facts = await execute_run(
        tmp_path, FLAC_AUDIO, "a", None,
        prepare=both(flac_download(data), holder.append), metadata=METADATA,
    )
    assert_failed_unpublished(run, facts, "internal_error")
    assert staged_names(run) == [STAGED_FLAC]  # own copy removed
    assert (run.staging / "job" / STAGED_FLAC).read_bytes() == b"impostor"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["verification", "commit", "placement", "lost_claim"])
async def test_tag_fallback_warning_requires_committed_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    stage_catalog = job_runner.stage_media_catalog_reference
    stage_update = job_repo.stage_job_update_if_status

    async def missing(*args: Any, **kwargs: Any) -> None:
        raise MediaFileMissingError("gone")

    async def stage_then_break_commit(session: Any, *args: Any, **kwargs: Any) -> None:
        await stage_catalog(session, *args, **kwargs)

        async def broken_commit() -> None:
            raise RuntimeError("commit failed")

        monkeypatch.setattr(session, "commit", broken_commit)

    async def lost(session: Any, job_id: str, statuses: Any, values: dict[str, Any]) -> bool:
        if values.get("status") == "completed":
            return False
        return await stage_update(session, job_id, statuses, values)

    async def broken_place(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    def prepare(run: SingleRun) -> None:
        flac_download(b"fLaC")(run)
        if failure == "placement":
            monkeypatch.setattr(run.storage, "place_final", broken_place)

    if failure == "verification":
        monkeypatch.setattr(job_runner, "stage_media_catalog_reference", missing)
    elif failure == "commit":
        monkeypatch.setattr(job_runner, "stage_media_catalog_reference", stage_then_break_commit)
    elif failure == "lost_claim":
        monkeypatch.setattr(job_repo, "stage_job_update_if_status", lost)
    run, facts = await execute_run(
        tmp_path, FLAC_AUDIO, "a", None, prepare=prepare, metadata=METADATA,
    )
    expected = {
        "verification": ("failed", "internal_error"),
        "commit": ("failed", "internal_error"),
        "placement": ("failed", "disk_write_failed"),
        "lost_claim": ("finalizing", None),
    }[failure]
    assert (facts.record.status, facts.record.error_code) == expected
    assert (facts.items, facts.files, facts.warnings) == ([], [], [])
    assert warning_events(run) == []
    assert _TAG_FALLBACK_SAVED not in facts.messages
    assert "job.completed" not in [event["event"] for event in run.bus.events]


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
async def test_cancel_during_conversion_keeps_staging_and_publishes_nothing(
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
    assert (run.staging / "job" / "media.mp4").read_bytes() == b"source"
    assert run.published_files() == []
    assert (facts.items, facts.files) == ([], [])
    assert facts.messages[-1] == "Cancelled"
    assert [event["event"] for event in run.bus.events][-1] == "job.cancelled"


@pytest.mark.anyio
async def test_cancelled_conversion_retry_reuses_staging_and_cleans_on_success(
    tmp_path: Path,
) -> None:
    run = SingleRun(tmp_path)
    try:
        await run.seed([fmt("a", AUDIO, audio="opus")], "a", "mp3")

        def cancel_and_terminate() -> None:
            run.cancel.set()
            run.media.convert_error = FFmpegError(FFmpegFailureKind.FAILED, "terminated")

        run.media.in_convert = cancel_and_terminate
        await run.run()
        cancelled = await run.facts()
        assert cancelled.record.status == "cancelled"
        assert (run.staging / "job" / "media.mp4").exists()
        assert run.published_files() == []
        async with run.sessions() as session:
            assert await job_repo.stage_retry_job(session, "job", "owner")
            await session.commit()
        run.cancel = asyncio.Event()
        run.media.in_convert = None
        run.media.convert_error = None
        await run.run()
        facts = await run.facts()
    finally:
        await run.engine.dispose()
    assert (facts.record.status, facts.record.final_path) == ("completed", f"audio/{TITLE}.mp3")
    assert run.published_files() == [run.library / "audio" / f"{TITLE}.mp3"]
    assert (run.library / "audio" / f"{TITLE}.mp3").read_bytes() == b"converted"
    assert not (run.staging / "job").exists()


# --- Name conflicts ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_name_conflict_with_conversion_publishes_numbered_file_with_its_facts(
    tmp_path: Path,
) -> None:
    def prepare(run: SingleRun) -> None:
        (run.library / "audio" / f"{TITLE}.mp3").write_bytes(b"existing")

    formats = [fmt("a", AUDIO, audio="mp3", bitrate=192.0)]
    run, facts = await execute_run(tmp_path, formats, "a", "mp3", prepare=prepare)
    assert (facts.record.status, facts.record.final_path) == (
        "completed", f"audio/{TITLE} (2).mp3")
    assert (run.library / "audio" / f"{TITLE}.mp3").read_bytes() == b"existing"
    assert (run.library / "audio" / f"{TITLE} (2).mp3").read_bytes() == b"converted"
    assert run.media.calls == ["download:raw-a", "convert:mp3"]
    assert facts.messages == [
        "Download started", "Converting to MP3", "Finalizing", "Saved to App Library",
    ]
    assert facts.warnings == []
    [item] = facts.items
    [media_file] = facts.files
    # The new file's real facts: a pure stream copy keeps the source bitrate.
    assert (item.media_type, item.format, item.codec, item.bitrate) == (
        "audio", "mp3", "mp3", 192.0)
    assert (media_file.path, media_file.size_bytes) == (
        f"audio/{TITLE} (2).mp3", len(b"converted"))
    assert not (run.staging / "job").exists()


@pytest.mark.anyio
async def test_name_conflict_original_audio_publishes_numbered_file(tmp_path: Path) -> None:
    def prepare(run: SingleRun) -> None:
        (run.library / "audio" / f"{TITLE}.mp4").write_bytes(b"existing")

    formats = [fmt("a", AUDIO, audio="opus", bitrate=160.0)]
    run, facts = await execute_run(tmp_path, formats, "a", None, prepare=prepare)
    assert (facts.record.status, facts.record.final_path) == (
        "completed", f"audio/{TITLE} (2).mp4")
    assert (run.library / "audio" / f"{TITLE}.mp4").read_bytes() == b"existing"
    assert (run.library / "audio" / f"{TITLE} (2).mp4").read_bytes() == b"source"
    [item] = facts.items
    assert (item.codec, item.bitrate, item.resolution) == ("opus", 160.0, None)


@pytest.mark.anyio
async def test_target_swap_right_after_publish_creates_no_catalog_and_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_link = os.link

    def link_then_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        real_link(src, dst, **kwargs)
        if not str(dst).startswith("."):  # the published name, not the hidden temp
            published = tmp_path / "library" / "video" / str(dst)
            published.unlink()
            published.write_bytes(b"impostor")

    monkeypatch.setattr(os, "link", link_then_replace)
    formats = [fmt("m", MUXED, video="h264", audio="aac")]
    run, facts = await execute_run(tmp_path, formats, "m", None)
    assert (facts.record.status, facts.record.error_code, facts.record.final_path) == (
        "failed", "internal_error", None)
    assert (facts.items, facts.files) == ([], [])  # the wrong inode is never catalogued
    assert (run.staging / "job" / "media.mp4").read_bytes() == b"source"  # not deleted
    assert "Saved to App Library" not in facts.messages


@pytest.mark.anyio
async def test_counter_exhaustion_fails_disk_write_and_keeps_staged_media(
    tmp_path: Path,
) -> None:
    names = conflict_media_filenames(TITLE, "mp4")

    def prepare(run: SingleRun) -> None:
        for name in names:
            (run.library / "audio" / name).write_bytes(b"existing")

    formats = [fmt("a", AUDIO, audio="opus", bitrate=160.0)]
    run, facts = await execute_run(tmp_path, formats, "a", None, prepare=prepare)
    assert (facts.record.status, facts.record.error_code, facts.record.final_path) == (
        "failed", "disk_write_failed", None)
    assert (facts.items, facts.files) == ([], [])
    assert sorted(p.name for p in (run.library / "audio").iterdir()) == sorted(names)
    assert (run.staging / "job" / "media.mp4").read_bytes() == b"source"


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
