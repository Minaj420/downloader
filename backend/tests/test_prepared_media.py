import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from app.config import Settings
from app.domain.jobs import CollectionProfile, JobError, JobErrorCode
from app.integrations.ffmpeg import (
    ConversionProfile,
    FFmpegError,
    FFmpegFailureCause,
    FFmpegFailureKind,
    FFmpegProcessor,
)
from app.integrations.ytdlp import DownloadProgress, YtDlpDownloader, YtDlpError, YtDlpFailureKind
from app.main import create_app
from app.services.analysis_normalizer import normalize_source_formats
from app.services.collection_profile_resolver import NativeChildPlan, resolve_native_child
from app.services.job_runner import _JobExecution
from app.services.prepared_media import (
    OriginalRemux,
    measure_final_dimensions,
    merge_container,
    prepare_media,
)
from app.services.storage_service import StorageService
from tests.fake_binaries import fake_ffmpeg_record


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
        self.conversions: list[tuple[Path, bool, bool]] = []
        self.convert_failure = False
        self.download_ext = "mp4"
        self.convert_error: BaseException | None = None
        self.convert_block = False
        self.converting = asyncio.Event()
        self.fallbacks = 0

    async def check(self) -> None:
        if self.cancel.is_set():
            raise JobError(JobErrorCode.CANCELLED)

    async def before_merge(self) -> None:
        self.calls.append(("status", "", Path()))
        if self.cancel_at == "status":
            self.cancel.set()

    async def before_remux(self) -> None:
        self.calls.append(("remux", "", Path()))
        if self.cancel_at == "remux":
            self.cancel.set()

    def on_fallback(self) -> None:
        self.fallbacks += 1

    def remux(self) -> OriginalRemux:
        return OriginalRemux(self.before_remux, self.on_fallback)

    def forwarder(self, index: int, count: int) -> Any:
        async def write(value: DownloadProgress) -> None:
            self.started.set()
            if self.block_progress:
                await self.release.wait()
            self.progress.append((index, count, value.downloaded_bytes))

        return lambda value: asyncio.create_task(write(value))

    async def download(self, **kwargs: Any) -> list[str]:
        selector = kwargs["format_selector"]
        path = Path(kwargs["output_template"].replace("%(ext)s", self.download_ext))
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

    async def convert(
        self, input_path: Path, output_path: Path, profile: ConversionProfile, *,
        copy_audio: bool = False, copy_video: bool = False, on_process_started: Any = None,
    ) -> None:
        assert input_path.parent == output_path.parent
        self.calls.append(("convert", profile, output_path))
        self.conversions.append((input_path, copy_audio, copy_video))
        if self.cancel_at == "convert_registration":
            self.cancel.set()
        on_process_started(cast(asyncio.subprocess.Process, object()))
        if self.cancel_at == "convert":
            self.cancel.set()
        if self.convert_block:
            self.converting.set()
            await self.release.wait()
        if self.convert_error is not None:
            raise self.convert_error
        if self.convert_failure:
            raise FFmpegError(FFmpegFailureKind.FAILED, "boom")

    async def run(
        self, plan: NativeChildPlan, key: str = "a" * 32, *,
        output: ConversionProfile | None = None, ffmpeg: FFmpegProcessor | None = None,
        original_remux: OriginalRemux | None = None, child_output: bool = False,
    ) -> Path:
        return await prepare_media(
            selected=plan.selected, companion=plan.companion,
            source_url=f"https://example.com/{key}", staging_key=key,
            storage=self.storage, downloader=self, ffmpeg=ffmpeg or self,
            check_cancelled=self.check, on_process_started=self.process_hook,
            progress_forwarder=self.forwarder, before_merge=self.before_merge,
            output=output, original_remux=original_remux, child_output=child_output,
        )


def assert_fresh(path: Path, stem: str, extension: str) -> None:
    """Per-call target: ``<stem>-<16 lowercase hex>.<extension>``."""
    assert re.fullmatch(rf"{stem}-[0-9a-f]{{16}}\.{re.escape(extension)}", path.name), path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline:
    return Pipeline(StorageService(tmp_path / "staging", tmp_path / "library"))


@pytest.mark.parametrize("raw_audio, container", [
    ("mp4a.40.2", "mp4"), ("mp4a.69", "mkv"), ("mp4a.6b", "mkv"),
    ("mp4a.40.34", "mkv"), ("mp4a", "mkv"),
])
def test_merge_container_uses_normalized_audio_subtype(raw_audio: str, container: str) -> None:
    video, audio = normalize_source_formats([
        {"format_id": "v", "ext": "mp4", "height": 720, "vcodec": "avc1.64001f", "acodec": "none"},
        {"format_id": "a", "ext": "m4a", "vcodec": "none", "acodec": raw_audio},
    ])
    assert video.video_codec == "h264"
    assert merge_container(video, audio) == container


@pytest.mark.anyio
@pytest.mark.parametrize("mode, ext, remux", [
    ("video", "mp4", False), ("audio", "webm", True),
], ids=["native", "original_remux_opt_in"])
async def test_child_boundary_rejects_multiple_adapter_outputs(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch, mode: str, ext: str, remux: bool,
) -> None:
    async def multiple(**kwargs: Any) -> list[str]:
        assert kwargs["child_output"] is True
        return [f"one.{ext}", f"two.{ext}"]
    monkeypatch.setattr(pipeline, "download", multiple)
    plan = (resolve_native_child(source(audio_codec="opus"), mode="audio") if remux
            else resolve_native_child(source(), mode=mode))
    with pytest.raises(JobError) as caught:
        await prepare_media(
            selected=plan.selected, companion=plan.companion,
            source_url="https://example.com/child", staging_key="child",
            storage=pipeline.storage, downloader=pipeline, ffmpeg=pipeline,
            check_cancelled=pipeline.check, on_process_started=pipeline.process_hook,
            progress_forwarder=pipeline.forwarder, before_merge=pipeline.before_merge,
            child_output=True, original_remux=pipeline.remux() if remux else None,
        )
    assert caught.value.code == JobErrorCode.MALFORMED_OUTPUT
    assert not pipeline.calls and not pipeline.conversions and pipeline.fallbacks == 0


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
    assert [path.name for path in results[:2]] == ["media.mp4", "media.mp4"]
    assert_fresh(results[2], "merged", "mp4")
    assert_fresh(results[3], "merged", "mkv")
    assert [path for name, _, path in pipeline.calls if name == "merge"] == results[2:]
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


def recorded(log: Path) -> list[list[str]]:
    return [chunk.splitlines() for chunk in log.read_text().split("END\n")[:-1]]


def tracked_helpers(pipeline: Pipeline) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []

    async def helper() -> None:
        await asyncio.sleep(0.01)

    def hook(_: Any) -> asyncio.Task[None]:
        tasks.append(asyncio.create_task(helper()))
        return tasks[-1]

    pipeline.process_hook = hook
    return tasks


@pytest.mark.anyio
async def test_without_output_never_converts_and_merge_argv_is_unchanged(
    pipeline: Pipeline, tmp_path: Path,
) -> None:
    log = tmp_path / "argv.log"
    binary = fake_ffmpeg_record(tmp_path, log)
    processor = FFmpegProcessor(binary=binary)
    merged = await pipeline.run(resolve_native_child(source(), mode="video"), ffmpeg=processor)
    media = await pipeline.run(resolve_native_child(source(), mode="audio"), "b" * 32)
    staging = merged.parent
    assert staging.name == "a" * 32 and media.name == "media.mp4"
    assert_fresh(merged, "merged", "mp4")
    assert recorded(log) == [[
        "-y", "-i", str(staging / "video.mp4"), "-i", str(staging / "audio.mp4"),
        "-c", "copy", str(merged),
    ]]
    assert [call[0] for call in pipeline.calls] == ["high", "audio", "status", "audio"]
    assert not pipeline.conversions


@pytest.mark.anyio
@pytest.mark.parametrize("profile, encoder", [
    ("mp3", ["-c:a", "libmp3lame", "-q:a", "0", "-id3v2_version", "3", "-f", "mp3"]),
    ("m4a", ["-c:a", "copy", "-movflags", "+faststart", "-f", "ipod"]),
])
async def test_audio_profile_downloads_then_converts_once(
    pipeline: Pipeline, tmp_path: Path, profile: ConversionProfile, encoder: list[str],
) -> None:
    log = tmp_path / "argv.log"
    processor = FFmpegProcessor(binary=fake_ffmpeg_record(tmp_path, log))
    tasks = tracked_helpers(pipeline)
    result = await pipeline.run(
        resolve_native_child(source(), mode="audio"), output=profile, ffmpeg=processor,
    )
    staging = result.parent
    assert staging.name == "a" * 32
    assert_fresh(result, "converted", profile)
    assert result.read_text() == "converted-media"
    assert [call[0] for call in pipeline.calls] == ["audio", "status"]
    assert recorded(log) == [[
        "-hide_banner", "-nostats", "-nostdin", "-y", "-i", str(staging / "media.mp4"),
        "-map", "0:a:0", "-vn", "-sn", "-dn", *encoder, str(result),
    ]]
    # Download and conversion processes both went through the attempt hook.
    assert len(tasks) == 2 and all(task.done() for task in tasks)


@pytest.mark.anyio
@pytest.mark.parametrize("mode, profile, merged_ext", [
    ("audio", "mp3", None),
    ("video", "mp4", "mp4"),
])
async def test_real_adapter_keeps_earlier_outputs_and_targets_fresh_names(
    pipeline: Pipeline, tmp_path: Path, mode: str, profile: ConversionProfile,
    merged_ext: str | None,
) -> None:
    log = tmp_path / "argv.log"
    processor = FFmpegProcessor(binary=fake_ffmpeg_record(tmp_path, log))
    staging = pipeline.storage.job_staging_dir("a" * 32)
    names = [f"converted.{profile}", f"converted-{'0' * 16}.{profile}"]
    if merged_ext is not None:
        names += [f"merged.{merged_ext}", f"merged-{'f' * 16}.{merged_ext}"]
    leftovers = {staging / name: f"earlier:{name}" for name in names}
    for path, content in leftovers.items():
        path.write_text(content)

    plan = resolve_native_child(source(), mode=mode)
    first = await pipeline.run(plan, output=profile, ffmpeg=processor)
    second = await pipeline.run(plan, output=profile, ffmpeg=processor)

    # Leftovers from an earlier attempt are neither reused nor removed.
    assert {path: path.read_text() for path in leftovers} == leftovers
    calls = recorded(log)
    merges = [call for call in calls if call[0] == "-y"]
    converts = [call for call in calls if call[0] == "-hide_banner"]
    assert len(calls) == len(merges) + len(converts)
    assert [Path(call[-1]) for call in converts] == [first, second]
    assert first != second
    for result in (first, second):
        assert result.parent == staging
        assert result not in leftovers
        assert_fresh(result, "converted", profile)
        assert result.read_text() == "converted-media"
    inputs = [Path(call[call.index("-i") + 1]) for call in converts]
    if merged_ext is None:
        assert not merges
        assert inputs == [staging / "media.mp4"] * 2
    else:
        merged = [Path(call[-1]) for call in merges]
        # Each conversion reads the merge output produced in the same call.
        assert inputs == merged and merged[0] != merged[1]
        for path in merged:
            assert path.parent == staging and path not in leftovers
            assert_fresh(path, "merged", merged_ext)
            assert path.read_text() == "converted-media"


@pytest.mark.anyio
@pytest.mark.parametrize("codecs, merged_ext, copy", [
    ({}, "mp4", True),
    ({"video_codec": "vp9", "audio_codec": "opus"}, "mkv", False),
])
async def test_merge_and_convert_call_hook_exactly_once(
    pipeline: Pipeline, codecs: dict[str, str], merged_ext: str, copy: bool,
) -> None:
    result = await pipeline.run(resolve_native_child(source(**codecs), mode="video"), output="mp4")
    assert [call[0] for call in pipeline.calls] == ["high", "audio", "status", "merge", "convert"]
    assert pipeline.calls[-1] == ("convert", "mp4", result)
    assert_fresh(result, "converted", "mp4")
    merged = pipeline.calls[-2][2]
    assert merged.parent == result.parent
    assert_fresh(merged, "merged", merged_ext)
    # Even an mp4 merge is remuxed (copy+copy) so faststart is applied.
    assert pipeline.conversions == [(merged, copy, copy)]


@pytest.mark.anyio
@pytest.mark.parametrize("mode, cancel_at, expected", [
    ("audio", "audio", ["audio"]),
    ("audio", "status", ["audio", "status"]),
    ("video", "status", ["high", "audio", "status"]),
    ("video", "merge_registration", ["high", "audio", "status", "merge"]),
])
async def test_cancel_before_convert_starts_no_conversion_and_drains(
    pipeline: Pipeline, mode: str, cancel_at: str, expected: list[str],
) -> None:
    tasks = tracked_helpers(pipeline)
    pipeline.cancel_at = cancel_at
    profile: ConversionProfile = "mp3" if mode == "audio" else "mp4"
    with pytest.raises(JobError) as caught:
        await pipeline.run(resolve_native_child(source(), mode=mode), output=profile)
    assert caught.value.code == JobErrorCode.CANCELLED
    assert [call[0] for call in pipeline.calls] == expected
    assert not pipeline.conversions
    assert tasks and all(task.done() for task in tasks)


@pytest.mark.anyio
async def test_convert_failure_propagates_after_draining_helpers(pipeline: Pipeline) -> None:
    tasks = tracked_helpers(pipeline)
    pipeline.convert_failure = True
    with pytest.raises(FFmpegError) as caught:
        await pipeline.run(resolve_native_child(source(), mode="audio"), output="opus")
    assert caught.value.kind == FFmpegFailureKind.FAILED
    assert [call[0] for call in pipeline.calls] == ["audio", "status", "convert"]
    assert len(tasks) == 2 and all(task.done() for task in tasks)


@pytest.mark.anyio
@pytest.mark.parametrize("mode, profile", [("audio", "mp4"), ("video", "flac")])
async def test_plan_error_happens_before_any_media_io(
    pipeline: Pipeline, tmp_path: Path, mode: str, profile: ConversionProfile,
) -> None:
    checks: list[None] = []

    async def check() -> None:
        checks.append(None)

    pipeline.check = check  # type: ignore[method-assign]
    with pytest.raises(JobError) as caught:
        await pipeline.run(resolve_native_child(source(), mode=mode), output=profile)
    assert caught.value.code == JobErrorCode.INVALID_PROFILE
    assert not pipeline.calls and not checks
    assert not (tmp_path / "staging" / ("a" * 32)).exists()


# --- M6.5b Original WebM/Opus remux ------------------------------------------------


def opus_plan(raw_audio: str = "opus", kind: str = "audio") -> NativeChildPlan:
    if kind == "muxed":
        return resolve_native_child(
            source(video_codec="vp9", audio_codec=raw_audio), mode="video",
            profile=CollectionProfile(max_height=360),
        )
    return resolve_native_child(source(audio_codec=raw_audio), mode="audio")


def exit_status() -> FFmpegError:
    return FFmpegError(FFmpegFailureKind.FAILED, "stderr", cause=FFmpegFailureCause.EXIT_STATUS)


def fake_ffmpeg_partial_fail(directory: Path) -> str:
    """Writes a partial output, then exits non-zero (an EXIT_STATUS failure)."""
    script = directory / "ffmpeg"
    script.write_text(
        '#!/bin/sh\nout=""\nfor a in "$@"; do out="$a"; done\n'
        "printf 'partial' > \"$out\"\necho 'Error: mux failed' >&2\nexit 1\n"
    )
    script.chmod(0o755)
    return str(script)


def webm_source(pipeline: Pipeline) -> Path:
    pipeline.download_ext = "webm"
    path = pipeline.storage.job_staging_dir("a" * 32) / "media.webm"
    path.write_bytes(b"webm-source")
    return path


@pytest.mark.anyio
async def test_original_webm_opus_remuxes_losslessly_into_fresh_target(
    pipeline: Pipeline, tmp_path: Path,
) -> None:
    log = tmp_path / "argv.log"
    processor = FFmpegProcessor(binary=fake_ffmpeg_record(tmp_path, log))
    tasks = tracked_helpers(pipeline)
    media = webm_source(pipeline)
    result = await pipeline.run(opus_plan(), ffmpeg=processor, original_remux=pipeline.remux())
    assert result.parent == media.parent
    assert_fresh(result, "remuxed", "opus")
    assert result.read_text() == "converted-media"
    assert media.read_bytes() == b"webm-source"
    assert [call[0] for call in pipeline.calls] == ["audio", "remux"]
    assert recorded(log) == [[
        "-hide_banner", "-nostats", "-nostdin", "-y", "-i", str(media),
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "copy", "-f", "opus", str(result),
    ]]
    assert pipeline.fallbacks == 0
    assert len(tasks) == 2 and all(task.done() for task in tasks)


@pytest.mark.anyio
async def test_real_adapter_exit_status_falls_back_once_to_intact_source(
    pipeline: Pipeline, tmp_path: Path,
) -> None:
    processor = FFmpegProcessor(binary=fake_ffmpeg_partial_fail(tmp_path))
    tasks = tracked_helpers(pipeline)
    media = webm_source(pipeline)
    result = await pipeline.run(opus_plan(), ffmpeg=processor, original_remux=pipeline.remux())
    assert result == media
    assert media.read_bytes() == b"webm-source"
    # The adapter discarded the partial remux output; nothing else was touched.
    assert [path.name for path in media.parent.iterdir()] == ["media.webm"]
    assert pipeline.fallbacks == 1
    assert len(tasks) == 2 and all(task.done() for task in tasks)


@pytest.mark.anyio
@pytest.mark.parametrize("error", [
    FFmpegError(FFmpegFailureKind.BINARY_MISSING),
    FFmpegError(FFmpegFailureKind.TIMEOUT),
    FFmpegError(FFmpegFailureKind.TIMEOUT, cause=FFmpegFailureCause.EXIT_STATUS),
    FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.OUTPUT_GUARD),
    FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.SPAWN),
    FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.NO_OUTPUT),
    FFmpegError(FFmpegFailureKind.FAILED, cause=FFmpegFailureCause.OUTPUT_UNVERIFIABLE),
    FFmpegError(FFmpegFailureKind.FAILED),
    OSError(28, "No space left on device"),
    JobError(JobErrorCode.INTERNAL_ERROR),
    asyncio.CancelledError(),
], ids=[
    "binary_missing", "timeout", "timeout_exit_status", "output_guard", "spawn",
    "no_output", "output_unverifiable", "unclassified", "oserror", "joberror", "cancelled",
])
async def test_ineligible_remux_failure_propagates_without_fallback(
    pipeline: Pipeline, error: BaseException,
) -> None:
    tasks = tracked_helpers(pipeline)
    pipeline.download_ext = "webm"
    pipeline.convert_error = error
    with pytest.raises(type(error)) as caught:
        await pipeline.run(opus_plan(), original_remux=pipeline.remux())
    assert caught.value is error
    assert pipeline.fallbacks == 0
    assert [call[0] for call in pipeline.calls] == ["audio", "remux", "convert"]
    assert len(tasks) == 2 and all(task.done() for task in tasks)


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_at, expected", [
    ("audio", ["audio"]),
    ("remux", ["audio", "remux"]),
    # Cancel's SIGTERM surfaces as a non-zero exit: cancellation still wins.
    ("convert", ["audio", "remux", "convert"]),
])
async def test_cancel_around_remux_wins_over_exit_status_fallback(
    pipeline: Pipeline, cancel_at: str, expected: list[str],
) -> None:
    tasks = tracked_helpers(pipeline)
    pipeline.download_ext = "webm"
    pipeline.cancel_at = cancel_at
    pipeline.convert_error = exit_status()
    with pytest.raises(JobError) as caught:
        await pipeline.run(opus_plan(), original_remux=pipeline.remux())
    assert caught.value.code == JobErrorCode.CANCELLED
    assert [call[0] for call in pipeline.calls] == expected
    assert pipeline.fallbacks == 0
    assert tasks and all(task.done() for task in tasks)


@pytest.mark.anyio
async def test_task_cancellation_during_remux_is_not_swallowed(pipeline: Pipeline) -> None:
    pipeline.download_ext = "webm"
    pipeline.convert_block = True
    task = asyncio.create_task(pipeline.run(opus_plan(), original_remux=pipeline.remux()))
    try:
        await asyncio.wait_for(pipeline.converting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pipeline.fallbacks == 0
    finally:
        pipeline.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_remux_registration_race_terminates_drains_and_cancels(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The actual single runner registration hook, as in the download/merge race.
    execution = object.__new__(_JobExecution)
    execution._job_id = "a" * 32
    execution._cancel_event = pipeline.cancel
    execution._processes = {}
    execution._helpers = set()
    process = cast(asyncio.subprocess.Process, type("Process", (), {"returncode": None})())
    terminating = asyncio.Event()
    terminated = []

    async def terminate(value: Any, grace: float) -> None:
        assert value is process and grace == 5.0
        terminating.set()
        await pipeline.release.wait()
        terminated.append(value)

    monkeypatch.setattr("app.services.job_runner.ensure_process_terminated", terminate)
    pipeline.process_hook = lambda _: execution._register_process(process)
    pipeline.download_ext = "webm"
    pipeline.cancel_at = "convert_registration"
    pipeline.convert_error = exit_status()
    task = asyncio.create_task(pipeline.run(opus_plan(), original_remux=pipeline.remux()))
    try:
        await asyncio.wait_for(terminating.wait(), 2)
        assert not task.done()
        pipeline.release.set()
        with pytest.raises(JobError) as caught:
            await task
        assert caught.value.code == JobErrorCode.CANCELLED
        assert terminated == [process]
        assert pipeline.fallbacks == 0
        assert all(helper.done() for helper in execution._helpers)
    finally:
        pipeline.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("raw_audio, kind, ext, enabled, child", [
    ("opus", "audio", "webm", False, False),
    ("opus", "audio", "webm", False, True),
    ("mp4a.40.2", "audio", "webm", True, True),
    ("opus", "audio", "m4a", True, False),
    ("opus", "audio", "mp4", True, True),
    ("opus", "audio", "WEBM", True, False),
    ("opus", "muxed", "webm", True, True),
], ids=["default_off", "child_default_off", "aac", "m4a", "child_mp4", "upper_suffix",
        "child_muxed"])
async def test_ineligible_original_returns_download_without_remux(
    pipeline: Pipeline, raw_audio: str, kind: str, ext: str, enabled: bool, child: bool,
) -> None:
    pipeline.download_ext = ext
    plan = opus_plan(raw_audio, kind)
    result = await pipeline.run(
        plan, original_remux=pipeline.remux() if enabled else None, child_output=child,
    )
    assert result.name == f"media.{ext}"
    assert [call[0] for call in pipeline.calls] == [plan.selected.format_id]
    assert not pipeline.conversions and pipeline.fallbacks == 0


@pytest.mark.anyio
@pytest.mark.parametrize("fails", [False, True], ids=["remuxed", "exit_status_fallback"])
async def test_child_mode_opts_into_original_remux(
    pipeline: Pipeline, tmp_path: Path, fails: bool,
) -> None:
    # M6.5c: the child CLI keeps its single-output contract and opts in too.
    log = tmp_path / "argv.log"
    binary = fake_ffmpeg_partial_fail(tmp_path) if fails else fake_ffmpeg_record(tmp_path, log)
    tasks = tracked_helpers(pipeline)
    media = webm_source(pipeline)
    result = await pipeline.run(
        opus_plan(), ffmpeg=FFmpegProcessor(binary=binary), original_remux=pipeline.remux(),
        child_output=True,
    )
    assert [call[0] for call in pipeline.calls] == ["audio", "remux"]
    assert media.read_bytes() == b"webm-source"
    if fails:
        assert result == media and pipeline.fallbacks == 1
        assert [path.name for path in media.parent.iterdir()] == ["media.webm"]
    else:
        assert_fresh(result, "remuxed", "opus")
        assert pipeline.fallbacks == 0
        assert recorded(log) == [[
            "-hide_banner", "-nostats", "-nostdin", "-y", "-i", str(media),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-c:a", "copy", "-f", "opus", str(result),
        ]]
    assert len(tasks) == 2 and all(task.done() for task in tasks)


@pytest.mark.anyio
async def test_remux_hooks_leave_merge_and_explicit_output_unchanged(pipeline: Pipeline) -> None:
    pipeline.download_ext = "webm"
    hooks = pipeline.remux()
    merged = await pipeline.run(
        resolve_native_child(source(video_codec="vp9", audio_codec="opus"), mode="video"),
        original_remux=hooks,
    )
    assert_fresh(merged, "merged", "mkv")
    converted = await pipeline.run(opus_plan(), "b" * 32, output="opus", original_remux=hooks)
    assert_fresh(converted, "converted", "opus")
    assert pipeline.conversions == [(converted.parent / "media.webm", True, False)]
    error = pipeline.convert_error = exit_status()
    with pytest.raises(FFmpegError) as caught:
        await pipeline.run(opus_plan(), "c" * 32, output="opus", original_remux=hooks)
    assert caught.value is error
    assert "remux" not in [call[0] for call in pipeline.calls]
    assert pipeline.fallbacks == 0


@pytest.mark.anyio
async def test_production_ffmpeg_uses_configured_conversion_timeout(settings: Settings) -> None:
    assert Settings.model_fields["ffmpeg_conversion_timeout_seconds"].default == 3600.0
    assert Settings.model_fields["ffprobe_binary"].default == "ffprobe"
    application = create_app(settings.model_copy(update={
        "ffmpeg_timeout_seconds": 12.0, "ffmpeg_conversion_timeout_seconds": 42.0,
        "ffprobe_binary": "custom-ffprobe", "ffprobe_timeout_seconds": 7.0}))
    try:
        ffmpeg = application.state.queue_manager._deps.ffmpeg
        assert ffmpeg._timeout_seconds == 12.0
        assert ffmpeg._conversion_timeout_seconds == 42.0
        assert ffmpeg._ffprobe_binary == "custom-ffprobe"
        assert ffmpeg._probe_timeout_seconds == 7.0
    finally:
        await application.state.engine.dispose()


def _staged(tmp_path: Path) -> tuple[StorageService, Path]:
    storage = StorageService(tmp_path / "staging", tmp_path / "library")
    media = storage.job_staging_dir("job") / "final.mp4"
    media.write_bytes(b"staged")
    return storage, media


def _fake_ffprobe(directory: Path, body: str) -> str:
    path = directory / "ffprobe"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


@pytest.mark.anyio
async def test_measure_final_dimensions_registers_process_and_drains_hook_task(
    tmp_path: Path,
) -> None:
    storage, media = _staged(tmp_path)
    binary = _fake_ffprobe(
        tmp_path, """echo '{"streams": [{"width": 1080, "height": 1920}]}'""")
    registered: list[asyncio.subprocess.Process] = []
    drained: list[bool] = []

    async def helper() -> None:
        await asyncio.sleep(0.05)
        drained.append(True)

    def hook(process: asyncio.subprocess.Process) -> asyncio.Task[Any]:
        registered.append(process)
        return asyncio.get_running_loop().create_task(helper())

    async def never_cancelled() -> None:
        return None

    result = await measure_final_dimensions(
        media, storage=storage, staging_key="job",
        ffmpeg=FFmpegProcessor(ffprobe_binary=binary),
        check_cancelled=never_cancelled, on_process_started=hook,
    )
    assert result == (1080, 1920) and result.label == "1080p"
    assert len(registered) == 1 and drained == [True]  # drained before returning
    assert media.read_bytes() == b"staged"


@pytest.mark.anyio
async def test_measure_final_dimensions_unknown_on_probe_failure(tmp_path: Path) -> None:
    storage, media = _staged(tmp_path)

    async def never_cancelled() -> None:
        return None

    for binary in (_fake_ffprobe(tmp_path, "echo 'secret diagnostics' >&2; exit 1"),
                   str(tmp_path / "missing-ffprobe")):
        result = await measure_final_dimensions(
            media, storage=storage, staging_key="job",
            ffmpeg=FFmpegProcessor(ffprobe_binary=binary),
            check_cancelled=never_cancelled, on_process_started=lambda process: None,
        )
        assert result is None
    assert media.read_bytes() == b"staged"


@pytest.mark.anyio
async def test_measure_final_dimensions_cancelled_before_probe_never_spawns(
    tmp_path: Path,
) -> None:
    storage, media = _staged(tmp_path)
    spawned = tmp_path / "spawned"
    binary = _fake_ffprobe(tmp_path, f"touch {spawned}")

    async def cancelled() -> None:
        raise JobError(JobErrorCode.CANCELLED)

    with pytest.raises(JobError) as caught:
        await measure_final_dimensions(
            media, storage=storage, staging_key="job",
            ffmpeg=FFmpegProcessor(ffprobe_binary=binary),
            check_cancelled=cancelled, on_process_started=lambda process: None,
        )
    assert caught.value.code == JobErrorCode.CANCELLED
    assert not spawned.exists() and media.read_bytes() == b"staged"


@pytest.mark.anyio
@pytest.mark.parametrize("kind", [
    "symlink_leaf", "symlink_parent", "symlink_job_dir", "fifo", "directory", "missing",
    "other_job", "outside_staging", "traversal",
])
async def test_measure_final_dimensions_prevalidates_staging_before_spawning(
    tmp_path: Path, kind: str,
) -> None:
    storage, media = _staged(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "final.mp4").write_bytes(b"outside")
    job_dir = media.parent
    if kind == "symlink_leaf":
        media.unlink()
        media.symlink_to(outside / "final.mp4")
        target = media
    elif kind == "symlink_parent":
        (job_dir / "nested").symlink_to(outside, target_is_directory=True)
        target = job_dir / "nested" / "final.mp4"
    elif kind == "symlink_job_dir":
        shutil.rmtree(job_dir)
        job_dir.symlink_to(outside, target_is_directory=True)
        target = job_dir / "final.mp4"
    elif kind == "fifo":
        media.unlink()
        os.mkfifo(media)
        target = media
    elif kind == "directory":
        media.unlink()
        media.mkdir()
        target = media
    elif kind == "missing":
        media.unlink()
        target = media
    elif kind == "other_job":
        target = storage.job_staging_dir("other") / "final.mp4"
        target.write_bytes(b"other")
    elif kind == "outside_staging":
        target = outside / "final.mp4"
    else:
        target = job_dir / ".." / "other" / "final.mp4"
    spawned = tmp_path / "spawned"
    binary = _fake_ffprobe(
        tmp_path, f"touch {spawned}; echo '{{\"streams\": [{{\"width\": 9, \"height\": 9}}]}}'")

    async def never_cancelled() -> None:
        return None

    result = await asyncio.wait_for(measure_final_dimensions(
        target, storage=storage, staging_key="job",
        ffmpeg=FFmpegProcessor(ffprobe_binary=binary),
        check_cancelled=never_cancelled, on_process_started=lambda process: None,
    ), timeout=5)
    assert result is None and not spawned.exists()
    assert (outside / "final.mp4").read_bytes() == b"outside"


def _aliased_staged(tmp_path: Path) -> tuple[StorageService, Path, Path]:
    # Configured staging root is a symlink alias of the real directory.
    (tmp_path / "real-staging").mkdir()
    alias = tmp_path / "staging-alias"
    alias.symlink_to(tmp_path / "real-staging", target_is_directory=True)
    storage = StorageService(alias, tmp_path / "library")
    media = storage.job_staging_dir("job") / "final.mp4"  # handed out resolved
    media.write_bytes(b"staged")
    return storage, media, alias


@pytest.mark.anyio
@pytest.mark.parametrize("spelling", ["resolved", "alias"])
async def test_measure_final_dimensions_works_under_aliased_staging_root(
    tmp_path: Path, spelling: str,
) -> None:
    storage, media, alias = _aliased_staged(tmp_path)
    assert media.parent.parent == (tmp_path / "real-staging").resolve()
    target = media if spelling == "resolved" else alias / "job" / "final.mp4"
    binary = _fake_ffprobe(tmp_path, """echo '{"streams": [{"width": 1920, "height": 1080}]}'""")

    async def never_cancelled() -> None:
        return None

    result = await measure_final_dimensions(
        target, storage=storage, staging_key="job",
        ffmpeg=FFmpegProcessor(ffprobe_binary=binary),
        check_cancelled=never_cancelled, on_process_started=lambda process: None,
    )
    assert result == (1920, 1080) and result.label == "1080p"
    assert media.read_bytes() == b"staged"


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["symlink_leaf", "symlink_job_dir", "outside_staging"])
async def test_measure_final_dimensions_aliased_root_still_rejects_outside(
    tmp_path: Path, kind: str,
) -> None:
    storage, media, _ = _aliased_staged(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "final.mp4").write_bytes(b"outside")
    target = media
    if kind == "symlink_leaf":
        media.unlink()
        media.symlink_to(outside / "final.mp4")
    elif kind == "symlink_job_dir":
        shutil.rmtree(media.parent)
        media.parent.symlink_to(outside, target_is_directory=True)
    else:
        target = outside / "final.mp4"
    spawned = tmp_path / "spawned"
    binary = _fake_ffprobe(
        tmp_path, f"touch {spawned}; echo '{{\"streams\": [{{\"width\": 9, \"height\": 9}}]}}'")

    async def never_cancelled() -> None:
        return None

    result = await measure_final_dimensions(
        target, storage=storage, staging_key="job",
        ffmpeg=FFmpegProcessor(ffprobe_binary=binary),
        check_cancelled=never_cancelled, on_process_started=lambda process: None,
    )
    assert result is None and not spawned.exists()
    assert (outside / "final.mp4").read_bytes() == b"outside"


@pytest.mark.anyio
async def test_measure_final_dimensions_non_whitelisted_format_is_unknown(
    tmp_path: Path,
) -> None:
    storage = StorageService(tmp_path / "staging", tmp_path / "library")
    playlist = storage.job_staging_dir("job") / "final.m3u8"
    playlist.write_text("#EXTM3U\n/elsewhere/video.mp4\n")
    argv = tmp_path / "argv"
    # Mimic ffprobe refusing a demuxer outside -format_whitelist.
    binary = _fake_ffprobe(
        tmp_path,
        f"""printf '%s\\n' "$@" > {argv}
echo "[hls] Format not on whitelist 'mov,matroska'" >&2; exit 1""")

    async def never_cancelled() -> None:
        return None

    result = await measure_final_dimensions(
        playlist, storage=storage, staging_key="job",
        ffmpeg=FFmpegProcessor(ffprobe_binary=binary),
        check_cancelled=never_cancelled, on_process_started=lambda process: None,
    )
    assert result is None  # unknown, no exception: the job carries on
    args = argv.read_text().splitlines()
    assert args[args.index("-format_whitelist") + 1] == "mov,matroska"
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    assert args.index("-protocol_whitelist") < args.index("-i")


@pytest.mark.anyio
async def test_measure_final_dimensions_probes_relative_staging_by_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    storage = StorageService(Path("staging"), Path("library"))
    media = storage.job_staging_dir("job") / "final.mp4"
    media.write_bytes(b"staged")
    argv = tmp_path / "argv"
    binary = _fake_ffprobe(
        tmp_path,
        f"""printf '%s\\n' "$@" > {argv}; echo '{{"streams": [{{"width": 3, "height": 2}}]}}'""")

    async def never_cancelled() -> None:
        return None

    result = await measure_final_dimensions(
        media, storage=storage, staging_key="job",
        ffmpeg=FFmpegProcessor(ffprobe_binary=binary),
        check_cancelled=never_cancelled, on_process_started=lambda process: None,
    )
    assert result == (3, 2)
    assert argv.read_text().splitlines()[-1] == str(media.absolute())
