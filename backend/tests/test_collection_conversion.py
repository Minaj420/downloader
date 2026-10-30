"""M6.4d-d3 collection child conversion wiring with a fake converter.

Known profiles that fit the collection mode convert after download/merge with a
single POST_PROCESSING transition. No real FFmpeg/yt-dlp process is started.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.domain.analysis import MediaFormatKind, NormalizedFormat
from app.domain.jobs import CollectionProfile, JobError, JobErrorCode, JobStatus
from app.integrations.ffmpeg import FFmpegError, FFmpegFailureKind
from app.models.jobs import DownloadJob, JobItem
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs
from app.services import collection_child_runner as child_runner
from app.services import collection_parent_runner as parent_runner
from app.services import collection_persistence as persistence
from app.services.collection_child_runner import ChildResult
from app.services.collection_profile_resolver import NativeChildPlan, resolve_native_child
from tests.test_collection_child_runner import Harness
from tests.test_collection_repository import child, parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def media_source(*, video: str = "avc1", audio: str = "mp4a.40.2") -> dict[str, Any]:
    return {"formats": [
        {"format_id": "mux", "ext": "mp4", "height": 360, "vcodec": video, "acodec": audio,
         "tbr": 900},
        {"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": audio, "abr": 128},
        {"format_id": "high", "ext": "mp4", "height": 1080, "vcodec": video, "acodec": "none",
         "tbr": 4000},
    ]}


async def seed(h: Harness, mode: str, output: str | None, *, height: int | None = None,
               status: str = "analyzing", children: list[JobItem] | None = None) -> None:
    async with h.sessions() as session:
        await jobs.stage_collection_job(session, parent(
            status=status, collection_mode=mode, output_profile=output,
            collection_profile_json=json.dumps({"max_height": height}),
        ), children or [child(2, title="Track"), child(9, status="failed")], owner_id="owner")
        await session.commit()


def record_transitions(monkeypatch: pytest.MonkeyPatch) -> list[JobStatus]:
    seen: list[JobStatus] = []
    real = child_runner.stage_child_transition

    async def recording(session: Any, **kwargs: Any) -> bool:
        won = await real(session, **kwargs)
        if won:
            seen.append(kwargs["status"])
        return won

    monkeypatch.setattr(child_runner, "stage_child_transition", recording)
    return seen


async def child_status(h: Harness, child_id: str = "child-2") -> str:
    async with h.sessions() as session:
        return (await session.scalar(select(JobItem.status).where(JobItem.id == child_id))) or ""


async def catalog(h: Harness) -> tuple[LibraryItem | None, list[MediaFile]]:
    async with h.sessions() as session:
        item = await session.get(LibraryItem, "child-2")
        copies = list((await session.scalars(select(MediaFile))).all())
    return item, copies


def published(h: Harness) -> list[Path]:
    return sorted(p for p in h.storage.library_root.rglob("*") if p.is_file())


PIPELINE = [JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.POST_PROCESSING,
            JobStatus.FINALIZING]


@pytest.mark.anyio
@pytest.mark.parametrize("output,audio,copy,codec,keep_bitrate", [
    ("mp3", "mp4a.40.2", False, "mp3", False),
    ("m4a", "mp4a.40.2", True, "aac", True),
    ("opus", "opus", True, "opus", True),
    ("opus", "mp4a.40.2", False, "opus", False),
    ("wav", "mp4a.40.2", False, "pcm_s16le", False),
    ("flac", "mp4a.40.2", False, "flac", False),
])
async def test_audio_conversion_fixed_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str, audio: str, copy: bool,
    codec: str, keep_bitrate: bool,
) -> None:
    h = Harness(tmp_path)
    try:
        await seed(h, "audio", output)
        h.adapters.source = media_source(audio=audio)
        transitions = record_transitions(monkeypatch)
        during: list[str] = []

        async def observe() -> None:
            during.append(await child_status(h))
        h.adapters.on_convert = observe
        assert await h.run() == ChildResult.COMPLETED
        assert h.adapters.calls == ["probe", "audio", "convert"]
        assert h.adapters.conversions == [("media.mp4", f"converted.{output}", output,
                                           copy, False)]
        assert transitions == PIPELINE and during == ["post_processing"]
        item, copies = await catalog(h)
        assert item is not None and [c.path for c in copies] == [f"audio/Track.{output}"]
        assert (item.format, item.codec, item.bitrate, item.media_type) == (
            output, codec, 128 if keep_bitrate else None, "audio")
        assert (h.storage.library_root / "audio" / f"Track.{output}").read_bytes() == (
            b"converted:media")
        assert await child_status(h) == "completed"
        assert not h.processes
        assert not (tmp_path / "staging" / "child-2").exists()
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("height,video,audio,calls,merged,copy", [
    (360, "avc1", "mp4a.40.2", ["probe", "mux", "convert"], "media.mp4", True),
    (360, "vp9", "opus", ["probe", "mux", "convert"], "media.mp4", False),
    (None, "avc1", "mp4a.40.2", ["probe", "high", "audio", "merge", "convert"],
     "merged.mp4", True),
    (None, "vp9", "opus", ["probe", "high", "audio", "merge", "convert"], "merged.mkv", False),
])
async def test_video_mp4_merges_then_converts_and_keeps_numbered_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, height: int | None, video: str,
    audio: str, calls: list[str], merged: str, copy: bool,
) -> None:
    h = Harness(tmp_path)
    try:
        await seed(h, "video", "mp4", height=height)
        h.adapters.source = media_source(video=video, audio=audio)
        selected = resolve_native_child(h.adapters.source, mode="video",
                                        profile=CollectionProfile(max_height=height)).selected
        transitions = record_transitions(monkeypatch)
        during: list[str] = []

        async def observe() -> None:
            during.append(await child_status(h))
        h.adapters.on_convert = observe
        assert await h.run() == ChildResult.COMPLETED
        assert h.adapters.calls == calls
        assert h.adapters.conversions == [(merged, "converted.mp4", "mp4", copy, copy)]
        # One POST_PROCESSING transition covers merge and conversion.
        assert transitions == PIPELINE and during == ["post_processing"]
        item, copies = await catalog(h)
        assert item is not None and [c.path for c in copies] == ["video/02 - Track.mp4"]
        assert (item.format, item.codec, item.media_type) == ("mp4", "h264", "video")
        assert item.bitrate == (selected.bitrate_kbps if copy else None)
        content = (h.storage.library_root / "video" / "02 - Track.mp4").read_bytes()
        assert content == (b"converted:mediamedia" if "merge" in calls else b"converted:media")
        assert not h.processes
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("selected,companion,code", [
    (NormalizedFormat(variant_id="a", format_id="audio", kind=MediaFormatKind.AUDIO_ONLY,
                      audio_codec="aac"), None, JobErrorCode.INVALID_PROFILE),
    (NormalizedFormat(variant_id="v", format_id="high", kind=MediaFormatKind.VIDEO_ONLY,
                      video_codec="h264"), None, JobErrorCode.INVALID_VARIANT),
])
async def test_resolved_source_plan_mismatch_fails_before_download_and_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: NormalizedFormat,
    companion: NormalizedFormat | None, code: JobErrorCode,
) -> None:
    h = Harness(tmp_path)
    try:
        await seed(h, "video", "mp4")
        monkeypatch.setattr(child_runner, "resolve_native_child",
                            lambda *args, **kwargs: NativeChildPlan(selected, companion))
        transitions = record_transitions(monkeypatch)
        with pytest.raises(JobError) as caught:
            await h.run()
        assert caught.value.code == code
        assert h.adapters.calls == ["probe"] and h.adapters.conversions == []
        assert transitions == [JobStatus.ANALYZING]
        assert await child_status(h) == "analyzing"  # caller records the failure
        assert not (tmp_path / "staging" / "child-2").exists()
        assert published(h) == [] and (await catalog(h)) == (None, [])
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", [FFmpegFailureKind.FAILED, FFmpegFailureKind.TIMEOUT])
@pytest.mark.parametrize("mode,output", [("audio", "mp3"), ("video", "mp4")])
async def test_ffmpeg_failure_never_publishes_original(
    tmp_path: Path, kind: FFmpegFailureKind, mode: str, output: str,
) -> None:
    h = Harness(tmp_path)
    try:
        await seed(h, mode, output)

        async def fail() -> None:
            raise FFmpegError(kind, "boom")
        h.adapters.on_convert = fail
        with pytest.raises(JobError) as caught:
            await h.run()
        assert caught.value.code == JobErrorCode.POST_PROCESSING_FAILED
        assert h.adapters.calls[-1] == "convert"
        assert await child_status(h) == "post_processing"
        assert published(h) == [] and (await catalog(h)) == (None, [])
        assert not h.processes
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("mode,output", [("audio", "flac"), ("video", "mp4")])
@pytest.mark.parametrize("origin", ["parent", "caller"])
async def test_blocked_conversion_cancel_and_repeated_cancel_drain(
    tmp_path: Path, mode: str, output: str, origin: str,
) -> None:
    h = Harness(tmp_path)
    task = None
    try:
        await seed(h, mode, output)
        h.adapters.block = "convert"
        task = asyncio.create_task(h.run())
        await asyncio.wait_for(h.adapters.started.wait(), 2)
        assert await child_status(h) == "post_processing"
        if origin == "parent":
            with h.gate:
                h.adapters.cancel.set()
        else:
            task.cancel()
        await asyncio.wait_for(h.adapters.cleanup.wait(), 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        h.adapters.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert h.adapters.reaped and not h.processes
        assert h.adapters.calls[-1] == "convert"
        assert await child_status(h) == "post_processing"
        assert published(h) == [] and (await catalog(h)) == (None, [])
    finally:
        h.adapters.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("origin", ["parent", "caller"])
async def test_cancel_while_conversion_returns_successfully_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str,
) -> None:
    h = Harness(tmp_path)
    task: asyncio.Task[ChildResult] | None = None
    try:
        await seed(h, "audio", "mp3")
        transitions = record_transitions(monkeypatch)

        async def cancel_then_succeed() -> None:
            if origin == "parent":
                h.adapters.cancel.set()
            else:
                assert task is not None
                task.cancel()
                await asyncio.sleep(0)  # The owner observes the caller cancel.
        h.adapters.on_convert = cancel_then_succeed
        task = asyncio.create_task(h.run())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert JobStatus.FINALIZING not in transitions
        assert await child_status(h) == "post_processing"
        assert published(h) == [] and (await catalog(h)) == (None, [])
        assert not h.processes
    finally:
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("output,existing,expected", [
    ("mp3", "Track.mp3", (None, None)),
    ("opus", "Track.opus", (None, None)),
    (None, "Track.mp4", ("aac", 128)),  # Original: unchanged catalog facts
])
async def test_skip_existing_keeps_copy_and_converted_facts_are_unknown(
    tmp_path: Path, output: str | None, existing: str, expected: tuple[Any, Any],
) -> None:
    h = Harness(tmp_path)
    try:
        await seed(h, "audio", output)
        h.adapters.source = media_source(audio="opus" if output == "opus" else "mp4a.40.2")
        kept = h.storage.library_root / "audio" / existing
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_bytes(b"existing")
        assert await h.run() == ChildResult.COMPLETED
        item, copies = await catalog(h)
        assert item is not None and [c.path for c in copies] == [f"audio/{existing}"]
        assert (item.codec, item.bitrate) == expected
        assert kept.read_bytes() == b"existing" and published(h) == [kept]
    finally:
        await h.engine.dispose()


async def _row_facts(h: Harness, child_id: str) -> tuple[Any, ...]:
    async with h.sessions() as session:
        row = await session.get(JobItem, child_id)
        assert row is not None
        return (row.status, row.final_path, row.error_code, row.progress_percent,
                row.completed_at, row.updated_at)


async def _run_parent(h: Harness) -> parent_runner.ParentRunOutcome:
    return await parent_runner.run_collection_parent(
        "parent", "owner", deps=h.deps, cancel_event=h.adapters.cancel,
        processes=h.processes, cancel_gate=h.gate,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
async def test_real_retry_converts_with_same_target_and_keeps_completed_sibling(
    tmp_path: Path, terminal: str,
) -> None:
    h = Harness(tmp_path)
    try:
        await seed(h, "audio", "mp3", status=terminal, children=[
            child(1, title="Kept", status="completed", final_path="audio/Kept.mp3",
                  progress_percent=100.0),
            child(2, title="Track", status=terminal,
                  error_code="network_error" if terminal == "failed" else "cancelled"),
        ])
        kept = await _row_facts(h, "child-1")
        async with h.sessions() as session:
            assert await persistence.stage_collection_retry(
                session, job_id="parent", owner_id="owner")
            await session.commit()
        assert await _run_parent(h) == parent_runner.ParentRunOutcome.EXHAUSTED
        assert h.adapters.calls == ["probe", "audio", "convert"]
        assert [c[2] for c in h.adapters.conversions] == ["mp3"]
        async with h.sessions() as session:
            record = await session.get(DownloadJob, "parent")
            assert record is not None
            assert (record.status, record.output_profile, record.completed_items) == (
                "completed", "mp3", 2)
        assert await _row_facts(h, "child-1") == kept
        item, copies = await catalog(h)
        assert item is not None and (item.codec, item.format) == ("mp3", "mp3")
        assert [c.path for c in copies] == ["audio/Track.mp3"]
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_recovery_then_retry_converts_interrupted_child_with_same_target(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    try:
        await seed(h, "video", "mp4", status="downloading", children=[
            child(1, title="Kept", status="completed", final_path="video/01 - Kept.mp4",
                  progress_percent=100.0),
            child(2, title="Track", status="post_processing"),
        ])
        kept = await _row_facts(h, "child-1")
        async with h.sessions() as session:
            assert await persistence.stage_collection_startup_recovery(
                session, job_id="parent", owner_id="owner",
            ) == persistence.CollectionRecoveryOutcome.STAGED
            await session.commit()
        async with h.sessions() as session:
            row = await session.get(JobItem, "child-2")
            assert row is not None
            assert (row.status, row.error_code) == ("failed", "interrupted_by_restart")
            assert await persistence.stage_collection_retry(
                session, job_id="parent", owner_id="owner")
            await session.commit()
        assert await _run_parent(h) == parent_runner.ParentRunOutcome.EXHAUSTED
        assert h.adapters.calls == ["probe", "high", "audio", "merge", "convert"]
        async with h.sessions() as session:
            record = await session.get(DownloadJob, "parent")
            assert record is not None
            assert (record.status, record.output_profile) == ("completed", "mp4")
        assert await _row_facts(h, "child-1") == kept
        item, copies = await catalog(h)
        assert item is not None and (item.codec, item.format) == ("h264", "mp4")
        assert [c.path for c in copies] == ["video/02 - Track.mp4"]
    finally:
        await h.engine.dispose()
