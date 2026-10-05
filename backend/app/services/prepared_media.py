"""Internal sequential media preparation; no job lifecycle or final placement.

Inputs are backend-resolved streams, not client selectors. The caller owns
cancellation/process registration and progress meaning; returned hook tasks
are drained before this boundary returns, including on failure/cancellation.
Child callers opt into the bounded child CLI; the single CLI is unchanged.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.domain.analysis import NormalizedFormat
from app.domain.jobs import JobError, JobErrorCode
from app.integrations.ffmpeg import FFmpegProcessor
from app.integrations.ytdlp import DownloadProgress, YtDlpDownloader
from app.services.storage_service import StorageService

HookTask = asyncio.Task[Any] | None


def merge_container(video: NormalizedFormat, audio: NormalizedFormat) -> str:
    if audio.audio_codec == "aac" and video.video_codec in {"h264", "hevc"}:
        return "mp4"
    return "mkv"


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
) -> Path:
    """Prepare one selected stream, optionally sequentially download and merge.

    Adapters retain subprocess ownership; registration must handle cancellation
    between the pre-step check and spawn. Hooks must return any scheduled helper
    so it cannot escape this invocation. No placement, cleanup or DB writes here.
    """
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

    try:
        staging_dir = storage.job_staging_dir(staging_key)
        if selected.kind == "video_only" and companion is not None:
            video = await download("video", selected.format_id, 0, 2)
            audio = await download("audio", companion.format_id, 1, 2)
            await check_cancelled()
            await before_merge()
            # The caller's status/activity hook may yield to a Cancel request.
            await check_cancelled()
            merged = staging_dir / f"merged.{merge_container(selected, companion)}"
            await ffmpeg.merge_streams(video, audio, merged, on_process_started=register)
            return merged
        return await download("media", selected.format_id, 0, 1)
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
