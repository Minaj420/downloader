import asyncio
import json
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest
from mutagen.flac import FLAC
from sqlalchemy import select, update

from app.domain.jobs import CollectionProfile, JobError, JobErrorCode, JobStatus
from app.integrations.ffmpeg import (
    ConversionProfile,
    FFmpegError,
    FFmpegFailureCause,
    FFmpegFailureKind,
    FFmpegProcessor,
    VideoDimensions,
)
from app.integrations.mutagen_tags import TagError
from app.integrations.ytdlp import DownloadProgress, YtDlpAnalyzer, YtDlpDownloader
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs
from app.services import collection_child_runner as child_runner
from app.services import storage_service
from app.services.collection_child_runner import ChildResult, ChildRunnerDeps, run_collection_child
from app.services.collection_persistence import (
    CollectionCancelRequestOutcome,
    stage_collection_cancel_request,
    stage_collection_retry,
)
from app.services.job_runner import _REMUX_FALLBACK_SAVED, _TAG_FALLBACK_SAVED
from app.services.storage_service import (
    MediaFileTargetChangedError,
    StagedEditResult,
    StoragePathError,
    StorageService,
)
from app.services.text_tags import TextTags
from tests.test_collection_repository import child, factory, parent
from tests.test_prepared_media import source
from tests.test_staged_tag_copy import tiny_flac


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Adapters(YtDlpAnalyzer, YtDlpDownloader, FFmpegProcessor):
    def __init__(self) -> None:
        self.source = source()
        self.calls: list[str] = []
        self.urls: list[str] = []
        self.callbacks: list[Any] = []
        self.cancel = asyncio.Event()
        self.block = ""
        self.started = asyncio.Event()
        self.cleanup = asyncio.Event()
        self.release = asyncio.Event()
        self.reaped = False
        self.cancel_at = ""
        self.killed = False
        self.after_probe: Any = None
        self.on_convert: Any = None
        self.conversions: list[tuple[str, str, str, bool, bool]] = []
        self.download_ext = "mp4"
        self.convert_error: BaseException | None = None
        # ``None``: ``b"converted:" + input``; otherwise the exact output bytes.
        self.convert_bytes: bytes | None = None
        self.dimensions: VideoDimensions | None = None
        self.probed: list[Path] = []

    async def probe_video_dimensions(
        self, media_path: Path, on_process_started: Any = None,
    ) -> VideoDimensions | None:
        # Fake probe: never runs ffprobe; the staged file must exist untouched.
        self.probed.append(media_path)
        assert media_path.is_file()
        on_process_started(cast(asyncio.subprocess.Process, self))
        # Only recorded in ``calls`` when a test cancels/blocks at this step.
        if "dimensions" in (self.cancel_at, self.block):
            await self.step("dimensions")
        return self.dimensions

    async def step(self, name: str) -> None:
        self.calls.append(name)
        if self.cancel_at == name:
            self.cancel.set()
        if self.block == name:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cleanup.set()
                await self.release.wait()
                self.reaped = True

    async def probe_child(self, url: str) -> dict[str, Any]:
        self.urls.append(url)
        await self.step("probe")
        if self.after_probe:
            await self.after_probe()
        return self.source

    async def download(self, **kwargs: Any) -> list[str]:
        assert kwargs["child_output"] is True
        self.urls.append(kwargs["url"])
        process = cast(asyncio.subprocess.Process, self)
        if self.cancel_at == "registration":
            self.cancel.set()
        kwargs["on_process_started"](process)
        self.callbacks.append(kwargs["on_progress"])
        kwargs["on_progress"](DownloadProgress(3, 10, None, 8, 2))
        await self.step(kwargs["format_selector"])
        path = Path(kwargs["output_template"].replace("%(ext)s", self.download_ext))
        path.write_bytes(b"media")
        return [str(path)]

    returncode: int | None = 0

    def kill(self) -> None:
        self.killed = True

    async def merge_streams(self, video: Path, audio: Path, output: Path,
                            on_process_started: Any = None) -> None:
        on_process_started(cast(asyncio.subprocess.Process, self))
        await self.step("merge")
        output.write_bytes(video.read_bytes() + audio.read_bytes())

    async def convert(self, input_path: Path, output_path: Path, profile: ConversionProfile, *,
                      copy_audio: bool = False, copy_video: bool = False,
                      on_process_started: Any = None) -> None:
        # Fake converter: never runs FFmpeg; only conversion tests reach it.
        self.conversions.append((input_path.name, output_path.name, profile,
                                 copy_audio, copy_video))
        on_process_started(cast(asyncio.subprocess.Process, self))
        await self.step("convert")
        if self.on_convert:
            await self.on_convert()
        if self.convert_error is not None:
            raise self.convert_error
        output_path.write_bytes(self.convert_bytes if self.convert_bytes is not None
                                else b"converted:" + input_path.read_bytes())


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.engine, self.sessions = factory(tmp_path)
        self.adapters = Adapters()
        self.storage = StorageService(tmp_path / "staging", tmp_path / "library")
        self.storage.ensure_layout()
        self.storage.initialize_delete_roots()
        self.deps = ChildRunnerDeps(self.sessions, self.adapters, self.adapters,
                                    self.adapters, self.storage)
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.gate = threading.Lock()

    async def seed(self, mode: str = "video", height: int | None = None,
                   output: str | None = None, **values: Any) -> None:
        async with self.sessions() as session:
            await jobs.stage_collection_job(session, parent(
                    status="analyzing", collection_mode=mode, output_profile=output,
                    collection_profile_json=json.dumps({"max_height": height}),
                    variant_id="NOT-A-CHILD-SELECTOR",
                ), [child(2, title="Track", **values), child(9, status="failed")], owner_id="owner")
            await session.commit()

    async def run(self, owner_id: str = "owner", child_id: str = "child-2",
                  committed: list[ActivityEvent] | None = None) -> ChildResult:
        return await run_collection_child(
            "parent", owner_id, child_id, deps=self.deps, cancel_event=self.adapters.cancel,
            processes=self.processes, cancel_gate=self.gate, committed_activities=committed,
        )

    async def warnings(self) -> list[ActivityEvent]:
        async with self.sessions() as session:
            return list((await session.scalars(
                select(ActivityEvent).where(ActivityEvent.level == "warning")
                .order_by(ActivityEvent.id))).all())

    async def facts(self) -> tuple[Any, ...]:
        async with self.sessions() as session:
            row = await session.get(JobItem, "child-2")
            record = await session.get(DownloadJob, "parent")
            item = await session.get(LibraryItem, "child-2")
            copies = (await session.scalars(select(MediaFile))).all()
            assert row and record
            return (row.status, record.status, record.completed_items, record.failed_items,
                    record.progress_percent, item, copies)


@pytest.mark.anyio
@pytest.mark.parametrize("mode,height,codec,audio,selectors,container", [
    ("audio", None, "avc1", "mp4a.40.2", ["audio"], "mp4"),
    ("video", 360, "avc1", "mp4a.40.2", ["mux"], "mp4"),
    ("video", 1080, "avc1", "mp4a.40.2", ["low", "audio", "merge"], "mp4"),
    ("video", None, "vp9", "opus", ["high", "audio", "merge"], "mkv"),
])
async def test_native_completion_catalog_and_counters(
    tmp_path: Path, mode: str, height: int | None, codec: str, audio: str,
    selectors: list[str], container: str,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed(mode, height)
        h.adapters.source = source(video_codec=codec, audio_codec=audio)
        assert await h.run() == ChildResult.COMPLETED
        assert h.adapters.calls == ["probe", *selectors]
        assert h.adapters.conversions == []  # Original/NULL never converts.
        assert set(h.adapters.urls) == {"https://example.com/2"}
        facts = await h.facts()
        assert facts[:5] == ("completed", "analyzing", 1, 1, 50)
        item, copies = facts[5:]
        assert (item.id, item.job_id, item.owner_id, item.title, item.format) == (
            "child-2", "parent", "owner", "Track", container,
        )
        assert item.codec == ("aac" if mode == "audio" else "vp9" if codec == "vp9" else "h264")
        assert len(copies) == 1 and copies[0].size_bytes == (10 if "merge" in selectors else 5)
        assert await h.run() == ChildResult.SKIPPED
        for callback in h.adapters.callbacks:
            callback(DownloadProgress(99, 100, None, None, None))
        await asyncio.sleep(0)
        assert (await h.facts())[:5] == facts[:5]
        assert not h.processes
        assert not (tmp_path / "staging" / "child-2").exists()
    finally:
        await h.engine.dispose()


def portrait_source() -> dict[str, Any]:
    return {"formats": [
        {"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2"},
        {"format_id": "portrait", "ext": "mp4", "width": 1080, "height": 1920,
         "vcodec": "avc1", "acodec": "none"},
        {"format_id": "tall", "ext": "mp4", "width": 2160, "height": 3840,
         "vcodec": "avc1", "acodec": "none"},
    ]}


@pytest.mark.anyio
@pytest.mark.parametrize("height,portrait,selectors", [
    # Below every source: same actual selector as uncapped Best Available.
    (240, False, ["high", "audio", "merge"]),
    (None, False, ["high", "audio", "merge"]),
    (1080, True, ["portrait", "audio", "merge"]),
    (1440, True, ["portrait", "audio", "merge"]),
    (2160, True, ["tall", "audio", "merge"]),
])
async def test_short_edge_ceiling_drives_actual_download_selector(
    tmp_path: Path, height: int | None, portrait: bool, selectors: list[str],
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("video", height)
        if portrait:
            h.adapters.source = portrait_source()
        assert await h.run() == ChildResult.COMPLETED
        assert h.adapters.calls == ["probe", *selectors]
        assert h.adapters.conversions == []
        assert (await h.facts())[:3] == ("completed", "analyzing", 1)
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("height", [None, 1080])
async def test_no_eligible_video_candidate_still_fails_before_download(
    tmp_path: Path, height: int | None,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("video", height)
        h.adapters.source = {"formats": [
            {"format_id": "silent", "ext": "mp4", "width": 1280, "height": 720,
             "vcodec": "avc1", "acodec": "none"},
        ]}
        with pytest.raises(JobError) as caught:
            await h.run()
        assert caught.value.code == JobErrorCode.INVALID_VARIANT
        assert h.adapters.calls == ["probe"]
        facts = await h.facts()
        assert facts[2] == 0 and facts[5] is None and facts[6] == []
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_retry_reuses_persisted_profile_and_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("video", 1080)
        resolved: list[tuple[CollectionProfile | None, str]] = []
        real_resolve = child_runner.resolve_native_child

        def spy(source: dict[str, Any], *, mode: str,
                profile: CollectionProfile | None = None) -> Any:
            plan = real_resolve(source, mode=mode, profile=profile)
            resolved.append((profile, plan.selected.format_id))
            return plan

        monkeypatch.setattr(child_runner, "resolve_native_child", spy)
        real_download = h.adapters.download
        selectors: list[str] = []

        async def download(**kwargs: Any) -> list[str]:
            selectors.append(kwargs["format_selector"])
            if len(selectors) == 1:
                raise JobError(JobErrorCode.NETWORK_ERROR)
            return await real_download(**kwargs)

        h.adapters.download = download  # type: ignore[method-assign]
        with pytest.raises(JobError):
            await h.run()
        # Caller settlement, then the real collection retry reset and a claim.
        async with h.sessions() as session:
            await session.execute(update(JobItem).where(JobItem.id == "child-2").values(
                status="failed", error_code="network_error"))
            await session.execute(update(DownloadJob).values(status="failed"))
            await session.commit()
        async with h.sessions() as session:
            assert await stage_collection_retry(session, job_id="parent", owner_id="owner")
            await session.commit()
        async with h.sessions() as session:
            await session.execute(update(DownloadJob).values(status="analyzing"))
            await session.commit()
        assert await h.run() == ChildResult.COMPLETED
        expected = CollectionProfile(max_height=1080)
        assert resolved == [(expected, "low"), (expected, "low")]
        assert selectors == ["low", "low", "audio"]
        async with h.sessions() as session:
            record = await session.get(DownloadJob, "parent")
            assert record is not None
            assert CollectionProfile.model_validate_json(
                record.collection_profile_json or "") == expected
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_completion_catalog_populates_probe_source_facts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        h.adapters.source = source() | {"extractor_key": "Soundcloud", "duration": 215.5}
        assert await h.run() == ChildResult.COMPLETED
        item, copies = (await h.facts())[5:]
        assert item is not None and len(copies) == 1
        assert (item.platform, item.duration) == ("soundcloud", 215.5)
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("mode,missing_title,expected", [
    ("video", False, "video/02 - Track.mp4"),
    ("video", True, "video/02 - Untitled.mp4"),
    ("audio", False, "audio/Track.mp4"),
    ("audio", True, "audio/Untitled.mp4"),
])
async def test_completion_filename_uses_persisted_order_for_video_only(
    tmp_path: Path, mode: str, missing_title: bool, expected: str,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed(mode, playlist_index=40, track_number=41)
        if missing_title:
            async with h.sessions() as session:
                await session.execute(
                    update(JobItem).where(JobItem.id == "child-2").values(title=None))
                await session.commit()
        # Fresh probe metadata is never a naming source.
        h.adapters.source = source() | {
            "title": "Probe Title", "playlist_index": 5, "track_number": 6,
        }
        assert await h.run() == ChildResult.COMPLETED
        item, copies = (await h.facts())[5:]
        assert [c.path for c in copies] == [expected]
        assert (h.storage.library_root / expected).read_bytes()
        assert item.title == ("Untitled" if missing_title else "Track")
        async with h.sessions() as session:
            row = await session.get(JobItem, "child-2")
            assert row is not None
            assert (row.source_order, row.title, row.playlist_index) == (
                2, None if missing_title else "Track", 40)
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("raw,expected", [
    (0, 0.0), (0.0, 0.0), (215, 215.0), (215.5, 215.5), (1e308, 1e308),
    (True, None), (False, None), ("204", None), (None, None), ([124], None),
    ({"seconds": 1}, None), (-1, None), (-0.5, None), (float("nan"), None),
    (float("inf"), None), (float("-inf"), None), (10**400, None),
])
async def test_completion_duration_normalization_keeps_real_facts_only(
    tmp_path: Path, raw: object, expected: float | None,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        h.adapters.source = source() | {"extractor_key": "Soundcloud", "duration": raw}
        assert await h.run() == ChildResult.COMPLETED
        item, _ = (await h.facts())[5:]
        assert item is not None
        # Malformed durations become unknown without blocking completion,
        # and the independent platform fact still reaches the catalog.
        assert item.duration == expected
        assert item.platform == "soundcloud"
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("probe,child_url,expected", [
    ({"extractor_key": "Youtube"}, "https://example.com/2", "youtube"),
    ({"extractor": "SoundCloud"}, "https://example.com/2", "soundcloud"),
    ({}, "https://music.youtube.com/watch?v=abc", "youtube_music"),
    ({"extractor_key": "Generic"}, "https://example.com/2", "other"),
])
async def test_completion_platform_uses_existing_detection_contract(
    tmp_path: Path, probe: dict[str, Any], child_url: str, expected: str,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio", source_url=child_url)
        h.adapters.source = source() | probe
        assert await h.run() == ChildResult.COMPLETED
        assert h.adapters.urls[0] == child_url
        item, _ = (await h.facts())[5:]
        assert item is not None and item.platform == expected
        assert item.duration is None
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["copy", "gate", "published", "cleanup"])
async def test_caller_cancel_linearizes_publish_and_drains_owned_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    h = Harness(tmp_path)
    loop = asyncio.get_running_loop()
    blocked = asyncio.Event()
    signal_waiting = asyncio.Event()
    signal_applied = asyncio.Event()
    release = threading.Event()
    storage_finished = threading.Event()
    storage_thread: list[int] = []
    signal_threads: list[int] = []
    predicates: list[Any] = []
    task = None

    def pause() -> None:
        loop.call_soon_threadsafe(blocked.set)
        assert release.wait(5), "test did not release storage worker"

    class Gate:
        def __enter__(self) -> None:
            is_storage = threading.get_ident() == storage_thread[0]
            if not is_storage:
                signal_threads.append(threading.get_ident())
                loop.call_soon_threadsafe(signal_waiting.set)
            h.gate.acquire()
            if is_storage and phase == "gate":
                pause()  # Storage owns the gate, before checking its predicate.

        def __exit__(self, *args: Any) -> None:
            is_storage = threading.get_ident() == storage_thread[0]
            h.gate.release()
            if not is_storage:
                loop.call_soon_threadsafe(signal_applied.set)

    real_place = h.storage._place_child_final_sync
    real_copy = shutil.copyfileobj
    real_link = os.link

    def place(*args: Any, **kwargs: Any) -> Any:
        storage_thread.append(threading.get_ident())
        predicates.append(args[4])
        try:
            result = real_place(*args, **kwargs)
            if phase == "cleanup":
                pause()  # Gate released, but the owned placement has not returned.
            return result
        finally:
            storage_finished.set()

    def copy(original: Any, copied: Any) -> None:
        real_copy(original, copied)
        if phase == "copy":
            pause()

    def link(src: Any, dst: Any, **kwargs: Any) -> None:
        real_link(src, dst, **kwargs)
        if phase == "published" and not str(dst).startswith("."):
            pause()  # Target visible; storage still owns the publish gate.

    async def loop_turn() -> None:
        responsive = loop.create_future()
        loop.call_soon(responsive.set_result, None)
        await asyncio.wait_for(responsive, 2)

    monkeypatch.setattr(h.storage, "_place_child_final_sync", place)
    monkeypatch.setattr(shutil, "copyfileobj", copy)
    monkeypatch.setattr(os, "link", link)
    try:
        await h.seed("audio")
        previous_tasks = asyncio.all_tasks()
        task = asyncio.create_task(run_collection_child(
            "parent", "owner", "child-2", deps=h.deps, cancel_event=h.adapters.cancel,
            processes=h.processes, cancel_gate=cast(threading.Lock, Gate()),
        ))
        await asyncio.wait_for(blocked.wait(), 2)
        task.cancel()
        await asyncio.wait_for(signal_waiting.wait(), 2)
        for _ in range(2):
            task.cancel()
            await loop_turn()  # Deterministic responsiveness/repeated-cancel checkpoint.
        assert not task.done()
        if phase in {"gate", "published"}:
            assert not signal_applied.is_set()
            assert not predicates[0]()  # Signal cannot change outside the held gate.
        else:
            await asyncio.wait_for(signal_applied.wait(), 2)
            assert predicates[0]()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert storage_finished.is_set() and signal_applied.is_set()
        assert len(signal_threads) == 1
        assert not h.processes
        assert not (asyncio.all_tasks() - previous_tasks)
        facts = await h.facts()
        assert facts[:5] == ("finalizing", "analyzing", 0, 1, 0)
        assert facts[5] is None and facts[6] == []
        staged = list((tmp_path / "staging" / "child-2").iterdir())
        targets = list((h.storage.library_root / "audio").iterdir())
        if phase == "copy":
            assert not targets and len(staged) == 1
            assert staged[0].read_bytes() == b"media"
        else:
            assert len(targets) == 1 and targets[0].read_bytes() == b"media"
            assert not staged
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("mismatch", ["owner", "id", "type", "parent_status", "completed",
                                      "stale", "child_parent"])
async def test_ineligible_skips_before_probe(tmp_path: Path, mismatch: str) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed(status="completed" if mismatch == "completed" else
                     "downloading" if mismatch == "stale" else "queued")
        async with h.sessions() as session:
            if mismatch == "type":
                await session.execute(update(DownloadJob).values(job_type="single"))
            if mismatch == "parent_status":
                await session.execute(update(DownloadJob).values(status="queued"))
            if mismatch == "child_parent":
                await jobs.stage_collection_job(session, parent("other"), [], owner_id="owner")
                await session.execute(update(JobItem).values(job_id="other"))
            await session.commit()
        assert await h.run("other" if mismatch == "owner" else "owner",
                           "missing" if mismatch == "id" else "child-2") == ChildResult.SKIPPED
        assert not h.adapters.calls
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("step", ["probe", "high", "merge", "dimensions"])
@pytest.mark.parametrize("origin", ["parent", "caller"])
async def test_blocked_step_parent_cancel_and_double_cancel_drain(
    tmp_path: Path, step: str, origin: str,
) -> None:
    h = Harness(tmp_path)
    task = None
    try:
        await h.seed()
        h.adapters.block = step
        task = asyncio.create_task(h.run())
        await asyncio.wait_for(h.adapters.started.wait(), 2)
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
        assert h.adapters.calls[-1] == step
        assert (await h.facts())[2] == 0
        assert (await h.facts())[5] is None
    finally:
        h.adapters.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("at,expected", [("probe", ["probe"]),
    ("high", ["probe", "high"]), ("audio", ["probe", "high", "audio"]),
    ("merge", ["probe", "high", "audio", "merge"]),
    ("dimensions", ["probe", "high", "audio", "merge", "dimensions"]),
    ("registration", ["probe", "high"])])
async def test_cancel_blocks_next_step(tmp_path: Path, at: str, expected: list[str]) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed()
        h.adapters.cancel_at = at
        h.adapters.dimensions = VideoDimensions(1920, 1080)
        if at == "registration":
            h.adapters.returncode = None
        with pytest.raises(asyncio.CancelledError):
            await h.run()
        assert h.adapters.calls == expected
        if at == "registration":
            assert h.adapters.killed
        if at == "dimensions":
            # Cancelled measurement never discards the staged media nor publishes.
            [probed] = h.adapters.probed
            assert probed.is_file() and probed.parent == tmp_path / "staging" / "child-2"
            assert not (tmp_path / "library").exists() or not list(
                (tmp_path / "library").rglob("*.mp4"))
        assert (await h.facts())[2] == 0
        assert (await h.facts())[5] is None
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("dimensions,label", [
    (VideoDimensions(1920, 1080), "1080p"),
    (VideoDimensions(1080, 1920), "1080p"),
    (VideoDimensions(1280, 720), "720p"),
    (None, None),
])
async def test_video_child_catalog_resolution_is_measured_short_edge_or_unknown(
    tmp_path: Path, dimensions: VideoDimensions | None, label: str | None,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("video")
        h.adapters.dimensions = dimensions
        assert await h.run() == ChildResult.COMPLETED
        [probed] = h.adapters.probed
        assert probed.name.startswith("merged-")  # the staged final output
        item, copies = (await h.facts())[5:]
        assert item is not None and item.resolution == label
        # Same measured short edge in the filename; collection order kept;
        # unmeasured: no suffix.
        qualifier = "" if label is None else f" ({label})"
        assert [c.path for c in copies] == [f"video/02 - Track{qualifier}{probed.suffix}"]
        assert (h.storage.library_root / copies[0].path).is_file()
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_video_child_name_conflict_publishes_numbered_measured_file(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("video", 360)  # muxed: the download is the final output
        h.adapters.dimensions = VideoDimensions(1280, 720)
        folder = h.storage.library_root / "video"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "02 - Track (720p).mp4").write_bytes(b"older 720p")
        (folder / "02 - Track.mp4").write_bytes(b"legacy unmeasured")
        assert await h.run() == ChildResult.COMPLETED
        item, copies = (await h.facts())[5:]
        assert item is not None and item.resolution == "720p"
        assert [c.path for c in copies] == ["video/02 - Track (720p) (2).mp4"]
        assert (folder / "02 - Track (720p) (2).mp4").read_bytes()
        assert (folder / "02 - Track (720p).mp4").read_bytes() == b"older 720p"
        assert (folder / "02 - Track.mp4").read_bytes() == b"legacy unmeasured"
        assert await h.warnings() == []
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_video_child_converted_output_is_the_measured_file(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("video", 360, output="mp4")
        h.adapters.dimensions = VideoDimensions(640, 360)
        assert await h.run() == ChildResult.COMPLETED
        [probed] = h.adapters.probed
        assert probed.name.startswith("converted-") and probed.suffix == ".mp4"
        item = (await h.facts())[5]
        assert item is not None and item.resolution == "360p"
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
async def test_unsafe_staged_child_output_is_never_probed(tmp_path: Path, kind: str) -> None:
    h = Harness(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    try:
        await h.seed("video", 360)  # muxed: the download is the final output
        h.adapters.dimensions = VideoDimensions(1920, 1080)
        original = h.adapters.download

        async def download(**kwargs: Any) -> list[str]:
            [produced] = await original(**kwargs)
            Path(produced).unlink()
            if kind == "symlink":
                Path(produced).symlink_to(outside)
            else:
                os.mkfifo(produced)
            return [produced]

        h.adapters.download = download  # type: ignore[method-assign]
        # Placement keeps its own typed rejection (mapped to a child failure).
        with pytest.raises(JobError):
            await h.run()
        assert h.adapters.probed == []
        assert outside.read_bytes() == b"outside"
        assert (await h.facts())[5] is None
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_audio_child_is_never_probed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        h.adapters.dimensions = VideoDimensions(1920, 1080)
        assert await h.run() == ChildResult.COMPLETED
        assert h.adapters.probed == []
        item = (await h.facts())[5]
        assert item is not None and item.resolution is None
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["placement", "catalog", "losing_cas", "placement_cancel"])
async def test_finalization_never_lies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      failure: str) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        if failure == "catalog":
            async def fail(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("catalog failure")
            monkeypatch.setattr("app.services.collection_persistence.stage_media_catalog_reference",
                                fail)
        if failure == "losing_cas":
            async def stale() -> None:
                async with h.sessions() as session:
                    await session.execute(update(DownloadJob).values(status="cancelled"))
                    await session.commit()
            h.adapters.after_probe = stale
        if failure in {"placement", "placement_cancel"}:
            real_place = h.storage.place_child_final

            async def place(*args: Any, **kwargs: Any) -> Any:
                if failure == "placement":
                    raise OSError("disk failed")
                h.adapters.cancel.set()
                return await real_place(*args, **kwargs)
            monkeypatch.setattr(h.storage, "place_child_final", place)
        if failure == "losing_cas":
            assert await h.run() == ChildResult.SKIPPED
        elif failure == "placement_cancel":
            with pytest.raises(asyncio.CancelledError):
                await h.run()
        else:
            with pytest.raises(JobError) as caught:
                await h.run()
            assert caught.value.code == (JobErrorCode.DISK_WRITE_FAILED if failure == "placement"
                                         else JobErrorCode.INTERNAL_ERROR)
        facts = await h.facts()
        assert facts[0] != "completed" and facts[2] == 0 and facts[4] == 0
        assert facts[5] is None and facts[6] == []
    finally:
        await h.engine.dispose()


# --- M6.5c Collection Original WebM/Opus remux ---------------------------------------


# Fake remux output with foreign (RIFF) magic: the tag writer reports this
# ``.opus`` content unsupported, so it is published unchanged and silently.
UNSUPPORTED_OPUS = b"RIFF fake remux output"


def opus_source() -> dict[str, Any]:
    return {"formats": [{"format_id": "opus", "ext": "webm", "vcodec": "none",
                         "acodec": "opus", "abr": 160}]}


def exit_status() -> FFmpegError:
    # Raw detail stands in for stderr/paths that must never reach Activity.
    return FFmpegError(FFmpegFailureKind.FAILED, "stderr /private/staging/media.webm",
                       cause=FFmpegFailureCause.EXIT_STATUS)


def original_opus(h: Harness, error: BaseException | None = None) -> None:
    h.adapters.source = opus_source()
    h.adapters.download_ext = "webm"
    h.adapters.convert_error = error


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


def published_files(h: Harness) -> list[Path]:
    return sorted(p for p in h.storage.library_root.rglob("*") if p.is_file())


@pytest.mark.anyio
async def test_original_opus_child_remuxes_losslessly_without_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        original_opus(h)
        h.adapters.convert_bytes = UNSUPPORTED_OPUS
        transitions = record_transitions(monkeypatch)
        committed: list[ActivityEvent] = []
        assert await h.run(committed=committed) == ChildResult.COMPLETED
        assert h.adapters.calls == ["probe", "opus", "convert"]
        assert transitions == [JobStatus.ANALYZING, JobStatus.DOWNLOADING,
                               JobStatus.POST_PROCESSING, JobStatus.FINALIZING]
        [(source_name, output_name, profile, copy_audio, copy_video)] = h.adapters.conversions
        assert (source_name, profile, copy_audio, copy_video) == ("media.webm", "opus", True,
                                                                  False)
        assert output_name.startswith("remuxed-") and output_name.endswith(".opus")
        facts = await h.facts()
        assert facts[:5] == ("completed", "analyzing", 1, 1, 50)
        item, copies = facts[5:]
        # Lossless copy: source codec and bitrate stay real facts.
        assert (item.format, item.codec, item.bitrate) == ("opus", "opus", 160)
        assert [c.path for c in copies] == ["audio/Track.opus"]
        assert (h.storage.library_root / "audio" / "Track.opus").read_bytes() == (
            UNSUPPORTED_OPUS)
        assert committed == [] and await h.warnings() == []
        assert not h.processes and not (tmp_path / "staging" / "child-2").exists()
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("existing", [False, True], ids=["saved", "name_conflict"])
async def test_exit_status_publishes_original_webm_with_one_committed_warning(
    tmp_path: Path, existing: bool,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        original_opus(h, exit_status())
        target = h.storage.library_root / "audio" / "Track.webm"
        if existing:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"older")
        committed: list[ActivityEvent] = []
        assert await h.run(committed=committed) == ChildResult.COMPLETED
        assert h.adapters.calls == ["probe", "opus", "convert"]
        # A conflict keeps the older file and publishes the new one at (2).
        published = "audio/Track (2).webm" if existing else "audio/Track.webm"
        assert target.read_bytes() == (b"older" if existing else b"media")
        assert (h.storage.library_root / published).read_bytes() == b"media"
        facts = await h.facts()
        assert facts[:5] == ("completed", "analyzing", 1, 1, 50)
        item, copies = facts[5:]
        assert item.format == "webm" and [c.path for c in copies] == [published]
        rows = await h.warnings()
        # The new original was really saved in both cases.
        message = _REMUX_FALLBACK_SAVED
        assert [(r.job_id, r.event_type, r.message, json.loads(r.details_json or "null"))
                for r in rows] == [("parent", "job", message, {"child_id": "child-2"})]
        assert "stderr" not in message and "/private" not in message
        assert [e.id for e in committed] == [rows[0].id]
        assert not h.processes and not (tmp_path / "staging" / "child-2").exists()
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("error, code", [
    (FFmpegError(FFmpegFailureKind.BINARY_MISSING), JobErrorCode.FFMPEG_MISSING),
    (FFmpegError(FFmpegFailureKind.TIMEOUT), JobErrorCode.POST_PROCESSING_FAILED),
    (FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.OUTPUT_GUARD),
     JobErrorCode.POST_PROCESSING_FAILED),
    (FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.SPAWN),
     JobErrorCode.POST_PROCESSING_FAILED),
    (FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.NO_OUTPUT),
     JobErrorCode.POST_PROCESSING_FAILED),
    (FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.OUTPUT_UNVERIFIABLE),
     JobErrorCode.POST_PROCESSING_FAILED),
    (FFmpegError(FFmpegFailureKind.FAILED), JobErrorCode.POST_PROCESSING_FAILED),
    (OSError(28, "No space left on device"), JobErrorCode.DISK_WRITE_FAILED),
    (JobError(JobErrorCode.MALFORMED_OUTPUT), JobErrorCode.MALFORMED_OUTPUT),
], ids=["binary_missing", "timeout", "output_guard", "spawn", "no_output",
        "output_unverifiable", "unclassified", "oserror", "joberror"])
async def test_ineligible_remux_failure_fails_child_without_warning(
    tmp_path: Path, error: BaseException, code: JobErrorCode,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        original_opus(h, error)
        committed: list[ActivityEvent] = []
        with pytest.raises(JobError) as caught:
            await h.run(committed=committed)
        assert caught.value.code == code
        assert h.adapters.calls == ["probe", "opus", "convert"]
        facts = await h.facts()
        assert facts[0] == "post_processing" and facts[2] == 0
        assert facts[5] is None and facts[6] == []
        assert committed == [] and await h.warnings() == []
        assert not published_files(h)
        assert not h.processes
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_explicit_opus_output_exit_status_fails_without_fallback(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio", output="opus")
        original_opus(h, exit_status())
        committed: list[ActivityEvent] = []
        with pytest.raises(JobError) as caught:
            await h.run(committed=committed)
        assert caught.value.code == JobErrorCode.POST_PROCESSING_FAILED
        [(source_name, output_name, *_)] = h.adapters.conversions
        assert source_name == "media.webm" and output_name.startswith("converted-")
        facts = await h.facts()
        assert facts[0] == "post_processing" and facts[5] is None and facts[6] == []
        assert committed == [] and await h.warnings() == []
        assert not published_files(h)
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["blocked", "sigterm_exit", "event_exit", "marker",
                                  "lost_finalizing"])
async def test_cancel_around_child_remux_never_falls_back_or_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    h = Harness(tmp_path)
    entered = asyncio.Event()
    task = None

    async def mark() -> None:
        # Durable marker only: no runtime event reaches this attempt.
        async with h.sessions() as session:
            assert await stage_collection_cancel_request(
                session, job_id="parent", owner_id="owner") == (
                    CollectionCancelRequestOutcome.STAGED)
            await session.commit()

    async def sigterm() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Cancel's SIGTERM surfaces from the adapter as a non-zero exit.
            raise exit_status() from None

    try:
        await h.seed("audio")
        original_opus(h, None if case in {"blocked", "sigterm_exit"} else exit_status())
        if case == "blocked":
            h.adapters.block = "convert"
        elif case == "sigterm_exit":
            h.adapters.on_convert = sigterm
        elif case == "event_exit":
            h.adapters.cancel_at = "convert"
        elif case == "marker":
            h.adapters.on_convert = mark
        else:
            real_place = h.storage.place_child_final

            async def place(*args: Any, **kwargs: Any) -> Any:
                await mark()
                return await real_place(*args, **kwargs)
            monkeypatch.setattr(h.storage, "place_child_final", place)
        committed: list[ActivityEvent] = []
        task = asyncio.create_task(h.run(committed=committed))
        if case in {"blocked", "sigterm_exit"}:
            await asyncio.wait_for((h.adapters.started if case == "blocked"
                                    else entered).wait(), 2)
            with h.gate:
                h.adapters.cancel.set()
            if case == "blocked":
                await asyncio.wait_for(h.adapters.cleanup.wait(), 2)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                assert not task.done()
                h.adapters.release.set()
        if case in {"marker", "lost_finalizing"}:
            assert await asyncio.wait_for(task, 2) == ChildResult.SKIPPED
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        if case == "blocked":
            assert h.adapters.reaped
        assert h.adapters.calls == ["probe", "opus", "convert"]
        facts = await h.facts()
        assert facts[0] == ("finalizing" if case == "lost_finalizing" else "post_processing")
        assert facts[2] == 0 and facts[5] is None and facts[6] == []
        assert committed == [] and await h.warnings() == []
        published = published_files(h)
        # Placement before a lost completion CAS is existing accepted behaviour.
        assert [p.name for p in published] == (
            ["Track.webm"] if case == "lost_finalizing" else [])
        assert not h.processes
    finally:
        h.adapters.release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["catalog", "activity", "commit"])
async def test_fallback_completion_rollback_leaves_no_warning_or_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from sqlalchemy import text

    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        original_opus(h, exit_status())
        if failure == "catalog":
            async def fail_catalog(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("catalog re-verification failure")
            monkeypatch.setattr(
                "app.services.collection_persistence.stage_media_catalog_reference",
                fail_catalog)
        elif failure == "activity":
            async def fail_activity(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("warning Activity write")
            monkeypatch.setattr(child_runner.activity, "add_activity_event", fail_activity)
        else:
            async with h.sessions() as session:
                await session.execute(text(
                    "CREATE TABLE child_commit_probe (id INTEGER PRIMARY KEY, "
                    "parent_id INTEGER REFERENCES child_commit_probe(id) "
                    "DEFERRABLE INITIALLY DEFERRED)"))
                await session.commit()
            real_completion = child_runner.stage_child_completion

            async def staged(session: Any, *args: Any, **kwargs: Any) -> bool:
                won = await real_completion(session, *args, **kwargs)
                # Real SQLite COMMIT failure after the warning is staged.
                await session.execute(text("INSERT INTO child_commit_probe VALUES (1, 2)"))
                return won
            monkeypatch.setattr(child_runner, "stage_child_completion", staged)
        committed: list[ActivityEvent] = []
        with pytest.raises(JobError) as caught:
            await h.run(committed=committed)
        assert caught.value.code == JobErrorCode.INTERNAL_ERROR
        facts = await h.facts()
        assert facts[0] == "finalizing" and facts[2] == 0 and facts[4] == 0
        assert facts[5] is None and facts[6] == []
        assert committed == [] and await h.warnings() == []
        assert not h.processes
    finally:
        await h.engine.dispose()


# --- M6.5-T4 Collection child audio text tags ------------------------------------------


# Explicit source music fields plus probe fields that are never tag sources.
TAGGED_PROBE: dict[str, Any] = {
    "track": "Probe Track", "artists": ["A", "B"], "album": "Probe Album",
    "track_number": 3, "title": "Probe Title", "playlist_index": 7,
}
# Ogg magic in an ``.m4a`` file: the writer reports it unsupported (no error).
UNSUPPORTED_M4A = b"OggS\x00\x02 not m4a"


def spy_tagging(
    h: Harness, monkeypatch: pytest.MonkeyPatch,
    after: Callable[[StagedEditResult], None] | None = None,
) -> list[tuple[str, Path, StagedEditResult]]:
    """Record each staged edit; ``after`` runs on the returned, still pinned result."""
    seen: list[tuple[str, Path, StagedEditResult]] = []
    real = h.storage.stage_edited_copy

    async def spy(staging_key: str, prepared: Path,
                  edit: Callable[[BinaryIO], bool]) -> StagedEditResult:
        result = await real(staging_key, prepared, edit)
        seen.append((staging_key, prepared, result))
        if after is not None:
            after(result)
        return result

    monkeypatch.setattr(h.storage, "stage_edited_copy", spy)
    return seen


def spy_placement(h: Harness, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Any]]:
    seen: list[tuple[Path, Any]] = []
    real = h.storage.place_child_final

    async def place(key: str, output: Path, **kwargs: Any) -> Any:
        seen.append((output, kwargs.get("source_identity")))
        return await real(key, output, **kwargs)

    monkeypatch.setattr(h.storage, "place_child_final", place)
    return seen


def staging_names(tmp_path: Path) -> list[str]:
    folder = tmp_path / "staging" / "child-2"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def tag_keys(path: Path) -> list[str]:
    return sorted(key.upper() for key in FLAC(path).keys())


async def warning_facts(h: Harness) -> list[tuple[str | None, str, str, Any]]:
    return [(r.job_id, r.event_type, r.message, json.loads(r.details_json or "null"))
            for r in await h.warnings()]


async def loop_turn() -> None:
    """One deterministic event-loop turn (no timing sleep)."""
    loop = asyncio.get_running_loop()
    responsive = loop.create_future()
    loop.call_soon(responsive.set_result, None)
    await asyncio.wait_for(responsive, 2)


@pytest.mark.anyio
async def test_audio_child_publishes_verified_tagged_explicit_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio", output="flac", playlist_index=40, track_number=41)
        h.adapters.source = source() | TAGGED_PROBE
        h.adapters.convert_bytes = tiny_flac()
        edits = spy_tagging(h, monkeypatch)
        placements = spy_placement(h, monkeypatch)
        committed: list[ActivityEvent] = []
        assert await h.run(committed=committed) == ChildResult.COMPLETED
        [(key, prepared, result)] = edits
        assert key == "child-2" and result.edited
        assert prepared.name.startswith("converted-") and prepared.suffix == ".flac"
        # The verified, pinned copy is the inode child placement is bound to.
        assert placements == [(result.path, result.identity)]
        item, copies = (await h.facts())[5:]
        published = h.storage.library_root / "audio" / "Track.flac"
        assert [c.path for c in copies] == ["audio/Track.flac"]
        assert copies[0].size_bytes == published.stat().st_size
        assert (item.title, item.format) == ("Track", "flac")
        tags = FLAC(published)
        # Source track wins over the title; explicit source track number only.
        assert tags["TITLE"] == ["Probe Track"] and tags["ARTIST"] == ["A", "B"]
        assert tags["ALBUM"] == ["Probe Album"] and tags["TRACKNUMBER"] == ["3"]
        assert tag_keys(published) == ["ALBUM", "ARTIST", "TITLE", "TRACKNUMBER"]
        assert committed == [] and await h.warnings() == []
        assert not h.processes and not (tmp_path / "staging" / "child-2").exists()
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("probe", [
    {},
    {"title": "Probe Title", "track": "  ", "playlist_index": 7, "playlist_count": 9,
     "uploader": "Uploader", "upload_date": "20200101"},
], ids=["no_metadata", "non_tag_probe_fields"])
async def test_child_title_is_tag_fallback_and_order_never_becomes_track_number(
    tmp_path: Path, probe: dict[str, Any],
) -> None:
    h = Harness(tmp_path)
    try:
        # Persisted order/track columns are naming/collection facts only.
        await h.seed("audio", output="flac", playlist_index=40, track_number=41)
        h.adapters.source = source() | probe
        h.adapters.convert_bytes = tiny_flac()
        assert await h.run() == ChildResult.COMPLETED
        published = h.storage.library_root / "audio" / "Track.flac"
        assert FLAC(published)["TITLE"] == ["Track"]
        assert tag_keys(published) == ["TITLE"]  # no track/disc/album/artist/date
        assert await h.warnings() == []
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("probe,expected", [
    ({"title": "  Probe Title \n"}, ["Probe Title"]),
    ({"title": "Probe Title", "track": "Probe Track"}, ["Probe Track"]),  # track wins
    ({"title": "   "}, None),
    ({"title": 123}, None),
    ({"title": "bad\x00"}, None),
    ({}, None),
], ids=["probe_title", "track_wins", "blank", "not_str", "nul", "missing"])
async def test_probe_title_is_tag_fallback_only_without_persisted_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: dict[str, Any],
    expected: list[str] | None,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio", output="flac")
        async with h.sessions() as session:
            await session.execute(
                update(JobItem).where(JobItem.id == "child-2").values(title=None))
            await session.commit()
        h.adapters.source = source() | probe
        h.adapters.convert_bytes = tiny_flac()
        edits = spy_tagging(h, monkeypatch)
        assert await h.run() == ChildResult.COMPLETED
        # Filename, catalog and the stored row keep their existing fallback.
        published = h.storage.library_root / "audio" / "Untitled.flac"
        assert published_files(h) == [published]
        item, copies = (await h.facts())[5:]
        assert item.title == "Untitled" and [c.path for c in copies] == ["audio/Untitled.flac"]
        async with h.sessions() as session:
            row = await session.get(JobItem, "child-2")
            assert row is not None and row.title is None
        if expected is None:
            # No usable title anywhere: nothing invented, no copy made.
            assert edits == [] and published.read_bytes() == tiny_flac()
        else:
            assert FLAC(published)["TITLE"] == expected
            assert tag_keys(published) == ["TITLE"]
        assert await h.warnings() == []
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("output", ["mp3", "m4a", "flac", "opus"])
async def test_typed_tag_failure_keeps_explicit_target_with_one_committed_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio", output=output)
        h.adapters.source = source() | TAGGED_PROBE
        # Default fake output is not parsable as the target: a typed TagError.
        edits = spy_tagging(h, monkeypatch)
        placements = spy_placement(h, monkeypatch)
        committed: list[ActivityEvent] = []
        assert await h.run(committed=committed) == ChildResult.COMPLETED
        [(_, prepared, result)] = edits
        assert not result.edited and result.path == prepared
        assert placements == [(prepared, result.identity)]
        # The ready explicit target, intact; never the pre-conversion .mp4 source.
        target = h.storage.library_root / "audio" / f"Track.{output}"
        assert published_files(h) == [target]
        assert target.read_bytes() == b"converted:media"
        item, copies = (await h.facts())[5:]
        assert item.format == output and [c.path for c in copies] == [f"audio/Track.{output}"]
        assert await warning_facts(h) == [
            ("parent", "job", _TAG_FALLBACK_SAVED, {"child_id": "child-2"})]
        assert "/" not in _TAG_FALLBACK_SAVED and "Probe" not in _TAG_FALLBACK_SAVED
        assert [e.id for e in committed] == [r.id for r in await h.warnings()]
        assert not h.processes and not (tmp_path / "staging" / "child-2").exists()
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["wav", "original_mp4", "remux_fallback", "video", "no_tags"])
async def test_ineligible_children_bypass_tagging_without_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    h = Harness(tmp_path)
    try:
        if case == "video":
            await h.seed("video", 360)  # muxed: the download is the final output
        else:
            await h.seed("audio", output={"wav": "wav", "no_tags": "flac"}.get(case))
        h.adapters.source = source() | TAGGED_PROBE
        if case == "remux_fallback":
            original_opus(h, exit_status())
            h.adapters.source = opus_source() | TAGGED_PROBE
        if case == "no_tags":
            # No usable source field and no persisted title: nothing is invented.
            h.adapters.source = source()
            h.adapters.convert_bytes = tiny_flac()
            async with h.sessions() as session:
                await session.execute(
                    update(JobItem).where(JobItem.id == "child-2").values(title=None))
                await session.commit()
        edits = spy_tagging(h, monkeypatch)
        placements = spy_placement(h, monkeypatch)
        committed: list[ActivityEvent] = []
        assert await h.run(committed=committed) == ChildResult.COMPLETED
        assert edits == [] and [identity for _, identity in placements] == [None]
        path, data, warnings = {
            "wav": ("audio/Track.wav", b"converted:media", []),
            "original_mp4": ("audio/Track.mp4", b"media", []),
            "remux_fallback": ("audio/Track.webm", b"media", [_REMUX_FALLBACK_SAVED]),
            "video": ("video/02 - Track.mp4", b"media", []),
            "no_tags": ("audio/Untitled.flac", tiny_flac(), []),
        }[case]
        assert published_files(h) == [h.storage.library_root / path]
        assert (h.storage.library_root / path).read_bytes() == data
        assert [r.message for r in await h.warnings()] == warnings
        assert len(committed) == len(warnings)
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
async def test_unsupported_content_is_published_unchanged_and_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio", output="m4a")
        h.adapters.source = source() | TAGGED_PROBE
        h.adapters.convert_bytes = UNSUPPORTED_M4A
        edits = spy_tagging(h, monkeypatch)
        committed: list[ActivityEvent] = []
        assert await h.run(committed=committed) == ChildResult.COMPLETED
        [(_, prepared, result)] = edits
        assert not result.edited and result.path == prepared
        assert (h.storage.library_root / "audio" / "Track.m4a").read_bytes() == UNSUPPORTED_M4A
        assert committed == [] and await h.warnings() == []
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure,code", [
    ("oserror", JobErrorCode.DISK_WRITE_FAILED),
    ("changed", JobErrorCode.INTERNAL_ERROR),
    ("path", JobErrorCode.INTERNAL_ERROR),
    ("cleanup", JobErrorCode.DISK_WRITE_FAILED),
])
async def test_tagging_storage_errors_fail_child_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, code: JobErrorCode,
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio", output="mp3")
        h.adapters.source = source() | TAGGED_PROBE
        errors: dict[str, BaseException] = {
            "oserror": OSError(28, "No space left on device"),
            "changed": MediaFileTargetChangedError("swapped"),
            "path": StoragePathError("escape"),
        }
        if failure == "cleanup":
            # The typed TagError is captured, but removing the own copy fails:
            # a storage error, never reported as an optional tag failure.
            def refuse(dir_fd: int, name: str, identity: tuple[int, int]) -> None:
                raise PermissionError(13, "denied")
            monkeypatch.setattr(h.storage, "_remove_own_copy", refuse)
        else:
            async def broken(*args: Any, **kwargs: Any) -> StagedEditResult:
                raise errors[failure]
            monkeypatch.setattr(h.storage, "stage_edited_copy", broken)
        committed: list[ActivityEvent] = []
        with pytest.raises(JobError) as caught:
            await h.run(committed=committed)
        assert caught.value.code == code
        facts = await h.facts()
        assert facts[0] == "post_processing" and facts[2] == 0
        assert facts[5] is None and facts[6] == []
        assert committed == [] and await h.warnings() == []
        assert published_files(h) == []
        assert [n for n in staging_names(tmp_path) if n.startswith("converted-")]
        assert not h.processes
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("edited", [True, False], ids=["tagged", "untouched"])
async def test_replaced_pinned_selection_is_rejected_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edited: bool,
) -> None:
    h = Harness(tmp_path)
    swapped: list[Path] = []

    def swap(result: StagedEditResult) -> None:
        assert result.edited is edited
        result.path.unlink()
        result.path.write_bytes(b"impostor")
        swapped.append(result.path)

    try:
        await h.seed("audio", output="flac" if edited else "m4a")
        h.adapters.source = source() | TAGGED_PROBE
        h.adapters.convert_bytes = tiny_flac() if edited else UNSUPPORTED_M4A
        edits = spy_tagging(h, monkeypatch, swap)
        committed: list[ActivityEvent] = []
        with pytest.raises(JobError) as caught:
            await h.run(committed=committed)
        assert caught.value.code == JobErrorCode.INTERNAL_ERROR
        [(_, prepared, _result)] = edits
        [path] = swapped
        # Fail closed before copy/publish; the foreign file is never deleted.
        assert path.read_bytes() == b"impostor"
        assert published_files(h) == []
        if edited:
            assert prepared.read_bytes() == tiny_flac()  # prepared output untouched
        facts = await h.facts()
        assert facts[0] == "finalizing" and facts[5] is None and facts[6] == []
        assert committed == [] and await h.warnings() == []
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("origin", ["caller", "parent"])
@pytest.mark.parametrize("outcome", ["tagged", "typed_failure"])
async def test_cancel_during_edit_drains_worker_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str, outcome: str,
) -> None:
    h = Harness(tmp_path)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release, finished = threading.Event(), threading.Event()
    real_editor = child_runner._text_tag_editor

    def blocking_editor(
        suffix: str, tags: TextTags, failures: list[TagError],
    ) -> Callable[[BinaryIO], bool]:
        real = real_editor(suffix, tags, failures)

        def edit(handle: BinaryIO) -> bool:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test did not release the edit worker"
            try:
                return real(handle)
            finally:
                finished.set()

        return edit

    drained = asyncio.Event()
    deliveries: list[bool] = []

    class DrainProbe:
        """Storage's ``asyncio`` namespace; records cancellation in its drain wait."""

        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

        async def wait(self, futures: Any, **kwargs: Any) -> Any:
            try:
                return await asyncio.wait(futures, **kwargs)
            except asyncio.CancelledError:
                # The real stage_edited_copy waiter was cancelled while its
                # blocking worker is still running; it keeps draining.
                deliveries.append(finished.is_set())
                drained.set()
                raise

    monkeypatch.setattr(child_runner, "_text_tag_editor", blocking_editor)
    monkeypatch.setattr(storage_service, "asyncio", DrainProbe())
    transitions = record_transitions(monkeypatch)
    tagged = outcome == "tagged"
    task = None
    try:
        await h.seed("audio", output="flac" if tagged else "mp3")
        h.adapters.source = source() | TAGGED_PROBE
        if tagged:
            h.adapters.convert_bytes = tiny_flac()
        committed: list[ActivityEvent] = []
        task = asyncio.create_task(h.run(committed=committed))
        await asyncio.wait_for(entered.wait(), 2)
        if origin == "parent":
            # Only the parent signal: the watcher must deliver it to the owned
            # execution, which reaches the staged-copy drain by itself.
            h.adapters.cancel.set()
            await asyncio.wait_for(drained.wait(), 2)
        else:
            task.cancel()
            await asyncio.wait_for(drained.wait(), 2)
            for _ in range(2):
                task.cancel()
                await loop_turn()
        await loop_turn()
        # Cancellation reached the drain while the worker was still blocked.
        assert deliveries and not any(deliveries)
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert finished.is_set()
        assert JobStatus.FINALIZING not in transitions
        facts = await h.facts()
        assert facts[0] == "post_processing" and facts[2] == 0
        assert facts[5] is None and facts[6] == []
        assert committed == [] and await h.warnings() == []
        assert published_files(h) == []
        names = staging_names(tmp_path)
        assert not [n for n in names if n.startswith(".edit-")]  # own copy discarded
        [prepared] = [n for n in names if n.startswith("converted-")]
        assert (tmp_path / "staging" / "child-2" / prepared).read_bytes() == (
            tiny_flac() if tagged else b"converted:media")
        assert not h.processes
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["lost_claim", "catalog", "commit"])
async def test_tag_fallback_warning_requires_committed_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from sqlalchemy import text

    h = Harness(tmp_path)
    try:
        await h.seed("audio", output="mp3")
        h.adapters.source = source() | TAGGED_PROBE  # typed TagError: fallback
        if failure == "lost_claim":
            async def lost(session: Any, *args: Any, **kwargs: Any) -> bool:
                return False
            monkeypatch.setattr(child_runner, "stage_child_completion", lost)
        elif failure == "catalog":
            async def fail_catalog(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("catalog re-verification failure")
            monkeypatch.setattr(
                "app.services.collection_persistence.stage_media_catalog_reference",
                fail_catalog)
        else:
            async with h.sessions() as session:
                await session.execute(text(
                    "CREATE TABLE child_commit_probe (id INTEGER PRIMARY KEY, "
                    "parent_id INTEGER REFERENCES child_commit_probe(id) "
                    "DEFERRABLE INITIALLY DEFERRED)"))
                await session.commit()
            real_completion = child_runner.stage_child_completion

            async def staged(session: Any, *args: Any, **kwargs: Any) -> bool:
                won = await real_completion(session, *args, **kwargs)
                # Real SQLite COMMIT failure after the warning is staged.
                await session.execute(text("INSERT INTO child_commit_probe VALUES (1, 2)"))
                return won
            monkeypatch.setattr(child_runner, "stage_child_completion", staged)
        committed: list[ActivityEvent] = []
        if failure == "lost_claim":
            assert await h.run(committed=committed) == ChildResult.SKIPPED
        else:
            with pytest.raises(JobError) as caught:
                await h.run(committed=committed)
            assert caught.value.code == JobErrorCode.INTERNAL_ERROR
        facts = await h.facts()
        assert facts[0] == "finalizing" and facts[2] == 0 and facts[4] == 0
        assert facts[5] is None and facts[6] == []
        assert committed == [] and await h.warnings() == []
        assert not h.processes
    finally:
        await h.engine.dispose()
