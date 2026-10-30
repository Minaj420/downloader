import asyncio
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select, update

from app.domain.jobs import JobError, JobErrorCode
from app.integrations.ffmpeg import ConversionProfile, FFmpegProcessor
from app.integrations.ytdlp import DownloadProgress, YtDlpAnalyzer, YtDlpDownloader
from app.models.jobs import DownloadJob, JobItem
from app.models.library import LibraryItem, MediaFile
from app.repositories import jobs
from app.services.collection_child_runner import ChildResult, ChildRunnerDeps, run_collection_child
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
        path = Path(kwargs["output_template"].replace("%(ext)s", "mp4"))
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
                   **values: Any) -> None:
        async with self.sessions() as session:
            await jobs.stage_collection_job(session, parent(
                    status="analyzing", collection_mode=mode,
                    collection_profile_json=json.dumps({"max_height": height}),
                    variant_id="NOT-A-CHILD-SELECTOR",
                ), [child(2, title="Track", **values), child(9, status="failed")], owner_id="owner")
            await session.commit()

    async def run(self, owner_id: str = "owner", child_id: str = "child-2") -> ChildResult:
        return await run_collection_child(
            "parent", owner_id, child_id, deps=self.deps, cancel_event=self.adapters.cancel,
            processes=self.processes, cancel_gate=self.gate,
        )

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
@pytest.mark.parametrize("step", ["probe", "high", "merge"])
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
    ("registration", ["probe", "high"])])
async def test_cancel_blocks_next_step(tmp_path: Path, at: str, expected: list[str]) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed()
        h.adapters.cancel_at = at
        if at == "registration":
            h.adapters.returncode = None
        with pytest.raises(asyncio.CancelledError):
            await h.run()
        assert h.adapters.calls == expected
        if at == "registration":
            assert h.adapters.killed
        assert (await h.facts())[2] == 0
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
