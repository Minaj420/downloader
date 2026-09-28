import asyncio
import time
from pathlib import Path

import pytest

from app.integrations import ffmpeg as ffmpeg_module
from app.integrations.ffmpeg import FFmpegError, FFmpegFailureKind, FFmpegProcessor

MERGE_SCRIPT = """#!/bin/sh
out=""
for a in "$@"; do out="$a"; done
printf 'merged' > "$out"
"""

FAIL_SCRIPT = """#!/bin/sh
echo 'Error: invalid codec' >&2
exit 1
"""

SLOW_SCRIPT = """#!/bin/sh
touch {started}
exec sleep 30
"""

IGNORE_TERM_SCRIPT = """#!/bin/sh
trap '' TERM
touch {started}
sleep 30
"""


def _write_script(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def _merge_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.m4a"
    output = tmp_path / "merged.mkv"
    video.write_text("v")
    audio.write_text("a")
    return video, audio, output


async def _wait_for_started(started: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not started.exists():
        if time.monotonic() > deadline:
            raise AssertionError("ffmpeg child never signalled startup")
        await asyncio.sleep(0.01)


def test_merge_streams_copies_without_reencode(tmp_path: Path) -> None:
    binary = _write_script(tmp_path, "ffmpeg", MERGE_SCRIPT)
    processor = FFmpegProcessor(binary=binary, timeout_seconds=10)
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.m4a"
    output = tmp_path / "merged.mkv"
    video.write_text("v")
    audio.write_text("a")

    asyncio.run(processor.merge_streams(video, audio, output))

    assert output.read_text() == "merged"


def test_merge_failure_raises(tmp_path: Path) -> None:
    binary = _write_script(tmp_path, "ffmpeg", FAIL_SCRIPT)
    processor = FFmpegProcessor(binary=binary, timeout_seconds=10)

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(
            processor.merge_streams(
                tmp_path / "v.mp4", tmp_path / "a.m4a", tmp_path / "out.mkv"
            )
        )

    assert exc_info.value.kind is FFmpegFailureKind.FAILED
    assert "invalid codec" in (exc_info.value.detail or "")


def test_merge_missing_binary(tmp_path: Path) -> None:
    processor = FFmpegProcessor(binary=str(tmp_path / "missing-ffmpeg"), timeout_seconds=10)

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(
            processor.merge_streams(
                tmp_path / "v.mp4", tmp_path / "a.m4a", tmp_path / "out.mkv"
            )
        )

    assert exc_info.value.kind is FFmpegFailureKind.BINARY_MISSING


def test_cancellation_terminates_child_process(tmp_path: Path) -> None:
    """A cancelled merge must not leave an orphaned FFmpeg process behind."""
    started = tmp_path / "started"
    binary = _write_script(tmp_path, "ffmpeg", SLOW_SCRIPT.format(started=started))
    processor = FFmpegProcessor(binary=binary, timeout_seconds=10)
    video, audio, output = _merge_inputs(tmp_path)
    seen: list[asyncio.subprocess.Process] = []

    async def run() -> None:
        task = asyncio.create_task(
            processor.merge_streams(video, audio, output, on_process_started=seen.append)
        )
        await _wait_for_started(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert len(seen) == 1
    assert seen[0].returncode is not None
    assert not output.exists()


def test_cancellation_kills_process_that_ignores_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the grace period the child is force killed, not left running."""
    monkeypatch.setattr(ffmpeg_module, "_TERMINATION_GRACE_SECONDS", 0.2)
    started = tmp_path / "started"
    binary = _write_script(tmp_path, "ffmpeg", IGNORE_TERM_SCRIPT.format(started=started))
    processor = FFmpegProcessor(binary=binary, timeout_seconds=10)
    video, audio, output = _merge_inputs(tmp_path)
    seen: list[asyncio.subprocess.Process] = []

    async def run() -> None:
        task = asyncio.create_task(
            processor.merge_streams(video, audio, output, on_process_started=seen.append)
        )
        await _wait_for_started(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert len(seen) == 1
    assert seen[0].returncode == -9
    assert not output.exists()


def test_merge_timeout_terminates_process_and_raises(tmp_path: Path) -> None:
    """The timeout path still raises TIMEOUT and reaps the child."""
    started = tmp_path / "started"
    binary = _write_script(tmp_path, "ffmpeg", SLOW_SCRIPT.format(started=started))
    processor = FFmpegProcessor(binary=binary, timeout_seconds=0.5)
    video, audio, output = _merge_inputs(tmp_path)
    seen: list[asyncio.subprocess.Process] = []

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(
            processor.merge_streams(video, audio, output, on_process_started=seen.append)
        )

    assert exc_info.value.kind is FFmpegFailureKind.TIMEOUT
    assert len(seen) == 1
    assert seen[0].returncode is not None
    assert not output.exists()
