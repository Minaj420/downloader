"""yt-dlp integration adapter.

yt-dlp runs as an isolated subprocess via ``asyncio.create_subprocess_exec``
with an argument array (never a shell string). The adapter hides all CLI
details — invocation, timeouts, output parsing, and failure mapping — from the
rest of the codebase (ARCHITECTURE.md §11–12).

Manual platform selection is a hint, not a forced extractor: yt-dlp has no
reliable generic "force extractor" switch, so the hint is validated after
detection and surfaced as a mismatch instead of a cryptic failure
(ARCHITECTURE.md §45).
"""

import asyncio
import json
from typing import Any

from app.domain.analysis import AnalysisError, AnalysisErrorCode

_PLAYLIST_ITEM_LIMIT = 25

_KNOWN_FAILURE_SIGNATURES: tuple[tuple[str, AnalysisErrorCode, bool], ...] = (
    ("unsupported url", AnalysisErrorCode.UNSUPPORTED_URL, False),
    ("sign in to confirm", AnalysisErrorCode.AUTHENTICATION_REQUIRED, False),
    ("login required", AnalysisErrorCode.AUTHENTICATION_REQUIRED, False),
    ("log in to", AnalysisErrorCode.AUTHENTICATION_REQUIRED, False),
    ("private video", AnalysisErrorCode.AUTHENTICATION_REQUIRED, False),
    ("members-only", AnalysisErrorCode.AUTHENTICATION_REQUIRED, False),
    ("members only", AnalysisErrorCode.AUTHENTICATION_REQUIRED, False),
    ("authentication", AnalysisErrorCode.AUTHENTICATION_REQUIRED, False),
    ("not available in your country", AnalysisErrorCode.GEO_RESTRICTED, False),
    ("geo-restricted", AnalysisErrorCode.GEO_RESTRICTED, False),
    ("geo restricted", AnalysisErrorCode.GEO_RESTRICTED, False),
    ("video unavailable", AnalysisErrorCode.MEDIA_UNAVAILABLE, False),
    ("has been removed", AnalysisErrorCode.MEDIA_UNAVAILABLE, False),
    ("removed by the uploader", AnalysisErrorCode.MEDIA_UNAVAILABLE, False),
    ("does not exist", AnalysisErrorCode.MEDIA_UNAVAILABLE, False),
    ("not exist", AnalysisErrorCode.MEDIA_UNAVAILABLE, False),
    ("404", AnalysisErrorCode.MEDIA_UNAVAILABLE, False),
    ("not found", AnalysisErrorCode.MEDIA_UNAVAILABLE, False),
    ("unavailable", AnalysisErrorCode.MEDIA_UNAVAILABLE, False),
    ("rate limit", AnalysisErrorCode.NETWORK_ERROR, True),
    ("http error 429", AnalysisErrorCode.NETWORK_ERROR, True),
    ("timed out", AnalysisErrorCode.NETWORK_ERROR, True),
    ("timeout", AnalysisErrorCode.NETWORK_ERROR, True),
    ("connection", AnalysisErrorCode.NETWORK_ERROR, True),
    ("getaddrinfo", AnalysisErrorCode.NETWORK_ERROR, True),
    ("temporary failure", AnalysisErrorCode.NETWORK_ERROR, True),
    ("name or service not known", AnalysisErrorCode.NETWORK_ERROR, True),
    ("network", AnalysisErrorCode.NETWORK_ERROR, True),
    ("ssl", AnalysisErrorCode.NETWORK_ERROR, True),
    ("unable to download", AnalysisErrorCode.NETWORK_ERROR, True),
    ("unable to extract", AnalysisErrorCode.EXTRACTOR_FAILED, True),
)


def _map_failure(stderr: str) -> AnalysisError:
    lowered = stderr.lower()
    for signature, code, retryable in _KNOWN_FAILURE_SIGNATURES:
        if signature in lowered:
            return AnalysisError(code, detail=stderr, retryable=retryable)
    return AnalysisError(
        AnalysisErrorCode.EXTRACTOR_FAILED, detail=stderr, retryable=True
    )


def _parse_info_json(stdout: bytes) -> dict[str, Any]:
    text = stdout.decode("utf-8", errors="replace").strip()
    start = text.find("{")
    if start < 0:
        raise AnalysisError(
            AnalysisErrorCode.MALFORMED_EXTRACTOR_OUTPUT,
            detail="yt-dlp produced no JSON output",
        )
    try:
        parsed = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise AnalysisError(
            AnalysisErrorCode.MALFORMED_EXTRACTOR_OUTPUT,
            detail=f"yt-dlp output is not valid JSON: {exc}",
        ) from exc
    if not isinstance(parsed, dict):
        raise AnalysisError(
            AnalysisErrorCode.MALFORMED_EXTRACTOR_OUTPUT,
            detail="yt-dlp output is not a JSON object",
        )
    return parsed


class YtDlpAnalyzer:
    """Runs yt-dlp in metadata-only mode and returns the raw info dict."""

    def __init__(
        self,
        binary: str = "yt-dlp",
        timeout_seconds: float = 90.0,
        playlist_item_limit: int = _PLAYLIST_ITEM_LIMIT,
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
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AnalysisError(
                AnalysisErrorCode.INTERNAL_ERROR,
                detail=f"yt-dlp could not be started: {exc}",
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise AnalysisError(
                AnalysisErrorCode.TIMEOUT,
                detail=f"yt-dlp exceeded {self._timeout_seconds}s",
            ) from None

        if process.returncode != 0:
            raise _map_failure(stderr.decode("utf-8", errors="replace"))
        return _parse_info_json(stdout)
