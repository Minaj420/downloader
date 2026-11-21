"""FFmpeg integration adapter.

FFmpeg also lives behind an integration boundary (ARCHITECTURE.md §14) and is
invoked with argument arrays only. Merging uses stream copy (``-c copy``):
no re-encoding and no quality changes. Conversion to an explicitly selected
output profile uses one fixed argument set per profile; it is a compatibility
conversion, never a quality increase.
"""

import asyncio
import contextlib
import json
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


class FFmpegFailureCause(StrEnum):
    """Adapter-internal refinement of ``FFmpegFailureKind.FAILED``.

    Purely descriptive: no cause authorises a fallback on its own, and the
    external ``kind``/job error mapping is unchanged.

    - ``OUTPUT_GUARD``: the output target already exists or could not be
      checked before spawning; FFmpeg never ran.
    - ``SPAWN``: the binary exists but could not be started.
    - ``EXIT_STATUS``: FFmpeg exited non-zero. This does NOT reliably
      separate media/codec problems from disk-full or permission errors
      while writing; stderr is deliberately not parsed.
    - ``NO_OUTPUT``: FFmpeg exited 0 but the output is missing or empty.
    - ``OUTPUT_UNVERIFIABLE``: FFmpeg exited 0 but the output could not be
      checked (stat failed with an error other than "not found").
    """

    OUTPUT_GUARD = "output_guard"
    SPAWN = "spawn"
    EXIT_STATUS = "exit_status"
    NO_OUTPUT = "no_output"
    OUTPUT_UNVERIFIABLE = "output_unverifiable"


class FFmpegError(Exception):
    def __init__(
        self,
        kind: FFmpegFailureKind,
        detail: str | None = None,
        *,
        cause: FFmpegFailureCause | None = None,
    ) -> None:
        self.kind = kind
        self.detail = detail
        # ``None`` means "not classified" (e.g. BINARY_MISSING, TIMEOUT); it
        # is never a fallback permission.
        self.cause = cause
        super().__init__(f"{kind.value}: {detail or ''}")


def _discard_output(output_path: Path) -> None:
    # Best effort: never mask the original failure or cancellation.
    try:
        output_path.unlink(missing_ok=True)
    except OSError:
        pass


# Demuxer names as FFmpeg lists them ("mov,mp4,m4a,3gp,3g2,mj2" and
# "matroska,webm"); ffprobe matches any shared token.
_PROBE_FORMAT_WHITELIST = "mov,matroska"


class VideoDimensions(NamedTuple):
    """Measured positive pixel dimensions of the first video stream."""

    width: int
    height: int

    @property
    def short_edge(self) -> int:
        return min(self.width, self.height)

    @property
    def label(self) -> str:
        # Short-edge label: 1080x1920 portrait is "1080p" like 1920x1080.
        return f"{self.short_edge}p"


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _parse_dimensions(stdout: bytes) -> VideoDimensions | None:
    # Best effort: anything but one stream with positive integer width/height
    # is "unknown"; nothing is guessed or defaulted.
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        return None
    width = _positive_int(streams[0].get("width"))
    height = _positive_int(streams[0].get("height"))
    if width is None or height is None:
        return None
    return VideoDimensions(width, height)


class FFmpegProcessor:
    def __init__(
        self,
        binary: str = "ffmpeg",
        timeout_seconds: float = 600.0,
        conversion_timeout_seconds: float = 3600.0,
        ffprobe_binary: str = "ffprobe",
        probe_timeout_seconds: float = 30.0,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._conversion_timeout_seconds = conversion_timeout_seconds
        self._ffprobe_binary = ffprobe_binary
        self._probe_timeout_seconds = probe_timeout_seconds

    async def probe_video_dimensions(
        self,
        media_path: Path,
        on_process_started: Callable[[asyncio.subprocess.Process], None] | None = None,
    ) -> VideoDimensions | None:
        """Measure width/height of the first real video stream; read-only, best effort.

        Uppercase ``V:0`` excludes attached pictures (cover art/thumbnails).

        Missing binary, spawn failure, non-zero exit, timeout and invalid or
        empty stream data all return ``None`` (unknown); raw tool output is
        never surfaced. Cancellation terminates the child and propagates. The
        probed file is never modified or removed.

        Only the MP4/MOV and Matroska/WebM demuxers over the ``file``
        protocol may open input, so a staged HLS/concat/playlist-style file
        cannot make ffprobe read a second input; other formats exit non-zero
        and are therefore unknown.
        """
        command = [
            self._ffprobe_binary, "-v", "error", "-select_streams", "V:0",
            "-show_entries", "stream=width,height", "-of", "json",
            "-format_whitelist", _PROBE_FORMAT_WHITELIST,
            "-protocol_whitelist", "file",
            "-i", str(media_path),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            if on_process_started is not None:
                on_process_started(process)
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self._probe_timeout_seconds
            )
        except TimeoutError:
            await _terminate(process)
            return None
        except BaseException:
            await _terminate(process)
            raise
        if process.returncode != 0:
            return None
        return _parse_dimensions(stdout)

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
        # Never let FFmpeg (argv carries ``-y``) or the failure cleanup below
        # touch an entry that already exists at the target: regular file,
        # symlink (dangling or not), directory or anything else. Only a
        # missing target proceeds; an unverifiable one fails closed. This is a
        # precheck for a private single-writer staging dir, not a race guard.
        try:
            output_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise FFmpegError(
                FFmpegFailureKind.FAILED,
                detail=f"ffmpeg output could not be checked: {exc}",
                cause=FFmpegFailureCause.OUTPUT_GUARD,
            ) from exc
        else:
            raise FFmpegError(
                FFmpegFailureKind.FAILED,
                detail=f"ffmpeg output already exists: {output_path}",
                cause=FFmpegFailureCause.OUTPUT_GUARD,
            )

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
                FFmpegFailureKind.FAILED,
                detail=f"ffmpeg could not be started: {exc}",
                cause=FFmpegFailureCause.SPAWN,
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
            # Non-zero exit; may also be disk-full/permission, see the cause docs.
            raise FFmpegError(
                FFmpegFailureKind.FAILED, detail=tail, cause=FFmpegFailureCause.EXIT_STATUS
            )
        if require_output:
            try:
                has_output = _has_output(output_path)
            except OSError as exc:
                _discard_output(output_path)
                raise FFmpegError(
                    FFmpegFailureKind.FAILED,
                    detail=f"ffmpeg output could not be checked: {exc}",
                    cause=FFmpegFailureCause.OUTPUT_UNVERIFIABLE,
                ) from exc
            if not has_output:
                _discard_output(output_path)
                raise FFmpegError(
                    FFmpegFailureKind.FAILED,
                    detail="ffmpeg produced no output",
                    cause=FFmpegFailureCause.NO_OUTPUT,
                )


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
    # Only "not found" means no output; any other OSError propagates so the
    # caller can report it as unverifiable instead of a plain missing output.
    try:
        return output_path.stat().st_size > 0
    except FileNotFoundError:
        return False
