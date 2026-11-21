import asyncio
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select, update

from app.domain.jobs import JobError, JobErrorCode, JobStatus
from app.integrations.ffmpeg import (
    ConversionProfile,
    FFmpegError,
    FFmpegFailureCause,
    FFmpegFailureKind,
    FFmpegProcessor,
    VideoDimensions,
)
from app.integrations.ytdlp import DownloadProgress, YtDlpAnalyzer, YtDlpDownloader
from app.models.jobs import ActivityEvent, DownloadJob, JobItem
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs
from app.services import collection_child_runner as child_runner
from app.services.collection_child_runner import ChildResult, ChildRunnerDeps, run_collection_child
from app.services.collection_persistence import (
    CollectionCancelRequestOutcome,
    stage_collection_cancel_request,
)
from app.services.job_runner import _REMUX_FALLBACK_SAVED, _REMUX_FALLBACK_SKIPPED
from app.services.storage_service import StorageService
from tests.test_collection_repository import child, factory, parent
from tests.test_prepared_media import source


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
        output_path.write_bytes(b"converted:" + input_path.read_bytes())


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

    def link(src: Path, dst: Path) -> None:
        real_link(src, dst)
        if phase == "published":
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
        item = (await h.facts())[5]
        assert item is not None and item.resolution == label
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
            b"converted:media")
        assert committed == [] and await h.warnings() == []
        assert not h.processes and not (tmp_path / "staging" / "child-2").exists()
    finally:
        await h.engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("existing", [False, True], ids=["saved", "skip_existing"])
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
        assert target.read_bytes() == (b"older" if existing else b"media")
        facts = await h.facts()
        assert facts[:5] == ("completed", "analyzing", 1, 1, 50)
        item, copies = facts[5:]
        assert item.format == "webm" and [c.path for c in copies] == ["audio/Track.webm"]
        rows = await h.warnings()
        message = _REMUX_FALLBACK_SKIPPED if existing else _REMUX_FALLBACK_SAVED
        assert [(r.job_id, r.event_type, r.message, json.loads(r.details_json or "null"))
                for r in rows] == [("parent", "job", message, {"child_id": "child-2"})]
        assert "stderr" not in message and "/private" not in message
        if existing:
            assert "saved the original" not in message
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
