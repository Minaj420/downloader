import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.domain.analysis import AnalysisError, AnalysisErrorCode
from app.integrations.ytdlp import YtDlpAnalyzer

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


def _write_script(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def _run(analyzer: YtDlpAnalyzer, url: str = "https://example.com/video") -> dict[str, Any]:
    return asyncio.run(analyzer.analyze(url))


def test_adapter_parses_json_stdout(tmp_path: Path) -> None:
    analyzer = YtDlpAnalyzer(
        binary=_write_script(tmp_path, "yt-dlp", JSON_SCRIPT), timeout_seconds=10
    )

    result = _run(analyzer)

    assert result["title"] == "Fake Result"


def test_adapter_uses_argument_array_without_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    # No shell metacharacters: every argument is a standalone value.
    assert all("|" not in arg and ";" not in arg and "&" not in arg for arg in args)


def test_adapter_maps_stderr_failures(tmp_path: Path) -> None:
    analyzer = YtDlpAnalyzer(
        binary=_write_script(tmp_path, "yt-dlp", FAIL_SCRIPT), timeout_seconds=10
    )

    with pytest.raises(AnalysisError) as exc_info:
        _run(analyzer)

    assert exc_info.value.code is AnalysisErrorCode.UNSUPPORTED_URL


def test_adapter_maps_rate_limit_to_retryable_network_error(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path,
        "yt-dlp",
        "#!/bin/sh\necho 'ERROR: HTTP Error 429: Too Many Requests' >&2\nexit 1\n",
    )
    analyzer = YtDlpAnalyzer(binary=script, timeout_seconds=10)

    with pytest.raises(AnalysisError) as exc_info:
        _run(analyzer)

    assert exc_info.value.code is AnalysisErrorCode.NETWORK_ERROR
    assert exc_info.value.retryable is True


def test_adapter_timeout_kills_process(tmp_path: Path) -> None:
    analyzer = YtDlpAnalyzer(
        binary=_write_script(tmp_path, "yt-dlp", SLOW_SCRIPT), timeout_seconds=0.2
    )

    with pytest.raises(AnalysisError) as exc_info:
        _run(analyzer)

    assert exc_info.value.code is AnalysisErrorCode.TIMEOUT


def test_adapter_rejects_malformed_json(tmp_path: Path) -> None:
    analyzer = YtDlpAnalyzer(
        binary=_write_script(tmp_path, "yt-dlp", MALFORMED_SCRIPT), timeout_seconds=10
    )

    with pytest.raises(AnalysisError) as exc_info:
        _run(analyzer)

    assert exc_info.value.code is AnalysisErrorCode.MALFORMED_EXTRACTOR_OUTPUT


def test_adapter_reports_missing_binary(tmp_path: Path) -> None:
    analyzer = YtDlpAnalyzer(binary=str(tmp_path / "does-not-exist"), timeout_seconds=10)

    with pytest.raises(AnalysisError) as exc_info:
        _run(analyzer)

    assert exc_info.value.code is AnalysisErrorCode.INTERNAL_ERROR
