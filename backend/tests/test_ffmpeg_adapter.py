import asyncio
import time
from pathlib import Path
from typing import get_args

import pytest

from app.domain.jobs import AUDIO_OUTPUT_PROFILES, ORIGINAL_OUTPUT_PROFILE, VIDEO_OUTPUT_PROFILES
from app.integrations import ffmpeg as ffmpeg_module
from app.integrations.ffmpeg import (
    ConversionProfile,
    FFmpegError,
    FFmpegFailureKind,
    FFmpegProcessor,
    output_extension,
)

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


# Fakes below write partial output to the last argument first, so cleanup
# assertions prove deletion rather than "never written".

ARGV_SCRIPT = """#!/bin/sh
printf '%s\\n' "$@" > {argv}
out=""
for a in "$@"; do out="$a"; done
printf 'converted' > "$out"
"""

PARTIAL_FAIL_SCRIPT = """#!/bin/sh
out=""
for a in "$@"; do out="$a"; done
printf 'partial' > "$out"
echo 'Error: encoder failed' >&2
exit 1
"""

PARTIAL_SLOW_SCRIPT = """#!/bin/sh
out=""
for a in "$@"; do out="$a"; done
printf 'partial' > "$out"
touch {started}
exec sleep 30
"""

PARTIAL_IGNORE_TERM_SCRIPT = """#!/bin/sh
trap '' TERM
out=""
for a in "$@"; do out="$a"; done
printf 'partial' > "$out"
touch {started}
sleep 30
"""

LONG_STDERR_SCRIPT = """#!/bin/sh
i=0
while [ $i -lt 600 ]; do printf 'noise-line\\n' >&2; i=$((i+1)); done
echo 'FINAL-MARKER' >&2
exit 1
"""

DELAYED_OK_SCRIPT = """#!/bin/sh
sleep 1
out=""
for a in "$@"; do out="$a"; done
printf 'done' > "$out"
"""

NO_OUTPUT_OK_SCRIPT = """#!/bin/sh
exit 0
"""

EMPTY_OUTPUT_OK_SCRIPT = """#!/bin/sh
out=""
for a in "$@"; do out="$a"; done
: > "$out"
exit 0
"""

_HEAD = ["-hide_banner", "-nostats", "-nostdin", "-y", "-i"]
_AUDIO_MAP = ["-map", "0:a:0", "-vn", "-sn", "-dn"]
_VIDEO_MAP = ["-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn"]
_X264 = ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "medium",
         "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high"]

EXPECTED_PROFILE_ARGS: dict[str, list[str]] = {
    "mp3": [*_AUDIO_MAP, "-c:a", "libmp3lame", "-q:a", "0", "-id3v2_version", "3",
            "-f", "mp3"],
    "wav": [*_AUDIO_MAP, "-c:a", "pcm_s16le", "-rf64", "auto", "-f", "wav"],
    "m4a": [*_AUDIO_MAP, "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart",
            "-f", "ipod"],
    "opus": [*_AUDIO_MAP, "-c:a", "libopus", "-b:a", "160k", "-vbr", "on", "-f", "opus"],
    "flac": [*_AUDIO_MAP, "-c:a", "flac", "-sample_fmt", "s16", "-f", "flac"],
    "mp4": [*_VIDEO_MAP, *_X264, "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-f", "mp4"],
}

EXPECTED_COPY_AUDIO_ARGS: dict[str, list[str]] = {
    "mp3": [*_AUDIO_MAP, "-c:a", "copy", "-id3v2_version", "3", "-f", "mp3"],
    "wav": [*_AUDIO_MAP, "-c:a", "copy", "-rf64", "auto", "-f", "wav"],
    "m4a": [*_AUDIO_MAP, "-c:a", "copy", "-movflags", "+faststart", "-f", "ipod"],
    "opus": [*_AUDIO_MAP, "-c:a", "copy", "-f", "opus"],
    "flac": [*_AUDIO_MAP, "-c:a", "copy", "-f", "flac"],
    "mp4": [*_VIDEO_MAP, *_X264, "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4"],
}

EXPECTED_EXTENSIONS = {
    "mp3": "mp3", "wav": "wav", "m4a": "m4a", "opus": "opus", "flac": "flac", "mp4": "mp4",
}


def _convert_paths(tmp_path: Path, profile: str) -> tuple[Path, Path]:
    source = tmp_path / "source.webm"
    source.write_text("source-media")
    return source, tmp_path / f"out.{EXPECTED_EXTENSIONS[profile]}"


def _run_argv_capture(
    tmp_path: Path, profile: ConversionProfile, *, copy_audio: bool = False,
    copy_video: bool = False,
) -> tuple[list[str], str, Path, Path]:
    argv_file = tmp_path / "argv.txt"
    binary = _write_script(tmp_path, "ffmpeg", ARGV_SCRIPT.format(argv=argv_file))
    processor = FFmpegProcessor(binary=binary, timeout_seconds=10)
    source, output = _convert_paths(tmp_path, profile)
    asyncio.run(
        processor.convert(
            source, output, profile, copy_audio=copy_audio, copy_video=copy_video
        )
    )
    return argv_file.read_text().splitlines(), binary, source, output


def test_conversion_profiles_match_domain_minus_original() -> None:
    domain = (AUDIO_OUTPUT_PROFILES | VIDEO_OUTPUT_PROFILES) - {ORIGINAL_OUTPUT_PROFILE}
    assert set(get_args(ConversionProfile)) == domain
    assert set(ffmpeg_module._PROFILES) == domain


@pytest.mark.parametrize("profile", sorted(EXPECTED_EXTENSIONS))
def test_output_extension(profile: ConversionProfile) -> None:
    assert output_extension(profile) == EXPECTED_EXTENSIONS[profile]


@pytest.mark.parametrize("profile", sorted(EXPECTED_PROFILE_ARGS))
def test_convert_exact_argv(tmp_path: Path, profile: ConversionProfile) -> None:
    argv, _, source, output = _run_argv_capture(tmp_path, profile)

    assert argv == [*_HEAD, str(source), *EXPECTED_PROFILE_ARGS[profile], str(output)]
    assert output.read_text() == "converted"
    assert source.read_text() == "source-media"


@pytest.mark.parametrize("profile", sorted(EXPECTED_COPY_AUDIO_ARGS))
def test_convert_copy_audio_argv(tmp_path: Path, profile: ConversionProfile) -> None:
    argv, _, source, output = _run_argv_capture(tmp_path, profile, copy_audio=True)

    assert argv == [*_HEAD, str(source), *EXPECTED_COPY_AUDIO_ARGS[profile], str(output)]


def test_convert_mp4_copy_video_argv(tmp_path: Path) -> None:
    argv, _, source, output = _run_argv_capture(tmp_path, "mp4", copy_video=True)

    assert argv == [
        *_HEAD, str(source), *_VIDEO_MAP, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-f", "mp4", str(output),
    ]
    assert "-vf" not in argv


def test_convert_mp4_copy_both_argv(tmp_path: Path) -> None:
    argv, _, source, output = _run_argv_capture(
        tmp_path, "mp4", copy_audio=True, copy_video=True
    )

    assert argv == [
        *_HEAD, str(source), *_VIDEO_MAP, "-c:v", "copy", "-c:a", "copy",
        "-movflags", "+faststart", "-f", "mp4", str(output),
    ]


def test_merge_argv_unchanged(tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.txt"
    binary = _write_script(tmp_path, "ffmpeg", ARGV_SCRIPT.format(argv=argv_file))
    processor = FFmpegProcessor(binary=binary, timeout_seconds=10)
    video, audio, output = _merge_inputs(tmp_path)

    asyncio.run(processor.merge_streams(video, audio, output))

    assert argv_file.read_text().splitlines() == [
        "-y", "-i", str(video), "-i", str(audio), "-c", "copy", str(output),
    ]


@pytest.mark.parametrize(
    ("profile", "output_name"),
    [("mp3", "out.m4a"), ("m4a", "out.mp4"), ("mp4", "out.mkv"), ("opus", "out"),
     ("flac", "out.FLAC")],
)
def test_convert_rejects_suffix_mismatch(
    tmp_path: Path, profile: ConversionProfile, output_name: str
) -> None:
    processor = FFmpegProcessor(binary=str(tmp_path / "missing-ffmpeg"))
    source = tmp_path / "source.webm"

    with pytest.raises(ValueError):
        asyncio.run(processor.convert(source, tmp_path / output_name, profile))


def test_convert_rejects_output_equal_to_input(tmp_path: Path) -> None:
    processor = FFmpegProcessor(binary=str(tmp_path / "missing-ffmpeg"))
    (tmp_path / "sub").mkdir()
    source = tmp_path / "track.mp3"
    source.write_text("source-media")

    with pytest.raises(ValueError):
        asyncio.run(processor.convert(source, tmp_path / "sub" / ".." / "track.mp3", "mp3"))
    assert source.read_text() == "source-media"


@pytest.mark.parametrize("profile", ["mp3", "wav", "m4a", "opus", "flac"])
def test_convert_rejects_copy_video_for_audio_profiles(
    tmp_path: Path, profile: ConversionProfile
) -> None:
    processor = FFmpegProcessor(binary=str(tmp_path / "missing-ffmpeg"))
    source, output = _convert_paths(tmp_path, profile)

    with pytest.raises(ValueError):
        asyncio.run(processor.convert(source, output, profile, copy_video=True))


def test_convert_missing_binary(tmp_path: Path) -> None:
    processor = FFmpegProcessor(binary=str(tmp_path / "missing-ffmpeg"))
    source, output = _convert_paths(tmp_path, "mp3")

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(processor.convert(source, output, "mp3"))

    assert exc_info.value.kind is FFmpegFailureKind.BINARY_MISSING
    assert source.read_text() == "source-media"


def test_convert_unstartable_binary_fails(tmp_path: Path) -> None:
    binary = tmp_path / "ffmpeg"
    binary.write_text("not executable")
    binary.chmod(0o644)
    processor = FFmpegProcessor(binary=str(binary))
    source, output = _convert_paths(tmp_path, "mp3")

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(processor.convert(source, output, "mp3"))

    assert exc_info.value.kind is FFmpegFailureKind.FAILED
    assert "could not be started" in (exc_info.value.detail or "")


def test_convert_failure_removes_partial_output_and_keeps_input(tmp_path: Path) -> None:
    binary = _write_script(tmp_path, "ffmpeg", PARTIAL_FAIL_SCRIPT)
    processor = FFmpegProcessor(binary=binary)
    source, output = _convert_paths(tmp_path, "opus")
    seen: list[asyncio.subprocess.Process] = []

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(processor.convert(source, output, "opus", on_process_started=seen.append))

    assert exc_info.value.kind is FFmpegFailureKind.FAILED
    assert "encoder failed" in (exc_info.value.detail or "")
    assert len(seen) == 1
    assert seen[0].returncode == 1
    assert not output.exists()
    assert source.read_text() == "source-media"


def test_convert_failure_stderr_is_tail_only(tmp_path: Path) -> None:
    binary = _write_script(tmp_path, "ffmpeg", LONG_STDERR_SCRIPT)
    processor = FFmpegProcessor(binary=binary)
    source, output = _convert_paths(tmp_path, "wav")

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(processor.convert(source, output, "wav"))

    detail = exc_info.value.detail or ""
    assert len(detail) == ffmpeg_module._STDERR_TAIL_BYTES
    assert detail.endswith("FINAL-MARKER\n")


def test_convert_timeout_removes_partial_output_and_reaps(tmp_path: Path) -> None:
    started = tmp_path / "started"
    binary = _write_script(tmp_path, "ffmpeg", PARTIAL_SLOW_SCRIPT.format(started=started))
    processor = FFmpegProcessor(
        binary=binary, timeout_seconds=10, conversion_timeout_seconds=0.5
    )
    source, output = _convert_paths(tmp_path, "mp4")
    seen: list[asyncio.subprocess.Process] = []

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(processor.convert(source, output, "mp4", on_process_started=seen.append))

    assert exc_info.value.kind is FFmpegFailureKind.TIMEOUT
    assert "0.5s" in (exc_info.value.detail or "")
    assert started.exists()
    assert len(seen) == 1
    assert seen[0].returncode is not None
    assert not output.exists()
    assert source.read_text() == "source-media"


def test_conversion_timeout_is_separate_from_merge_timeout(tmp_path: Path) -> None:
    """Convert ignores the short merge timeout; merge still honours it."""
    binary = _write_script(tmp_path, "ffmpeg", DELAYED_OK_SCRIPT)
    processor = FFmpegProcessor(
        binary=binary, timeout_seconds=0.3, conversion_timeout_seconds=10
    )
    source, output = _convert_paths(tmp_path, "flac")

    asyncio.run(processor.convert(source, output, "flac"))
    assert output.read_text() == "done"

    video, audio, merged = _merge_inputs(tmp_path)
    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(processor.merge_streams(video, audio, merged))
    assert exc_info.value.kind is FFmpegFailureKind.TIMEOUT


def test_default_timeouts() -> None:
    processor = FFmpegProcessor()
    assert processor._timeout_seconds == 600.0
    assert processor._conversion_timeout_seconds == 3600.0


def test_convert_cancellation_removes_partial_output_and_reaps(tmp_path: Path) -> None:
    started = tmp_path / "started"
    binary = _write_script(tmp_path, "ffmpeg", PARTIAL_SLOW_SCRIPT.format(started=started))
    processor = FFmpegProcessor(binary=binary)
    source, output = _convert_paths(tmp_path, "m4a")
    seen: list[asyncio.subprocess.Process] = []

    async def run() -> None:
        task = asyncio.create_task(
            processor.convert(source, output, "m4a", on_process_started=seen.append)
        )
        await _wait_for_started(started)
        assert output.read_text() == "partial"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert len(seen) == 1
    assert seen[0].returncode is not None
    assert not output.exists()
    assert source.read_text() == "source-media"


def test_convert_cancellation_kills_process_ignoring_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ffmpeg_module, "_TERMINATION_GRACE_SECONDS", 0.2)
    started = tmp_path / "started"
    binary = _write_script(
        tmp_path, "ffmpeg", PARTIAL_IGNORE_TERM_SCRIPT.format(started=started)
    )
    processor = FFmpegProcessor(binary=binary)
    source, output = _convert_paths(tmp_path, "mp3")
    seen: list[asyncio.subprocess.Process] = []

    async def run() -> None:
        task = asyncio.create_task(
            processor.convert(source, output, "mp3", on_process_started=seen.append)
        )
        await _wait_for_started(started)
        assert output.read_text() == "partial"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert len(seen) == 1
    assert seen[0].returncode == -9
    assert not output.exists()
    assert source.read_text() == "source-media"


def test_cleanup_oserror_does_not_mask_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _write_script(tmp_path, "ffmpeg", PARTIAL_FAIL_SCRIPT)
    processor = FFmpegProcessor(binary=binary)
    source, output = _convert_paths(tmp_path, "mp3")
    original_unlink = Path.unlink

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == output:
            raise PermissionError("denied")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(processor.convert(source, output, "mp3"))

    assert exc_info.value.kind is FFmpegFailureKind.FAILED
    assert source.read_text() == "source-media"


def test_merge_failure_removes_partial_output_and_keeps_inputs(tmp_path: Path) -> None:
    binary = _write_script(tmp_path, "ffmpeg", PARTIAL_FAIL_SCRIPT)
    processor = FFmpegProcessor(binary=binary, timeout_seconds=10)
    video, audio, output = _merge_inputs(tmp_path)

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(processor.merge_streams(video, audio, output))

    assert exc_info.value.kind is FFmpegFailureKind.FAILED
    assert not output.exists()
    assert video.read_text() == "v"
    assert audio.read_text() == "a"


def test_merge_timeout_removes_partial_output(tmp_path: Path) -> None:
    started = tmp_path / "started"
    binary = _write_script(tmp_path, "ffmpeg", PARTIAL_SLOW_SCRIPT.format(started=started))
    processor = FFmpegProcessor(binary=binary, timeout_seconds=0.5)
    video, audio, output = _merge_inputs(tmp_path)
    seen: list[asyncio.subprocess.Process] = []

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(
            processor.merge_streams(video, audio, output, on_process_started=seen.append)
        )

    assert exc_info.value.kind is FFmpegFailureKind.TIMEOUT
    assert started.exists()
    assert seen[0].returncode is not None
    assert not output.exists()
    assert video.read_text() == "v"
    assert audio.read_text() == "a"


def test_merge_cancellation_removes_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ffmpeg_module, "_TERMINATION_GRACE_SECONDS", 0.2)
    started = tmp_path / "started"
    binary = _write_script(
        tmp_path, "ffmpeg", PARTIAL_IGNORE_TERM_SCRIPT.format(started=started)
    )
    processor = FFmpegProcessor(binary=binary, timeout_seconds=10)
    video, audio, output = _merge_inputs(tmp_path)
    seen: list[asyncio.subprocess.Process] = []

    async def run() -> None:
        task = asyncio.create_task(
            processor.merge_streams(video, audio, output, on_process_started=seen.append)
        )
        await _wait_for_started(started)
        assert output.read_text() == "partial"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert seen[0].returncode == -9
    assert not output.exists()
    assert video.read_text() == "v"
    assert audio.read_text() == "a"


@pytest.mark.parametrize(
    ("script", "written"), [(NO_OUTPUT_OK_SCRIPT, False), (EMPTY_OUTPUT_OK_SCRIPT, True)]
)
def test_convert_without_output_fails_and_removes_file(
    tmp_path: Path, script: str, written: bool
) -> None:
    binary = _write_script(tmp_path, "ffmpeg", script)
    processor = FFmpegProcessor(binary=binary)
    source, output = _convert_paths(tmp_path, "flac")

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(processor.convert(source, output, "flac"))

    assert exc_info.value.kind is FFmpegFailureKind.FAILED
    assert exc_info.value.detail == "ffmpeg produced no output"
    assert not output.exists()
    assert source.read_text() == "source-media"


def test_merge_does_not_require_output(tmp_path: Path) -> None:
    binary = _write_script(tmp_path, "ffmpeg", NO_OUTPUT_OK_SCRIPT)
    processor = FFmpegProcessor(binary=binary, timeout_seconds=10)
    video, audio, output = _merge_inputs(tmp_path)

    asyncio.run(processor.merge_streams(video, audio, output))

    assert not output.exists()


def test_second_cancel_during_grace_kills_child_and_removes_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real second task.cancel() while the first cancel waits out the grace period."""
    monkeypatch.setattr(ffmpeg_module, "_TERMINATION_GRACE_SECONDS", 10.0)
    started = tmp_path / "started"
    binary = _write_script(
        tmp_path, "ffmpeg", PARTIAL_IGNORE_TERM_SCRIPT.format(started=started)
    )
    processor = FFmpegProcessor(binary=binary)
    source, output = _convert_paths(tmp_path, "mp3")
    seen: list[asyncio.subprocess.Process] = []

    async def run() -> None:
        task = asyncio.create_task(
            processor.convert(source, output, "mp3", on_process_started=seen.append)
        )
        await _wait_for_started(started)
        task.cancel()
        await asyncio.sleep(0.3)
        # The child ignores SIGTERM, so the task is still inside the grace wait.
        assert not task.done()
        assert seen[0].returncode is None
        began = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - began < 5.0
        await asyncio.wait_for(seen[0].wait(), timeout=5.0)

    asyncio.run(run())

    assert seen[0].returncode == -9
    assert not output.exists()
    assert source.read_text() == "source-media"


def test_interrupted_termination_on_timeout_kills_child_and_removes_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic variant: termination itself raises CancelledError on timeout."""

    async def cancelled_termination(
        process: asyncio.subprocess.Process, grace_seconds: float
    ) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(ffmpeg_module, "ensure_process_terminated", cancelled_termination)
    started = tmp_path / "started"
    binary = _write_script(tmp_path, "ffmpeg", PARTIAL_SLOW_SCRIPT.format(started=started))
    processor = FFmpegProcessor(binary=binary, conversion_timeout_seconds=0.5)
    source, output = _convert_paths(tmp_path, "wav")
    seen: list[asyncio.subprocess.Process] = []

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await processor.convert(source, output, "wav", on_process_started=seen.append)
        await asyncio.wait_for(seen[0].wait(), timeout=5.0)

    asyncio.run(run())

    assert started.exists()
    assert seen[0].returncode == -9
    assert not output.exists()
    assert source.read_text() == "source-media"
