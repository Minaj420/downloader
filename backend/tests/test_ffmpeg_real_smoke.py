"""Real FFmpeg/ffprobe smoke tests for the conversion adapter (M6.4f-f1).

Every medium is synthesised locally under ``tmp_path``: PCM audio is written
with the standard library and video frames are raw rgb24 bytes, so no lavfi
source filter or network access is needed. All helper processes run as argv
lists (``shell=False``) with a timeout. Tests skip when ``ffmpeg``/``ffprobe``
are not on PATH, or when the build lacks an encoder/filter the case depends
on; any other failure, including unreadable probe output, fails the test.
"""

import array
import asyncio
import contextlib
import errno
import functools
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from app.domain.analysis import MediaFormatKind, NormalizedFormat
from app.integrations import ffmpeg as ffmpeg_module
from app.integrations.ffmpeg import (
    ConversionProfile,
    FFmpegError,
    FFmpegFailureKind,
    FFmpegProcessor,
)
from app.services.analysis_normalizer import normalize_source_formats
from app.services.conversion_plan import plan_conversion

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    _FFMPEG is None or _FFPROBE is None,
    reason="real FFmpeg smoke needs both ffmpeg and ffprobe on PATH",
)

_TOOL_TIMEOUT_SECONDS = 60.0
_CONVERSION_TIMEOUT_SECONDS = 120.0
_SAMPLE_RATE = 48_000
_CHANNELS = 2
_SAMPLE_WIDTH = 2
_ODD_SIZE = (321, 241)
_EVEN_SIZE = (320, 240)
_FRAME_RATE = 10
_FRAMES = 10


# --- tool helpers -----------------------------------------------------------


def _ffmpeg() -> str:
    assert _FFMPEG is not None
    return _FFMPEG


def _ffprobe() -> str:
    assert _FFPROBE is not None
    return _FFPROBE


def _run_tool(
    argv: list[str], timeout: float = _TOOL_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[bytes]:
    # subprocess.run kills and reaps the child itself on TimeoutExpired.
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        tail = result.stderr[-2_000:].decode("utf-8", errors="replace")
        raise AssertionError(f"{Path(argv[0]).name} exited {result.returncode}: {tail}")
    return result


@functools.cache
def _encoders() -> frozenset[str]:
    lines = _run_tool([_ffmpeg(), "-hide_banner", "-encoders"]).stdout.decode().splitlines()
    names: set[str] = set()
    in_table = False
    for line in lines:
        if line.strip().startswith("------"):
            in_table = True
            continue
        parts = line.split()
        if in_table and len(parts) >= 2:
            names.add(parts[1])
    if not names:
        raise AssertionError("ffmpeg -encoders produced no parsable encoder table")
    return frozenset(names)


@functools.cache
def _filters() -> frozenset[str]:
    lines = _run_tool([_ffmpeg(), "-hide_banner", "-filters"]).stdout.decode().splitlines()
    names = {
        parts[1] for parts in (line.split() for line in lines)
        if len(parts) >= 3 and "->" in parts[2]
    }
    if not names:
        raise AssertionError("ffmpeg -filters produced no parsable filter table")
    return frozenset(names)


def _require(encoders: tuple[str, ...] = (), filters: tuple[str, ...] = ()) -> None:
    missing_encoders = [name for name in encoders if name not in _encoders()]
    missing_filters = [name for name in filters if name not in _filters()]
    if missing_encoders:
        pytest.skip(f"ffmpeg build lacks encoder(s): {', '.join(missing_encoders)}")
    if missing_filters:
        pytest.skip(f"ffmpeg build lacks filter(s): {', '.join(missing_filters)}")


def _probe(path: Path) -> dict[str, Any]:
    result = _run_tool([
        _ffprobe(), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
    ])
    data = json.loads(result.stdout)
    assert isinstance(data, dict), "ffprobe returned non-object JSON"
    return data


def _stream_types(info: dict[str, Any]) -> list[tuple[str, str]]:
    return [(stream["codec_type"], stream["codec_name"]) for stream in info["streams"]]


def _format_names(info: dict[str, Any]) -> list[str]:
    return str(info["format"]["format_name"]).split(",")


def _packet_hash(path: Path, selector: str) -> str:
    """SHA-256 over the raw packet payloads of one stream (stream copy, no decode)."""
    result = _run_tool([
        _ffmpeg(), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
        "-map", selector, "-c", "copy", "-f", "hash", "-hash", "sha256", "-",
    ])
    digest = result.stdout.decode().strip()
    assert digest.startswith("SHA256="), f"unexpected hash muxer output {digest!r}"
    return digest


def _file_digest(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


# --- synthetic sources ------------------------------------------------------


def _pcm_tone(seconds: float, frequency: float = 440.0) -> bytes:
    frames = int(_SAMPLE_RATE * seconds)
    samples = array.array("h")
    for index in range(frames):
        value = int(12_000 * math.sin(2 * math.pi * frequency * index / _SAMPLE_RATE))
        samples.extend((value,) * _CHANNELS)
    return samples.tobytes()


def _write_tone_wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(_CHANNELS)
        handle.setsampwidth(_SAMPLE_WIDTH)
        handle.setframerate(_SAMPLE_RATE)
        handle.writeframes(_pcm_tone(seconds))
    return path


def _write_raw_frames(path: Path, size: tuple[int, int]) -> Path:
    frame_bytes = size[0] * size[1] * 3
    pattern = bytes(range(256)) * (frame_bytes // 256 + 2)
    with path.open("wb") as handle:
        for index in range(_FRAMES):
            offset = (index * 7) % 256
            handle.write(pattern[offset:offset + frame_bytes])
    return path


def _raw_video_input(frames: Path, size: tuple[int, int]) -> list[str]:
    return [
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", f"{size[0]}x{size[1]}",
        "-framerate", str(_FRAME_RATE), "-i", str(frames),
    ]


def _encode(arguments: list[str], output: Path) -> Path:
    _run_tool([_ffmpeg(), "-hide_banner", "-nostdin", "-v", "error", "-y", *arguments,
               str(output)])
    return output


def _odd_video_source(tmp_path: Path) -> Path:
    """Lossless RGB (ffv1/bgr0) + PCM MKV whose 321x241 size YUV cannot round."""
    frames = _write_raw_frames(tmp_path / "frames_odd.rgb", _ODD_SIZE)
    tone = _write_tone_wav(tmp_path / "video_tone.wav")
    source = _encode(
        [*_raw_video_input(frames, _ODD_SIZE), "-i", str(tone),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "ffv1", "-pix_fmt", "bgr0",
         "-c:a", "pcm_s16le", "-shortest", "-f", "matroska"],
        tmp_path / "source_odd.mkv",
    )
    video = [s for s in _probe(source)["streams"] if s["codec_type"] == "video"]
    assert [(v["width"], v["height"]) for v in video] == [_ODD_SIZE]
    return source


# --- top-level ISO BMFF box parser ------------------------------------------


def _top_level_boxes(data: bytes) -> list[str]:
    boxes: list[str] = []
    offset = 0
    end = len(data)
    while offset < end:
        if end - offset < 8:
            raise AssertionError(f"truncated box header at offset {offset}")
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8].decode("latin-1")
        header = 8
        if size == 1:
            if end - offset < 16:
                raise AssertionError(f"truncated extended size for {kind!r} at {offset}")
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or size > end - offset:
            raise AssertionError(f"box {kind!r} at {offset} has out-of-bounds size {size}")
        boxes.append(kind)
        offset += size
    return boxes


def _assert_faststart(path: Path) -> None:
    boxes = _top_level_boxes(path.read_bytes())
    assert "moov" in boxes and "mdat" in boxes, boxes
    assert boxes.index("moov") < boxes.index("mdat"), boxes


def _box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def test_box_parser_handles_extended_and_open_ended_sizes() -> None:
    extended = (1).to_bytes(4, "big") + b"moov" + (20).to_bytes(8, "big") + b"abcd"
    open_ended = (0).to_bytes(4, "big") + b"mdat" + b"payload-to-eof"
    data = _box(b"ftyp", b"M4A ") + extended + open_ended

    assert _top_level_boxes(data) == ["ftyp", "moov", "mdat"]


@pytest.mark.parametrize(
    "data",
    [
        _box(b"ftyp", b"M4A ") + b"\x00\x00\x00",  # truncated header
        (64).to_bytes(4, "big") + b"mdat" + b"short",  # size past EOF
        (4).to_bytes(4, "big") + b"free",  # size smaller than header
        (1).to_bytes(4, "big") + b"moov" + (8).to_bytes(8, "big"),  # extended < header
        (1).to_bytes(4, "big") + b"moov" + b"\x00\x00",  # truncated extended size
        (1).to_bytes(4, "big") + b"moov" + (2**63).to_bytes(8, "big"),  # huge size
    ],
    ids=["truncated-header", "past-eof", "below-header", "extended-below-header",
         "truncated-extended", "huge-extended"],
)
def test_box_parser_rejects_out_of_bounds_sizes(data: bytes) -> None:
    with pytest.raises(AssertionError):
        _top_level_boxes(data)


# --- six profiles -----------------------------------------------------------


def _processor() -> FFmpegProcessor:
    return FFmpegProcessor(
        binary=_ffmpeg(), conversion_timeout_seconds=_CONVERSION_TIMEOUT_SECONDS
    )


class _ProfileCase(NamedTuple):
    profile: ConversionProfile
    encoders: tuple[str, ...]
    filters: tuple[str, ...]
    format_name: str
    streams: list[tuple[str, str]]


_PROFILE_CASES = [
    _ProfileCase("mp3", ("libmp3lame",), (), "mp3", [("audio", "mp3")]),
    _ProfileCase("wav", ("pcm_s16le",), (), "wav", [("audio", "pcm_s16le")]),
    _ProfileCase("m4a", ("aac",), (), "mp4", [("audio", "aac")]),
    _ProfileCase("opus", ("libopus",), (), "ogg", [("audio", "opus")]),
    _ProfileCase("flac", ("flac",), (), "flac", [("audio", "flac")]),
    _ProfileCase(
        "mp4", ("libx264", "aac"), ("scale",), "mp4", [("video", "h264"), ("audio", "aac")]
    ),
]


@requires_ffmpeg
@pytest.mark.parametrize("case", _PROFILE_CASES, ids=[c.profile for c in _PROFILE_CASES])
def test_profile_produces_expected_real_output(tmp_path: Path, case: _ProfileCase) -> None:
    _require(case.encoders, case.filters)
    if case.profile == "mp4":
        source = _odd_video_source(tmp_path)
    else:
        source = _write_tone_wav(tmp_path / "source.wav")
    before = _file_digest(source)
    output = tmp_path / f"out.{ffmpeg_module.output_extension(case.profile)}"

    asyncio.run(_processor().convert(source, output, case.profile))

    info = _probe(output)
    assert _stream_types(info) == case.streams
    assert case.format_name in _format_names(info)
    streams = info["streams"]
    if case.profile == "m4a":
        assert info["format"]["tags"]["major_brand"].strip() == "M4A"
        _assert_faststart(output)
    elif case.profile == "flac":
        assert streams[0]["sample_fmt"] == "s16"
    elif case.profile == "mp4":
        video = streams[0]
        assert video["profile"] == "High"
        assert video["pix_fmt"] == "yuv420p"
        assert (video["width"], video["height"]) == _EVEN_SIZE
        _assert_faststart(output)
    assert _file_digest(source) == before


# --- planner-driven stream copy ---------------------------------------------


class _CopyCase(NamedTuple):
    name: str
    profile: ConversionProfile
    encoders: tuple[str, ...]
    source_name: str
    source_codecs: list[str]
    selected: NormalizedFormat
    format_name: str
    streams: list[tuple[str, str]]


def _audio_format(codec: str) -> NormalizedFormat:
    return NormalizedFormat(
        variant_id="a", format_id="a", kind=MediaFormatKind.AUDIO_ONLY, audio_codec=codec
    )


_COPY_CASES = [
    _CopyCase("aac-to-m4a", "m4a", ("aac",), "source_aac.m4a",
              ["-c:a", "aac", "-b:a", "128k", "-f", "mp4"],
              _audio_format("aac"), "mp4", [("audio", "aac")]),
    _CopyCase("opus-webm-to-opus", "opus", ("libopus",), "source.webm",
              ["-c:a", "libopus", "-b:a", "96k", "-f", "webm"],
              _audio_format("opus"), "ogg", [("audio", "opus")]),
    _CopyCase("mp3-to-mp3", "mp3", ("libmp3lame",), "source_mp3.mp3",
              ["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"],
              _audio_format("mp3"), "mp3", [("audio", "mp3")]),
    _CopyCase("h264-aac-mkv-to-mp4", "mp4", ("libx264", "aac"), "source_h264.mkv",
              ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k", "-shortest", "-f", "matroska"],
              NormalizedFormat(variant_id="v", format_id="v", kind=MediaFormatKind.MUXED,
                               video_codec="h264", audio_codec="aac"),
              "mp4", [("video", "h264"), ("audio", "aac")]),
]


def _copy_source(tmp_path: Path, case: _CopyCase) -> Path:
    tone = _write_tone_wav(tmp_path / "copy_tone.wav")
    if case.profile == "mp4":
        frames = _write_raw_frames(tmp_path / "frames_even.rgb", _EVEN_SIZE)
        inputs = [*_raw_video_input(frames, _EVEN_SIZE), "-i", str(tone),
                  "-map", "0:v:0", "-map", "1:a:0"]
    else:
        inputs = ["-i", str(tone)]
    return _encode([*inputs, *case.source_codecs], tmp_path / case.source_name)


@requires_ffmpeg
@pytest.mark.parametrize("case", _COPY_CASES, ids=[c.name for c in _COPY_CASES])
def test_planned_stream_copy_preserves_packets(tmp_path: Path, case: _CopyCase) -> None:
    _require(case.encoders)
    source = _copy_source(tmp_path, case)
    plan = plan_conversion(case.profile, case.selected, None)
    assert plan.copy_audio is True
    assert plan.copy_video is (case.profile == "mp4")
    before = _file_digest(source)
    output = tmp_path / f"out.{ffmpeg_module.output_extension(case.profile)}"

    asyncio.run(_processor().convert(
        source, output, case.profile,
        copy_audio=plan.copy_audio, copy_video=plan.copy_video,
    ))

    info = _probe(output)
    assert _stream_types(info) == case.streams
    assert case.format_name in _format_names(info)
    selectors = ["0:a:0"] if case.profile != "mp4" else ["0:v:0", "0:a:0"]
    for selector in selectors:
        assert _packet_hash(output, selector) == _packet_hash(source, selector), selector
    if case.profile == "m4a":
        assert info["format"]["tags"]["major_brand"].strip() == "M4A"
    if case.profile in ("m4a", "mp4"):
        _assert_faststart(output)
    assert _file_digest(source) == before


# --- normalized mp4a.69 (MP3 in MP4) to MP4 ---------------------------------


def _streams_of(info: dict[str, Any], codec_type: str) -> list[dict[str, Any]]:
    return [s for s in info["streams"] if s["codec_type"] == codec_type]


@requires_ffmpeg
def test_normalized_mp3_in_mp4a_source_is_transcoded_to_aac(tmp_path: Path) -> None:
    _require(("libx264", "libmp3lame", "aac"))
    frames = _write_raw_frames(tmp_path / "frames_even.rgb", _EVEN_SIZE)
    tone = _write_tone_wav(tmp_path / "mp3_tone.wav")
    source = _encode(
        [*_raw_video_input(frames, _EVEN_SIZE), "-i", str(tone),
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "libmp3lame", "-b:a", "128k", "-shortest", "-f", "mp4"],
        tmp_path / "source_h264_mp3.mp4",
    )
    assert _stream_types(_probe(source)) == [("video", "h264"), ("audio", "mp3")]

    [selected] = normalize_source_formats([
        {"format_id": "m", "ext": "mp4", "height": _EVEN_SIZE[1],
         "vcodec": "avc1.640028", "acodec": "mp4a.69"},
    ])
    assert (selected.video_codec, selected.audio_codec) == ("h264", "mp3")
    plan = plan_conversion("mp4", selected, None)
    assert (plan.copy_video, plan.copy_audio) == (True, False)
    before = _file_digest(source)
    output = tmp_path / "out.mp4"

    asyncio.run(_processor().convert(
        source, output, "mp4", copy_audio=plan.copy_audio, copy_video=plan.copy_video,
    ))

    info = _probe(output)
    assert _stream_types(info) == [("video", "h264"), ("audio", "aac")]
    assert "mp4" in _format_names(info)
    assert len(_streams_of(info, "audio")) == 1
    assert _packet_hash(output, "0:v:0") == _packet_hash(source, "0:v:0")
    _assert_faststart(output)
    assert _file_digest(source) == before


# --- denylisted H.264 profile (High 4:4:4) to MP4 ---------------------------


@requires_ffmpeg
def test_high444_h264_source_video_is_transcoded_audio_copied(tmp_path: Path) -> None:
    # Source and target both use libx264 + aac; the MP4 transcode adds scale.
    _require(("libx264", "aac"), ("scale",))
    frames = _write_raw_frames(tmp_path / "frames_even.rgb", _EVEN_SIZE)
    tone = _write_tone_wav(tmp_path / "high444_tone.wav")
    source = _encode(
        [*_raw_video_input(frames, _EVEN_SIZE), "-i", str(tone),
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv444p",
         "-c:a", "aac", "-b:a", "128k", "-shortest", "-f", "mp4"],
        tmp_path / "source_high444.mp4",
    )
    source_info = _probe(source)
    assert _stream_types(source_info) == [("video", "h264"), ("audio", "aac")]
    [source_video] = _streams_of(source_info, "video")
    assert source_video["profile"] == "High 4:4:4 Predictive"
    assert source_video["pix_fmt"] == "yuv444p"

    [selected] = normalize_source_formats([
        {"format_id": "m", "ext": "mp4", "height": _EVEN_SIZE[1],
         "vcodec": "avc1.f4001e", "acodec": "mp4a.40.2"},
    ])
    assert (selected.video_codec, selected.h264_profile_idc) == ("h264", 244)
    plan = plan_conversion("mp4", selected, None)
    assert (plan.copy_video, plan.copy_audio, plan.keep_source_bitrate) == (False, True, False)
    before = _file_digest(source)
    output = tmp_path / "out.mp4"

    asyncio.run(_processor().convert(
        source, output, "mp4", copy_audio=plan.copy_audio, copy_video=plan.copy_video,
    ))

    info = _probe(output)
    assert _stream_types(info) == [("video", "h264"), ("audio", "aac")]
    assert "mp4" in _format_names(info)
    [video] = _streams_of(info, "video")
    assert video["profile"] == "High"
    assert video["pix_fmt"] == "yuv420p"
    assert _packet_hash(output, "0:a:0") == _packet_hash(source, "0:a:0")
    assert _packet_hash(output, "0:v:0") != _packet_hash(source, "0:v:0")
    _assert_faststart(output)
    assert _file_digest(source) == before


# --- missing encoder --------------------------------------------------------

_MISSING_ENCODER = "nonexistent_encoder_m64f"


@requires_ffmpeg
def test_missing_encoder_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _MISSING_ENCODER not in _encoders()
    profile = ffmpeg_module._PROFILES["mp3"]
    monkeypatch.setitem(
        ffmpeg_module._PROFILES, "mp3",
        profile._replace(audio_codec=("-c:a", _MISSING_ENCODER)),
    )
    source = _write_tone_wav(tmp_path / "source.wav")
    before = _file_digest(source)
    output = tmp_path / "out.mp3"
    seen: list[asyncio.subprocess.Process] = []

    with pytest.raises(FFmpegError) as exc_info:
        asyncio.run(_processor().convert(source, output, "mp3", on_process_started=seen.append))

    assert exc_info.value.kind is FFmpegFailureKind.FAILED
    assert _MISSING_ENCODER in (exc_info.value.detail or "")
    assert len(seen) == 1
    assert seen[0].returncode not in (None, 0)
    assert not output.exists()
    assert _file_digest(source) == before


# --- real cancellation ------------------------------------------------------

_FIFO_OPEN_TIMEOUT_SECONDS = 20.0
_PROGRESS_TIMEOUT_SECONDS = 20.0
_CANCEL_RETURN_TIMEOUT_SECONDS = 15.0
_WRITER_JOIN_TIMEOUT_SECONDS = 5.0
_MIN_PROGRESS_BYTES = 64 * 1024
_WRITER_PACING_SECONDS = 0.002


def _streaming_wav_header() -> bytes:
    block_align = _CHANNELS * _SAMPLE_WIDTH
    fmt = struct.pack(
        "<HHIIHH", 1, _CHANNELS, _SAMPLE_RATE, _SAMPLE_RATE * block_align, block_align,
        _SAMPLE_WIDTH * 8,
    )
    unbounded = struct.pack("<I", 0xFFFFFFFF)
    return (b"RIFF" + unbounded + b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + unbounded)


class _FifoWavWriter(threading.Thread):
    """Feeds an endless WAV stream into a FIFO without ever blocking.

    The FIFO is opened non-blocking (retrying ENXIO until FFmpeg opens the
    read end) and written non-blocking, so ``stop`` is honoured promptly and
    a vanished reader surfaces as EPIPE instead of a hang.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(name="fifo-wav-writer", daemon=True)
        self._path = path
        self._stop_requested = threading.Event()
        self.opened = threading.Event()
        self.bytes_written = 0
        self.reader_closed = False
        self.errors: list[BaseException] = []

    def stop(self) -> None:
        self._stop_requested.set()

    def run(self) -> None:
        fd = -1
        try:
            deadline = time.monotonic() + _FIFO_OPEN_TIMEOUT_SECONDS
            while fd < 0:
                if self._stop_requested.is_set():
                    return
                try:
                    fd = os.open(self._path, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as exc:
                    if exc.errno != errno.ENXIO:
                        raise
                    if time.monotonic() > deadline:
                        raise TimeoutError("no reader opened the FIFO") from None
                    time.sleep(0.01)
            self.opened.set()
            chunk = _pcm_tone(0.1)
            pending = memoryview(_streaming_wav_header())
            while not self._stop_requested.is_set():
                if len(pending) == 0:
                    pending = memoryview(chunk)
                    time.sleep(_WRITER_PACING_SECONDS)
                try:
                    written = os.write(fd, pending)
                except BlockingIOError:
                    time.sleep(_WRITER_PACING_SECONDS)
                    continue
                except BrokenPipeError:
                    self.reader_closed = True
                    return
                self.bytes_written += written
                pending = pending[written:]
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            if fd >= 0:
                os.close(fd)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


async def _wait_until(condition: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


@requires_ffmpeg
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="real cancellation smoke needs POSIX FIFOs")
def test_real_cancellation_terminates_reaps_and_discards_output(tmp_path: Path) -> None:
    _require(("pcm_s16le",))
    fifo = tmp_path / "live.wav"
    os.mkfifo(fifo)
    output = tmp_path / "out.wav"
    processor = _processor()
    seen: list[asyncio.subprocess.Process] = []
    writer = _FifoWavWriter(fifo)

    async def run() -> None:
        task = asyncio.create_task(
            processor.convert(fifo, output, "wav", on_process_started=seen.append)
        )
        try:
            await _wait_until(
                lambda: writer.bytes_written > _MIN_PROGRESS_BYTES
                and _file_size(output) > _MIN_PROGRESS_BYTES,
                _PROGRESS_TIMEOUT_SECONDS,
                "ffmpeg to consume the FIFO stream and grow its output",
            )
            assert not task.done()
            assert len(seen) == 1
            assert seen[0].returncode is None
            task.cancel()
            await asyncio.wait({task}, timeout=_CANCEL_RETURN_TIMEOUT_SECONDS)
            assert task.done(), "adapter did not return after cancellation"
            # Captured before any cleanup wait so a missing reap cannot be masked.
            returncode_at_return = seen[0].returncode
            assert returncode_at_return is not None
            assert task.cancelled()
            with pytest.raises(ProcessLookupError):
                os.kill(seen[0].pid, 0)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=_CANCEL_RETURN_TIMEOUT_SECONDS)
            for process in seen:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await asyncio.wait_for(process.wait(), timeout=_TOOL_TIMEOUT_SECONDS)

    writer.start()
    try:
        asyncio.run(run())
    finally:
        writer.stop()
        writer.join(timeout=_WRITER_JOIN_TIMEOUT_SECONDS)

    assert not writer.is_alive()
    assert writer.errors == []
    assert writer.opened.is_set()
    assert not output.exists()
    assert fifo.exists()
