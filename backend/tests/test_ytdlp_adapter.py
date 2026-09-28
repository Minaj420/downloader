import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.integrations.ytdlp import (
    DownloadProgress,
    YtDlpAnalyzer,
    YtDlpDownloader,
    YtDlpError,
    YtDlpFailureKind,
    parse_progress_line,
)

JSON_SCRIPT = """#!/bin/sh
cat <<'JSON'
{"_type": "video", "title": "Fake Result", "extractor_key": "Youtube"}
JSON
"""

ARGS_SCRIPT = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a" >> "$ARGS_FILE"; done
printf '%s' '{"_type": "video", "title": "ok"}'
"""

FAIL_SCRIPT = """#!/bin/sh
echo 'ERROR: Unsupported URL: https://example.com/nope' >&2
exit 1
"""

SLOW_SCRIPT = """#!/bin/sh
sleep 5
"""

MALFORMED_SCRIPT = """#!/bin/sh
printf 'not json at all'
"""

DOWNLOAD_SCRIPT = """#!/bin/sh
out=""
next=0
url=""
for a in "$@"; do
  if [ "$next" = "1" ]; then out="$a"; next=0; fi
  [ "$a" = "-o" ] && next=1
  url="$a"
done
out=$(printf '%s' "$out" | sed 's/%(ext)s/mp4/g')
printf 'PROG 3 10 NA 1000000 7\\n'
printf 'fake media' > "$out"
printf '%s\\n' "$out"
"""


def _write_script(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def _run(analyzer: YtDlpAnalyzer, url: str = "https://example.com/video") -> dict[str, Any]:
    return asyncio.run(analyzer.analyze(url))


class TestAnalyzer:
    def test_parses_json_stdout(self, tmp_path: Path) -> None:
        analyzer = YtDlpAnalyzer(
            binary=_write_script(tmp_path, "yt-dlp", JSON_SCRIPT), timeout_seconds=10
        )

        result = _run(analyzer)

        assert result["title"] == "Fake Result"

    def test_uses_argument_array_without_shell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        args_file = tmp_path / "args.txt"
        monkeypatch.setenv("ARGS_FILE", str(args_file))
        analyzer = YtDlpAnalyzer(
            binary=_write_script(tmp_path, "yt-dlp", ARGS_SCRIPT),
            timeout_seconds=10,
            playlist_item_limit=25,
        )

        _run(analyzer, "https://example.com/watch?v=1")

        args = args_file.read_text().splitlines()
        assert args[-1] == "https://example.com/watch?v=1"
        assert "--dump-single-json" in args
        assert "--flat-playlist" in args
        assert "--ignore-config" in args
        assert "--playlist-items" in args
        assert "1-25" in args
        assert all("|" not in arg and ";" not in arg and "&" not in arg for arg in args)

    def test_maps_stderr_failures(self, tmp_path: Path) -> None:
        analyzer = YtDlpAnalyzer(
            binary=_write_script(tmp_path, "yt-dlp", FAIL_SCRIPT), timeout_seconds=10
        )

        with pytest.raises(YtDlpError) as exc_info:
            _run(analyzer)

        assert exc_info.value.kind is YtDlpFailureKind.UNSUPPORTED_URL

    def test_maps_rate_limit_to_network_error(self, tmp_path: Path) -> None:
        script = _write_script(
            tmp_path,
            "yt-dlp",
            "#!/bin/sh\necho 'ERROR: HTTP Error 429: Too Many Requests' >&2\nexit 1\n",
        )
        analyzer = YtDlpAnalyzer(binary=script, timeout_seconds=10)

        with pytest.raises(YtDlpError) as exc_info:
            _run(analyzer)

        assert exc_info.value.kind is YtDlpFailureKind.NETWORK_ERROR

    def test_timeout_kills_process(self, tmp_path: Path) -> None:
        analyzer = YtDlpAnalyzer(
            binary=_write_script(tmp_path, "yt-dlp", SLOW_SCRIPT), timeout_seconds=0.2
        )

        with pytest.raises(YtDlpError) as exc_info:
            _run(analyzer)

        assert exc_info.value.kind is YtDlpFailureKind.TIMEOUT

    def test_rejects_malformed_json(self, tmp_path: Path) -> None:
        analyzer = YtDlpAnalyzer(
            binary=_write_script(tmp_path, "yt-dlp", MALFORMED_SCRIPT), timeout_seconds=10
        )

        with pytest.raises(YtDlpError) as exc_info:
            _run(analyzer)

        assert exc_info.value.kind is YtDlpFailureKind.MALFORMED_OUTPUT

    def test_reports_missing_binary(self, tmp_path: Path) -> None:
        analyzer = YtDlpAnalyzer(binary=str(tmp_path / "does-not-exist"), timeout_seconds=10)

        with pytest.raises(YtDlpError) as exc_info:
            _run(analyzer)

        assert exc_info.value.kind is YtDlpFailureKind.BINARY_MISSING


class TestProgressParsing:
    def test_parses_progress_line(self) -> None:
        progress = parse_progress_line("PROG 123 456 NA 1000000 7")

        assert progress == DownloadProgress(
            downloaded_bytes=123,
            total_bytes=456,
            total_bytes_estimate=None,
            speed_bytes_per_second=1_000_000,
            eta_seconds=7,
        )

    def test_parses_estimate_when_total_unknown(self) -> None:
        progress = parse_progress_line("PROG 10 NA 512 2000 NA")

        assert progress is not None
        assert progress.total_bytes is None
        assert progress.total_bytes_estimate == 512
        assert progress.eta_seconds is None

    def test_ignores_non_progress_lines(self) -> None:
        assert parse_progress_line("[download] Destination: x") is None
        assert parse_progress_line("PROG NA NA NA NA NA") is None


class TestDownloader:
    def test_download_reports_progress_and_file(self, tmp_path: Path) -> None:
        binary = _write_script(tmp_path, "yt-dlp", DOWNLOAD_SCRIPT)
        downloader = YtDlpDownloader(binary=binary, timeout_seconds=10)
        template = str(tmp_path / "out" / "media.%(ext)s")
        (tmp_path / "out").mkdir()
        progress_calls: list[DownloadProgress] = []

        produced = asyncio.run(
            downloader.download(
                url="https://example.com/watch?v=1",
                format_selector="137",
                output_template=template,
                on_progress=progress_calls.append,
            )
        )

        expected_file = tmp_path / "out" / "media.mp4"
        assert produced == [str(expected_file)]
        assert expected_file.read_text() == "fake media"
        assert progress_calls and progress_calls[0].downloaded_bytes == 3
        assert progress_calls[0].total_bytes == 10
        assert progress_calls[0].speed_bytes_per_second == 1_000_000
        assert progress_calls[0].eta_seconds == 7

    def test_download_passes_format_selector_and_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        args_file = tmp_path / "args.txt"
        monkeypatch.setenv("ARGS_FILE", str(args_file))
        binary = _write_script(tmp_path, "yt-dlp", ARGS_SCRIPT)
        downloader = YtDlpDownloader(binary=binary, timeout_seconds=10)

        asyncio.run(
            downloader.download(
                url="https://example.com/watch?v=1",
                format_selector="251+bestaudio",
                output_template="/tmp/x/media.%(ext)s",
                on_progress=lambda _: None,
            )
        )

        args = args_file.read_text().splitlines()
        selector_index = args.index("-f")
        assert args[selector_index + 1] == "251+bestaudio"
        output_index = args.index("-o")
        assert args[output_index + 1] == "/tmp/x/media.%(ext)s"
        assert "--no-simulate" in args
        assert "--progress" in args
        assert "--no-playlist" in args
        assert args[-1] == "https://example.com/watch?v=1"

    def test_download_failure_maps_stderr(self, tmp_path: Path) -> None:
        binary = _write_script(tmp_path, "yt-dlp", FAIL_SCRIPT)
        downloader = YtDlpDownloader(binary=binary, timeout_seconds=10)

        with pytest.raises(YtDlpError) as exc_info:
            asyncio.run(
                downloader.download(
                    url="https://example.com/watch?v=1",
                    format_selector="137",
                    output_template=str(tmp_path / "media.%(ext)s"),
                    on_progress=lambda _: None,
                )
            )

        assert exc_info.value.kind is YtDlpFailureKind.UNSUPPORTED_URL

    def test_download_timeout(self, tmp_path: Path) -> None:
        binary = _write_script(tmp_path, "yt-dlp", SLOW_SCRIPT)
        downloader = YtDlpDownloader(binary=binary, timeout_seconds=0.2)

        with pytest.raises(YtDlpError) as exc_info:
            asyncio.run(
                downloader.download(
                    url="https://example.com/watch?v=1",
                    format_selector="137",
                    output_template=str(tmp_path / "media.%(ext)s"),
                    on_progress=lambda _: None,
                )
            )

        assert exc_info.value.kind is YtDlpFailureKind.TIMEOUT

    def test_download_missing_binary(self, tmp_path: Path) -> None:
        downloader = YtDlpDownloader(binary=str(tmp_path / "missing"), timeout_seconds=10)

        with pytest.raises(YtDlpError) as exc_info:
            asyncio.run(
                downloader.download(
                    url="https://example.com/watch?v=1",
                    format_selector="137",
                    output_template=str(tmp_path / "media.%(ext)s"),
                    on_progress=lambda _: None,
                )
            )

        assert exc_info.value.kind is YtDlpFailureKind.BINARY_MISSING

    def test_download_exposes_process_for_cancellation(self, tmp_path: Path) -> None:
        binary = _write_script(tmp_path, "yt-dlp", DOWNLOAD_SCRIPT)
        downloader = YtDlpDownloader(binary=binary, timeout_seconds=10)
        seen: list[Any] = []

        asyncio.run(
            downloader.download(
                url="https://example.com/watch?v=1",
                format_selector="137",
                output_template=str(tmp_path / "media.%(ext)s"),
                on_progress=lambda _: None,
                on_process_started=seen.append,
            )
        )

        assert len(seen) == 1
        assert seen[0].returncode == 0
