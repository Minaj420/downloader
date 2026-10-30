"""FFmpeg integration adapter.

FFmpeg also lives behind an integration boundary (ARCHITECTURE.md §14) and is
invoked with argument arrays only. Merging uses stream copy (``-c copy``):
no re-encoding and no quality changes. Conversion to an explicitly selected
output profile uses one fixed argument set per profile; it is a compatibility
conversion, never a quality increase.
"""

import asyncio
import contextlib
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Literal, NamedTuple

from app.utils.processes import ensure_process_terminated

_STDERR_TAIL_BYTES = 4_000
_TERMINATION_GRACE_SECONDS = 5.0
_REAP_TIMEOUT_SECONDS = 5.0
_REAP_DRAIN_SECONDS = 1.0

# Local mirror of the domain output profiles minus ``original``; the adapter
# does not import the domain layer.
ConversionProfile = Literal["mp3", "wav", "m4a", "opus", "flac", "mp4"]


class _Profile(NamedTuple):
    extension: str
    muxer: str
    streams: tuple[str, ...]
    video_codec: tuple[str, ...]
    audio_codec: tuple[str, ...]
    container: tuple[str, ...]


_AUDIO_STREAMS = ("-map", "0:a:0", "-vn", "-sn", "-dn")
_FASTSTART = ("-movflags", "+faststart")

_PROFILES: dict[ConversionProfile, _Profile] = {
    "mp3": _Profile(
        "mp3", "mp3", _AUDIO_STREAMS, (),
        ("-c:a", "libmp3lame", "-q:a", "0"), ("-id3v2_version", "3"),
    ),
    "wav": _Profile(
        "wav", "wav", _AUDIO_STREAMS, (),
        ("-c:a", "pcm_s16le"), ("-rf64", "auto"),
    ),
    "m4a": _Profile(
        "m4a", "ipod", _AUDIO_STREAMS, (),
        ("-c:a", "aac", "-b:a", "256k"), _FASTSTART,
    ),
    "opus": _Profile(
        "opus", "opus", _AUDIO_STREAMS, (),
        ("-c:a", "libopus", "-b:a", "160k", "-vbr", "on"), (),
    ),
    "flac": _Profile(
        "flac", "flac", _AUDIO_STREAMS, (),
        ("-c:a", "flac", "-sample_fmt", "s16"), (),
    ),
    "mp4": _Profile(
        "mp4", "mp4", ("-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn"),
        # libx264 + yuv420p rejects odd dimensions; the filter is part of the
        # encode group because stream copy cannot be combined with filters.
        ("-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-profile:v", "high"),
        ("-c:a", "aac", "-b:a", "192k"), _FASTSTART,
    ),
}


def output_extension(profile: ConversionProfile) -> str:
    """File extension (without dot) produced by ``profile``."""
    return _PROFILES[profile].extension


class FFmpegFailureKind(StrEnum):
    BINARY_MISSING = "binary_missing"
    FAILED = "failed"
    TIMEOUT = "timeout"


class FFmpegError(Exception):
    def __init__(self, kind: FFmpegFailureKind, detail: str | None = None) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind.value}: {detail or ''}")


def _discard_output(output_path: Path) -> None:
    # Best effort: never mask the original failure or cancellation.
    try:
        output_path.unlink(missing_ok=True)
    except OSError:
        pass


class FFmpegProcessor:
    def __init__(
        self,
        binary: str = "ffmpeg",
        timeout_seconds: float = 600.0,
        conversion_timeout_seconds: float = 3600.0,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._conversion_timeout_seconds = conversion_timeout_seconds

    async def merge_streams(
        self,
        video_path: Path,
        audio_path: Path,
        output_path: Path,
        on_process_started: Callable[[asyncio.subprocess.Process], None] | None = None,
    ) -> None:
        """Merge a video-only and an audio stream without re-encoding."""
        command = [
            self._binary,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-c",
            "copy",
            str(output_path),
        ]
        await self._run(command, output_path, on_process_started, self._timeout_seconds)

    async def convert(
        self,
        input_path: Path,
        output_path: Path,
        profile: ConversionProfile,
        *,
        copy_audio: bool = False,
        copy_video: bool = False,
        on_process_started: Callable[[asyncio.subprocess.Process], None] | None = None,
    ) -> None:
        """Convert ``input_path`` into ``output_path`` using a fixed profile.

        ``copy_audio`` / ``copy_video`` replace the encoder arguments with
        stream copy; deciding whether the source stream is compatible is the
        caller's responsibility.
        """
        spec = _PROFILES[profile]
        if output_path.suffix != f".{spec.extension}":
            raise ValueError(
                f"output suffix {output_path.suffix!r} does not match profile {profile!r}"
            )
        if input_path.resolve() == output_path.resolve():
            raise ValueError("conversion output must differ from its input")
        if copy_video and profile != "mp4":
            raise ValueError(f"copy_video is not valid for profile {profile!r}")

        video_codec = ("-c:v", "copy") if copy_video else spec.video_codec
        audio_codec = ("-c:a", "copy") if copy_audio else spec.audio_codec
        command = [
            self._binary,
            "-hide_banner",
            "-nostats",
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            *spec.streams,
            *video_codec,
            *audio_codec,
            *spec.container,
            "-f",
            spec.muxer,
            str(output_path),
        ]
        await self._run(
            command,
            output_path,
            on_process_started,
            self._conversion_timeout_seconds,
            require_output=True,
        )

    async def _run(
        self,
        command: list[str],
        output_path: Path,
        on_process_started: Callable[[asyncio.subprocess.Process], None] | None,
        timeout_seconds: float,
        require_output: bool = False,
    ) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise FFmpegError(
                FFmpegFailureKind.BINARY_MISSING,
                detail=f"ffmpeg binary not found: {self._binary}",
            ) from exc
        except OSError as exc:
            raise FFmpegError(
                FFmpegFailureKind.FAILED, detail=f"ffmpeg could not be started: {exc}"
            ) from exc

        try:
            if on_process_started is not None:
                on_process_started(process)
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            try:
                await _terminate(process)
            finally:
                _discard_output(output_path)
            raise FFmpegError(
                FFmpegFailureKind.TIMEOUT,
                detail=f"ffmpeg exceeded {timeout_seconds}s",
            ) from None
        except BaseException:
            # Task cancellation or another interruption: terminate the child so
            # it cannot outlive us, drop its partial output, then propagate.
            try:
                await _terminate(process)
            finally:
                _discard_output(output_path)
            raise

        if process.returncode != 0:
            _discard_output(output_path)
            tail = stderr[-_STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")
            raise FFmpegError(FFmpegFailureKind.FAILED, detail=tail)
        if require_output and not _has_output(output_path):
            _discard_output(output_path)
            raise FFmpegError(FFmpegFailureKind.FAILED, detail="ffmpeg produced no output")


async def _terminate(process: asyncio.subprocess.Process) -> None:
    # A second cancellation during the grace period must not leave the child
    # running: force kill it, reap it, then re-raise the interruption unchanged.
    try:
        await ensure_process_terminated(process, _TERMINATION_GRACE_SECONDS)
    except BaseException:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await _reap_after_kill(process)
        raise


async def _reap_after_kill(process: asyncio.subprocess.Process) -> None:
    # Bounded and immune to further cancellations; the caller re-raises the
    # original interruption. A child stuck in the kernel past the bound is an
    # accepted risk. The waiter is owned here and never outlives this call.
    loop = asyncio.get_running_loop()
    waiter = asyncio.ensure_future(process.wait())
    waiter.add_done_callback(_consume_outcome)
    try:
        await _await_until(waiter, loop.time() + _REAP_TIMEOUT_SECONDS)
    finally:
        if not waiter.done():
            waiter.cancel()
            await _await_until(waiter, loop.time() + _REAP_DRAIN_SECONDS)


async def _await_until(waiter: asyncio.Future[int], deadline: float) -> None:
    loop = asyncio.get_running_loop()
    while not waiter.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        # asyncio.wait leaves ``waiter`` untouched when we are cancelled.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({waiter}, timeout=remaining)


def _consume_outcome(waiter: asyncio.Future[int]) -> None:
    if not waiter.cancelled():
        waiter.exception()


def _has_output(output_path: Path) -> bool:
    try:
        return output_path.stat().st_size > 0
    except OSError:
        return False
