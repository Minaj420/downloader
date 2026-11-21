"""Internal sequential media preparation; no job lifecycle or final placement.

Inputs are backend-resolved streams, not client selectors. The caller owns
cancellation/process registration and progress meaning; returned hook tasks
are drained before this boundary returns, including on failure/cancellation.
Child callers opt into the bounded child CLI; the single CLI is unchanged.
"""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NamedTuple

from app.domain.analysis import NormalizedFormat
from app.domain.jobs import JobError, JobErrorCode
from app.integrations.ffmpeg import (
    ConversionProfile,
    FFmpegError,
    FFmpegFailureCause,
    FFmpegFailureKind,
    FFmpegProcessor,
    VideoDimensions,
    output_extension,
)
from app.integrations.ytdlp import DownloadProgress, YtDlpDownloader
from app.services.conversion_plan import plan_conversion
from app.services.storage_service import StorageService

HookTask = asyncio.Task[Any] | None


class OriginalRemux(NamedTuple):
    """Opt-in Original WebM/Opus → ``.opus`` lossless remux (PRODUCT_SPEC §14).

    ``before`` is the caller's status hook, awaited once before FFmpeg runs.
    ``on_fallback`` is a synchronous recorder called at most once when the
    remux failed with a non-zero FFmpeg exit and the job was not cancelled;
    the caller then publishes the returned original WebM with its warning.
    """

    before: Callable[[], Awaitable[None]]
    on_fallback: Callable[[], None]


def _remux_eligible(selected: NormalizedFormat, source: Path) -> bool:
    # The downloaded file's real suffix decides, never the analysis ext.
    return (
        selected.kind == "audio_only"
        and selected.audio_codec == "opus"
        and source.suffix == ".webm"
    )


def _fresh_name(stem: str, extension: str) -> str:
    # The FFmpeg adapter refuses existing targets; a fresh name per call lets a
    # retry reuse staging without touching outputs left by an earlier attempt.
    return f"{stem}-{secrets.token_hex(8)}.{extension}"


def merge_container(video: NormalizedFormat, audio: NormalizedFormat) -> str:
    if audio.audio_codec == "aac" and video.video_codec in {"h264", "hevc"}:
        return "mp4"
    return "mkv"


async def measure_final_dimensions(
    media: Path,
    *,
    storage: StorageService,
    staging_key: str,
    ffmpeg: FFmpegProcessor,
    check_cancelled: Callable[[], Awaitable[None]],
    on_process_started: Callable[[asyncio.subprocess.Process], HookTask],
) -> VideoDimensions | None:
    """Measure the staged final VIDEO output; ``None`` means unknown.

    Read-only and best effort (adapter returns ``None`` for every probe
    failure); cancellation propagates and the staged file is never touched.
    Before spawning, storage must confirm ``media`` is a regular file inside
    ``staging_key``'s own staging (no symlinks, FIFO-safe); otherwise the
    result is unknown, no process starts, and placement later reports its
    usual error. This precheck does not pin the inode the probe reopens.
    Callers must not use it for audio and must keep ``prepare_media``'s result
    for placement. Hook tasks are drained before returning, also on failure.
    """
    helpers: set[asyncio.Task[Any]] = set()

    def register(process: asyncio.subprocess.Process) -> None:
        task = on_process_started(process)
        if task is not None:
            helpers.add(task)

    try:
        await check_cancelled()
        # Same cwd interpretation as storage's walk; ffprobe gets an absolute
        # local path (never a protocol-looking or option-looking relative name).
        media = media.absolute()
        if not storage.is_staged_regular_file(staging_key, media):
            return None
        return await ffmpeg.probe_video_dimensions(media, on_process_started=register)
    finally:
        if helpers:
            drain = asyncio.gather(*helpers, return_exceptions=True)
            cancelled = False
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError


async def prepare_media(
    *,
    selected: NormalizedFormat,
    companion: NormalizedFormat | None,
    source_url: str,
    staging_key: str,
    storage: StorageService,
    downloader: YtDlpDownloader,
    ffmpeg: FFmpegProcessor,
    check_cancelled: Callable[[], Awaitable[None]],
    on_process_started: Callable[[asyncio.subprocess.Process], HookTask],
    progress_forwarder: Callable[[int, int], Callable[[DownloadProgress], HookTask]],
    before_merge: Callable[[], Awaitable[None]],
    child_output: bool = False,
    output: ConversionProfile | None = None,
    original_remux: OriginalRemux | None = None,
) -> Path:
    """Prepare one selected stream, optionally sequentially download and merge.

    Adapters retain subprocess ownership; registration must handle cancellation
    between the pre-step check and spawn. Hooks must return any scheduled helper
    so it cannot escape this invocation. No placement, cleanup or DB writes here.
    With ``output`` the result is converted last; the plan is validated before
    any media I/O and ``before_merge`` still runs once, before the first
    post-processing step. Intermediate files stay in staging for the caller.

    ``original_remux`` applies only without ``output`` (single and child
    callers alike), to an audio-only Opus download whose real file is
    ``.webm``. Only an ``EXIT_STATUS`` failure, after a fresh cancellation
    check, falls back to the untouched source; every other failure propagates
    unchanged. An explicit ``output`` never falls back.
    """
    plan = plan_conversion(output, selected, companion) if output is not None else None
    closed = False
    helpers: set[asyncio.Task[Any]] = set()

    def register(process: asyncio.subprocess.Process) -> None:
        task = on_process_started(process)
        if task is not None:
            helpers.add(task)

    async def download(prefix: str, format_id: str, index: int, count: int) -> Path:
        await check_cancelled()
        forward = progress_forwarder(index, count)

        def progress(value: DownloadProgress) -> None:
            if closed:
                return
            task = forward(value)
            if task is not None:
                helpers.add(task)

        options = {"child_output": True} if child_output else {}
        produced = await downloader.download(
            url=source_url,
            format_selector=format_id,
            output_template=str(staging_dir / f"{prefix}.%(ext)s"),
            on_progress=progress,
            on_process_started=register,
            **options,
        )
        if not produced or (child_output and len(produced) != 1):
            raise JobError(
                JobErrorCode.MALFORMED_OUTPUT,
                detail=f"download produced no file for format {format_id}",
            )
        return Path(produced[0])

    async def remux(source: Path, hooks: OriginalRemux) -> Path:
        await check_cancelled()
        await hooks.before()
        # The caller's status/activity hook may yield to a Cancel request.
        await check_cancelled()
        remuxed = staging_dir / _fresh_name("remuxed", "opus")
        try:
            await ffmpeg.convert(
                source, remuxed, "opus", copy_audio=True, on_process_started=register,
            )
        except FFmpegError as exc:
            if (
                exc.kind is not FFmpegFailureKind.FAILED
                or exc.cause is not FFmpegFailureCause.EXIT_STATUS
            ):
                raise
            # A non-zero exit may be the SIGTERM sent for Cancel: cancellation wins.
            await check_cancelled()
            hooks.on_fallback()
            return source
        return remuxed

    try:
        staging_dir = storage.job_staging_dir(staging_key)
        if selected.kind == "video_only" and companion is not None:
            video = await download("video", selected.format_id, 0, 2)
            audio = await download("audio", companion.format_id, 1, 2)
            await check_cancelled()
            await before_merge()
            # The caller's status/activity hook may yield to a Cancel request.
            await check_cancelled()
            merged = staging_dir / _fresh_name("merged", merge_container(selected, companion))
            await ffmpeg.merge_streams(video, audio, merged, on_process_started=register)
            if output is None or plan is None:
                return merged
            source = merged
        else:
            source = await download("media", selected.format_id, 0, 1)
            if output is None or plan is None:
                if original_remux is None or not _remux_eligible(selected, source):
                    return source
                return await remux(source, original_remux)
            await check_cancelled()
            await before_merge()
        await check_cancelled()
        converted = staging_dir / _fresh_name("converted", output_extension(output))
        await ffmpeg.convert(
            source, converted, output,
            copy_audio=plan.copy_audio, copy_video=plan.copy_video,
            on_process_started=register,
        )
        return converted
    finally:
        closed = True
        if helpers:
            drain = asyncio.gather(*helpers, return_exceptions=True)
            cancelled = False
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
