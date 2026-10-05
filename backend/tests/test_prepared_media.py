import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from app.domain.jobs import CollectionProfile, JobError, JobErrorCode
from app.integrations.ffmpeg import FFmpegProcessor
from app.integrations.ytdlp import DownloadProgress, YtDlpDownloader, YtDlpError, YtDlpFailureKind
from app.services.collection_profile_resolver import NativeChildPlan, resolve_native_child
from app.services.job_runner import _JobExecution
from app.services.prepared_media import prepare_media
from app.services.storage_service import StorageService


def source(*, video_codec: str = "avc1", audio_codec: str = "mp4a.40.2") -> dict[str, Any]:
    return {"formats": [
        {"format_id": "mux", "ext": "mp4", "height": 360,
         "vcodec": video_codec, "acodec": audio_codec},
        {"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": audio_codec},
        {"format_id": "low", "ext": "mp4", "height": 720,
         "vcodec": video_codec, "acodec": "none"},
        {"format_id": "high", "ext": "mp4", "height": 2160,
         "vcodec": video_codec, "acodec": "none"},
    ]}


class Pipeline(YtDlpDownloader, FFmpegProcessor):
    """Controlled adapters: record paths, never mutate the filesystem."""

    def __init__(self, storage: StorageService) -> None:
        self.storage = storage
        self.cancel = asyncio.Event()
        self.calls: list[tuple[str, str, Path]] = []
        self.callbacks: list[Any] = []
        self.progress: list[tuple[int, int, int | None]] = []
        self.cancel_at = ""
        self.failure = False
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.block_progress = False
        self.process_hook: Any = lambda process: None

    async def check(self) -> None:
        if self.cancel.is_set():
            raise JobError(JobErrorCode.CANCELLED)

    async def before_merge(self) -> None:
        self.calls.append(("status", "", Path()))
        if self.cancel_at == "status":
            self.cancel.set()

    def forwarder(self, index: int, count: int) -> Any:
        async def write(value: DownloadProgress) -> None:
            self.started.set()
            if self.block_progress:
                await self.release.wait()
            self.progress.append((index, count, value.downloaded_bytes))

        return lambda value: asyncio.create_task(write(value))

    async def download(self, **kwargs: Any) -> list[str]:
        selector = kwargs["format_selector"]
        path = Path(kwargs["output_template"].replace("%(ext)s", "mp4"))
        self.calls.append((selector, kwargs["url"], path))
        if self.cancel_at == "registration":
            self.cancel.set()
        kwargs["on_process_started"](cast(asyncio.subprocess.Process, object()))
        callback = kwargs["on_progress"]
        self.callbacks.append(callback)
        callback(DownloadProgress(3, 10, None, None, None))
        if self.cancel_at == selector:
            self.cancel.set()
        if self.failure:
            raise YtDlpError(YtDlpFailureKind.NETWORK_ERROR)
        return [str(path)]

    async def merge_streams(
        self, video_path: Path, audio_path: Path, output_path: Path,
        on_process_started: Any = None,
    ) -> None:
        assert video_path.parent == audio_path.parent == output_path.parent
        self.calls.append(("merge", "", output_path))
        if self.cancel_at == "merge_registration":
            self.cancel.set()
        on_process_started(cast(asyncio.subprocess.Process, object()))

    async def run(self, plan: NativeChildPlan, key: str = "a" * 32) -> Path:
        return await prepare_media(
            selected=plan.selected, companion=plan.companion,
            source_url=f"https://example.com/{key}", staging_key=key,
            storage=self.storage, downloader=self, ffmpeg=self,
            check_cancelled=self.check, on_process_started=self.process_hook,
            progress_forwarder=self.forwarder, before_merge=self.before_merge,
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline:
    return Pipeline(StorageService(tmp_path / "staging", tmp_path / "library"))


@pytest.mark.anyio
async def test_child_boundary_rejects_multiple_adapter_outputs(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def multiple(**kwargs: Any) -> list[str]:
        assert kwargs["child_output"] is True
        return ["one.mp4", "two.mp4"]
    monkeypatch.setattr(pipeline, "download", multiple)
    plan = resolve_native_child(source(), mode="video")
    with pytest.raises(JobError) as caught:
        await prepare_media(
            selected=plan.selected, companion=plan.companion,
            source_url="https://example.com/child", staging_key="child",
            storage=pipeline.storage, downloader=pipeline, ffmpeg=pipeline,
            check_cancelled=pipeline.check, on_process_started=pipeline.process_hook,
            progress_forwarder=pipeline.forwarder, before_merge=pipeline.before_merge,
            child_output=True,
        )
    assert caught.value.code == JobErrorCode.MALFORMED_OUTPUT
    assert not pipeline.calls


@pytest.mark.anyio
async def test_native_heterogeneous_plans_and_ceiling_use_actual_selectors(
    pipeline: Pipeline,
) -> None:
    plans = [
        resolve_native_child(source(), mode="video", profile=CollectionProfile(max_height=360)),
        resolve_native_child(source(), mode="audio"),
        resolve_native_child(source(), mode="video", profile=CollectionProfile(max_height=1080)),
        resolve_native_child(source(video_codec="vp9", audio_codec="opus"), mode="video"),
    ]
    results = []
    for index, plan in enumerate(plans):
        key = str(index) * 32
        start = len(pipeline.calls)
        results.append(await pipeline.run(plan, key))
        calls = pipeline.calls[start:]
        assert all(path.parent.name == key for name, _, path in calls if name != "status")
        assert all(url == f"https://example.com/{key}" for name, url, _ in calls
                   if name not in {"merge", "status"})
    assert [call[0] for call in pipeline.calls] == [
        "mux", "audio", "low", "audio", "status", "merge",
        "high", "audio", "status", "merge",
    ]
    assert [path.name for path in results] == [
        "media.mp4", "media.mp4", "merged.mp4", "merged.mkv",
    ]
    assert len({path.parent for path in results}) == 4
    assert pipeline.progress == [(0, 1, 3), (0, 1, 3), (0, 2, 3), (1, 2, 3),
                                 (0, 2, 3), (1, 2, 3)]


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_at, expected", [
    ("before", []), ("high", ["high"]), ("audio", ["high", "audio"]),
    ("status", ["high", "audio", "status"]),
])
async def test_cancel_blocks_every_next_download_and_merge(
    pipeline: Pipeline, cancel_at: str, expected: list[str],
) -> None:
    pipeline.cancel_at = cancel_at
    if cancel_at == "before":
        pipeline.cancel.set()
    with pytest.raises(JobError) as caught:
        await pipeline.run(resolve_native_child(source(), mode="video"))
    assert caught.value.code == JobErrorCode.CANCELLED
    assert [call[0] for call in pipeline.calls] == expected


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_at", ["registration", "merge_registration"])
async def test_registration_race_uses_attempt_hook_and_drains_termination(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch, cancel_at: str,
) -> None:
    # Use the actual single runner's registration race handling, not a copy.
    execution = object.__new__(_JobExecution)
    execution._job_id = "a" * 32
    execution._cancel_event = pipeline.cancel
    execution._processes = {}
    execution._helpers = set()
    process = cast(asyncio.subprocess.Process, type("Process", (), {"returncode": None})())
    registered = []

    async def terminate(value: Any, grace: float) -> None:
        assert value is process and grace == 5.0
        pipeline.started.set()
        await pipeline.release.wait()
        registered.append(value)

    monkeypatch.setattr("app.services.job_runner.ensure_process_terminated", terminate)
    pipeline.process_hook = lambda _: execution._register_process(process)
    pipeline.cancel_at = cancel_at
    task = asyncio.create_task(pipeline.run(resolve_native_child(source(), mode="video")))
    try:
        await asyncio.wait_for(pipeline.started.wait(), 2)
        assert not task.done()
        assert execution._processes[execution._job_id] is process
        pipeline.release.set()
        if cancel_at == "registration":
            with pytest.raises(JobError):
                await task
            assert [call[0] for call in pipeline.calls] == ["high"]
        else:
            await task
        assert registered == [process]
        assert all(helper.done() for helper in execution._helpers)
    finally:
        pipeline.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_callbacks_close_and_helpers_drain_even_with_repeated_cancellation(
    pipeline: Pipeline, outcome: str,
) -> None:
    pipeline.block_progress = True
    pipeline.failure = outcome == "failure"
    task = asyncio.create_task(pipeline.run(resolve_native_child(source(), mode="audio")))
    try:
        await asyncio.wait_for(pipeline.started.wait(), 2)
        assert not task.done()
        if outcome == "cancelled":
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not task.done()
        # Download has returned/raised: callback is already closed while draining.
        pipeline.callbacks[0](DownloadProgress(99, 100, None, None, None))
        pipeline.release.set()
        if outcome == "failure":
            with pytest.raises(YtDlpError):
                await task
        elif outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        pipeline.callbacks[0](DownloadProgress(99, 100, None, None, None))
        await asyncio.sleep(0)
        assert pipeline.progress == [(0, 1, 3)]
        # A later invocation cannot receive writes from the previous callbacks.
        pipeline.block_progress = False
        pipeline.failure = False
        await pipeline.run(resolve_native_child(source(), mode="audio"), "b" * 32)
        assert pipeline.progress == [(0, 1, 3), (0, 1, 3)]
    finally:
        pipeline.release.set()
        await asyncio.gather(task, return_exceptions=True)
