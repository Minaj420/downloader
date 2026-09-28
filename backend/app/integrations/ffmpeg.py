"""FFmpeg integration adapter.

FFmpeg also lives behind an integration boundary (ARCHITECTURE.md §14) and is
invoked with argument arrays only. Merging uses stream copy (``-c copy``):
no re-encoding and no quality changes.
"""

import asyncio
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from app.utils.processes import ensure_process_terminated

_STDERR_TAIL_BYTES = 4_000
_TERMINATION_GRACE_SECONDS = 5.0


class FFmpegFailureKind(StrEnum):
    BINARY_MISSING = "binary_missing"
    FAILED = "failed"
    TIMEOUT = "timeout"


class FFmpegError(Exception):
    def __init__(self, kind: FFmpegFailureKind, detail: str | None = None) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind.value}: {detail or ''}")


class FFmpegProcessor:
    def __init__(self, binary: str = "ffmpeg", timeout_seconds: float = 600.0) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds

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
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
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
        if on_process_started is not None:
            on_process_started(process)

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout_seconds
            )
        except TimeoutError:
            await ensure_process_terminated(process, _TERMINATION_GRACE_SECONDS)
            raise FFmpegError(
                FFmpegFailureKind.TIMEOUT,
                detail=f"ffmpeg exceeded {self._timeout_seconds}s",
            ) from None
        except BaseException:
            # Task cancellation or another interruption: terminate the child so
            # it cannot outlive us, then let the cancellation propagate.
            await ensure_process_terminated(process, _TERMINATION_GRACE_SECONDS)
            raise

        if process.returncode != 0:
            tail = stderr[-_STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")
            raise FFmpegError(FFmpegFailureKind.FAILED, detail=tail)
