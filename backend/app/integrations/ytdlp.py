"""yt-dlp integration adapter.

yt-dlp runs as an isolated subprocess via ``asyncio.create_subprocess_exec``
with an argument array (never a shell string). The adapter hides all CLI
details — invocation, timeouts, progress parsing, output discovery, and
failure mapping — from the rest of the codebase (ARCHITECTURE.md §11–13).

Failures are raised as neutral ``YtDlpError`` kinds so both the analysis and
the job domains can map them to their own user-facing error codes.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.domain.analysis import COLLECTION_ANALYSIS_WINDOW_LIMIT, AnalysisError
from app.utils.urls import validate_media_url

_PROGRESS_TEMPLATE = (
    "PROG %(progress.downloaded_bytes)s %(progress.total_bytes)s "
    "%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s"
)
_STDERR_CAP_BYTES = 64_000


class YtDlpFailureKind(StrEnum):
    BINARY_MISSING = "binary_missing"
    UNSUPPORTED_URL = "unsupported_url"
    AUTHENTICATION_REQUIRED = "authentication_required"
    GEO_RESTRICTED = "geo_restricted"
    MEDIA_UNAVAILABLE = "media_unavailable"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    MALFORMED_OUTPUT = "malformed_output"
    EXTRACTOR_FAILED = "extractor_failed"


class YtDlpError(Exception):
    def __init__(self, kind: YtDlpFailureKind, detail: str | None = None) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind.value}: {detail or ''}")


_KNOWN_FAILURE_SIGNATURES: tuple[tuple[str, YtDlpFailureKind], ...] = (
    ("unsupported url", YtDlpFailureKind.UNSUPPORTED_URL),
    ("sign in to confirm", YtDlpFailureKind.AUTHENTICATION_REQUIRED),
    ("login required", YtDlpFailureKind.AUTHENTICATION_REQUIRED),
    ("log in to", YtDlpFailureKind.AUTHENTICATION_REQUIRED),
    ("private video", YtDlpFailureKind.AUTHENTICATION_REQUIRED),
    ("members-only", YtDlpFailureKind.AUTHENTICATION_REQUIRED),
    ("members only", YtDlpFailureKind.AUTHENTICATION_REQUIRED),
    ("authentication", YtDlpFailureKind.AUTHENTICATION_REQUIRED),
    ("not available in your country", YtDlpFailureKind.GEO_RESTRICTED),
    ("geo-restricted", YtDlpFailureKind.GEO_RESTRICTED),
    ("geo restricted", YtDlpFailureKind.GEO_RESTRICTED),
    ("video unavailable", YtDlpFailureKind.MEDIA_UNAVAILABLE),
    ("has been removed", YtDlpFailureKind.MEDIA_UNAVAILABLE),
    ("removed by the uploader", YtDlpFailureKind.MEDIA_UNAVAILABLE),
    ("does not exist", YtDlpFailureKind.MEDIA_UNAVAILABLE),
    ("not exist", YtDlpFailureKind.MEDIA_UNAVAILABLE),
    ("404", YtDlpFailureKind.MEDIA_UNAVAILABLE),
    ("not found", YtDlpFailureKind.MEDIA_UNAVAILABLE),
    ("unavailable", YtDlpFailureKind.MEDIA_UNAVAILABLE),
    ("rate limit", YtDlpFailureKind.NETWORK_ERROR),
    ("http error 429", YtDlpFailureKind.NETWORK_ERROR),
    ("timed out", YtDlpFailureKind.NETWORK_ERROR),
    ("timeout", YtDlpFailureKind.NETWORK_ERROR),
    ("connection", YtDlpFailureKind.NETWORK_ERROR),
    ("getaddrinfo", YtDlpFailureKind.NETWORK_ERROR),
    ("temporary failure", YtDlpFailureKind.NETWORK_ERROR),
    ("name or service not known", YtDlpFailureKind.NETWORK_ERROR),
    ("network", YtDlpFailureKind.NETWORK_ERROR),
    ("ssl", YtDlpFailureKind.NETWORK_ERROR),
    ("unable to download", YtDlpFailureKind.NETWORK_ERROR),
    ("unable to extract", YtDlpFailureKind.EXTRACTOR_FAILED),
)


def map_failure(stderr: str) -> YtDlpError:
    lowered = stderr.lower()
    for signature, kind in _KNOWN_FAILURE_SIGNATURES:
        if signature in lowered:
            return YtDlpError(kind, detail=stderr)
    return YtDlpError(YtDlpFailureKind.EXTRACTOR_FAILED, detail=stderr)


def _parse_info_json(stdout: bytes) -> dict[str, Any]:
    text = stdout.decode("utf-8", errors="replace").strip()
    start = text.find("{")
    if start < 0:
        raise YtDlpError(
            YtDlpFailureKind.MALFORMED_OUTPUT,
            detail="yt-dlp produced no JSON output",
        )
    try:
        parsed = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise YtDlpError(
            YtDlpFailureKind.MALFORMED_OUTPUT,
            detail=f"yt-dlp output is not valid JSON: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise YtDlpError(
            YtDlpFailureKind.MALFORMED_OUTPUT,
            detail="yt-dlp output is not a JSON object",
        )
    return parsed


async def _spawn(
    command: list[str],
) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise YtDlpError(
            YtDlpFailureKind.BINARY_MISSING,
            detail=f"yt-dlp binary not found: {command[0]}",
        ) from exc
    except OSError as exc:
        raise YtDlpError(
            YtDlpFailureKind.EXTRACTOR_FAILED,
            detail=f"yt-dlp could not be started: {exc}",
        ) from exc


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass  # The process exited between the returncode check and kill.
    await process.wait()


async def _kill_deferred_cancellation(
    process: asyncio.subprocess.Process,
) -> asyncio.CancelledError | None:
    """Own cleanup until reaped; return the first caller cancellation to propagate."""
    cleanup = asyncio.create_task(_kill(process))
    cancellation = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    cleanup.result()  # Retrieve failures; no detached cleanup task survives this call.
    return cancellation


@dataclass(slots=True)
class DownloadProgress:
    downloaded_bytes: int | None
    total_bytes: int | None
    total_bytes_estimate: int | None
    speed_bytes_per_second: int | None
    eta_seconds: int | None


def _parse_int(value: str) -> int | None:
    try:
        number = float(value)
    except ValueError:
        return None
    if number < 0:
        return None
    return int(number)


def parse_progress_line(line: str) -> DownloadProgress | None:
    parts = line.split()
    if len(parts) != 6:
        return None
    raw = [_parse_int(part) for part in parts[1:]]
    if all(value is None for value in raw):
        return None
    return DownloadProgress(
        downloaded_bytes=raw[0],
        total_bytes=raw[1],
        total_bytes_estimate=raw[2],
        speed_bytes_per_second=raw[3],
        eta_seconds=raw[4],
    )


class YtDlpAnalyzer:
    """Runs yt-dlp in metadata-only mode and returns the raw info dict."""

    def __init__(
        self,
        binary: str = "yt-dlp",
        timeout_seconds: float = 90.0,
        playlist_item_limit: int = COLLECTION_ANALYSIS_WINDOW_LIMIT,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._playlist_item_limit = playlist_item_limit

    async def analyze(self, url: str, platform_hint: str | None = None) -> dict[str, Any]:
        del platform_hint  # advisory only; mismatch is validated after detection
        command = [
            self._binary,
            "--ignore-config",
            "--no-warnings",
            "--dump-single-json",
            "--flat-playlist",
            "--playlist-items",
            f"1-{self._playlist_item_limit}",
            url,
        ]
        process = await _spawn(command)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout_seconds
            )
        except TimeoutError:
            await _kill(process)
            raise YtDlpError(
                YtDlpFailureKind.TIMEOUT,
                detail=f"yt-dlp exceeded {self._timeout_seconds}s",
            ) from None

        if process.returncode != 0:
            raise map_failure(stderr.decode("utf-8", errors="replace"))
        return _parse_info_json(stdout)

    async def probe_child(self, source_url: str) -> dict[str, Any]:
        """Probe one persisted child URL, never a collection or flat preview."""
        try:
            url = validate_media_url(source_url)
        except (AnalysisError, ValueError) as exc:
            raise YtDlpError(YtDlpFailureKind.UNSUPPORTED_URL) from exc
        process = await _spawn([
            self._binary, "--ignore-config", "--no-warnings", "--dump-single-json",
            "--skip-download", "--no-playlist", "--playlist-items", "1", "--", url,
        ])
        cancellation = None
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout_seconds
            )
        except TimeoutError:
            raise YtDlpError(
                YtDlpFailureKind.TIMEOUT,
                detail=f"yt-dlp exceeded {self._timeout_seconds}s",
            ) from None
        except asyncio.CancelledError as exc:
            cancellation = exc
            raise
        finally:
            cleanup_cancellation = await _kill_deferred_cancellation(process)
            if cancellation is None and cleanup_cancellation is not None:
                raise cleanup_cancellation
        if process.returncode != 0:
            raise map_failure(stderr.decode("utf-8", errors="replace"))
        info = _parse_info_json(stdout)
        if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
            raise YtDlpError(YtDlpFailureKind.MALFORMED_OUTPUT)
        return info


ProcessCallback = Callable[[asyncio.subprocess.Process], None]


class YtDlpDownloader:
    """Downloads a single selected format and reports normalized progress."""

    def __init__(self, binary: str = "yt-dlp", timeout_seconds: float = 3600.0) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds

    async def download(
        self,
        *,
        url: str,
        format_selector: str,
        output_template: str,
        on_progress: Callable[[DownloadProgress], None],
        on_process_started: ProcessCallback | None = None,
        child_output: bool = False,
    ) -> list[str]:
        """Download one format and return the produced file paths."""
        command = [
            self._binary,
            "--ignore-config",
            "--no-warnings",
            "--newline",
            "--progress",
            "--no-playlist",
            "--progress-template",
            _PROGRESS_TEMPLATE,
            "-f",
            format_selector,
            "-o",
            output_template,
            "--print",
            "after_move:filepath",
            "--no-simulate",
            url,
        ]
        if child_output:
            command[-1:-1] = ["--playlist-items", "1", "--"]
        process = await _spawn(command)
        if on_process_started is not None:
            on_process_started(process)

        stderr_chunks: list[bytes] = []
        stderr_size = 0

        async def _drain_stderr() -> None:
            nonlocal stderr_size
            assert process.stderr is not None
            async for chunk in process.stderr:
                stderr_size += len(chunk)
                if stderr_size <= _STDERR_CAP_BYTES:
                    stderr_chunks.append(chunk)

        produced_files: list[str] = []
        stderr_task = asyncio.create_task(_drain_stderr())
        try:
            async with asyncio.timeout(self._timeout_seconds):
                assert process.stdout is not None
                async for raw_line in process.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line.startswith("PROG "):
                        progress = parse_progress_line(line)
                        if progress is not None:
                            on_progress(progress)
                    elif line.startswith("[") or not line:
                        continue
                    elif child_output or Path(line).is_file():
                        if line not in produced_files:
                            produced_files.append(line)
                await process.wait()
        except TimeoutError:
            await _kill(process)
            stderr_task.cancel()
            raise YtDlpError(
                YtDlpFailureKind.TIMEOUT,
                detail=f"yt-dlp exceeded {self._timeout_seconds}s",
            ) from None
        except BaseException:
            await _kill(process)
            stderr_task.cancel()
            raise
        finally:
            if process.returncode is None:
                await _kill(process)
            if child_output and stderr_task.cancelled():
                await asyncio.gather(stderr_task, return_exceptions=True)
            elif child_output and not stderr_task.done():
                await _finish_stderr_drain(stderr_task)
                await asyncio.gather(stderr_task, return_exceptions=True)

        await _finish_stderr_drain(stderr_task)
        if process.returncode != 0:
            stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
            raise map_failure(stderr)
        if child_output:
            # Validate the expected CLI output name, not filesystem ownership.
            # No resolve/stat here: StorageService validates child ownership.
            expected = Path(output_template)
            if len(produced_files) != 1:
                raise YtDlpError(YtDlpFailureKind.MALFORMED_OUTPUT)
            output = Path(produced_files[0])
            if (output.parent != expected.parent or not output.suffix
                    or output.name != expected.name.replace("%(ext)s", output.suffix[1:])):
                raise YtDlpError(YtDlpFailureKind.MALFORMED_OUTPUT)
        return produced_files


async def _finish_stderr_drain(stderr_task: asyncio.Task[None]) -> None:
    """Wait briefly for stderr EOF; orphaned writers must not block us."""
    if not stderr_task.done():
        await asyncio.wait({stderr_task}, timeout=5.0)
    stderr_task.cancel()
