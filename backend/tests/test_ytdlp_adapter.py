import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from app.domain.analysis import COLLECTION_ANALYSIS_WINDOW_LIMIT, COLLECTION_SELECTED_ITEM_LIMIT
from app.integrations.ytdlp import (
    DownloadProgress,
    YtDlpAnalyzer,
    YtDlpDownloader,
    YtDlpError,
    YtDlpFailureKind,
    _kill,
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


@pytest.mark.parametrize("child_output", [False, True])
def test_download_child_bound_preserves_single_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, child_output: bool,
) -> None:
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("ARGS_FILE", str(args_file))
    body = DOWNLOAD_SCRIPT.replace('out=""',
        'for a in "$@"; do printf \'%s\\n\' "$a" >> "$ARGS_FILE"; done\nout=""')
    downloader = YtDlpDownloader(binary=_write_script(tmp_path, "download", body))
    produced = asyncio.run(downloader.download(
        url="https://example.com/child?list=x", format_selector="native",
        output_template=str(tmp_path / "media.%(ext)s"), on_progress=lambda _: None,
        child_output=child_output,
    ))
    assert produced == [str(tmp_path / "media.mp4")]
    args = args_file.read_text().splitlines()
    assert "--no-playlist" in args
    if child_output:
        assert args[-4:] == ["--playlist-items", "1", "--", "https://example.com/child?list=x"]
    else:
        assert "--playlist-items" not in args and "--" not in args


@pytest.mark.parametrize("output", ["empty", "multiple", "unexpected", "outside"])
def test_child_download_rejects_wrong_expected_output(tmp_path: Path, output: str) -> None:
    lines = {
        "empty": [], "multiple": [tmp_path / "media.mp4", tmp_path / "media.webm"],
        "unexpected": [tmp_path / "other.mp4"], "outside": [tmp_path.parent / "media.mp4"],
    }[output]
    body = "#!/bin/sh\n" + "".join(f"printf '%s\\n' '{line}'\n" for line in lines)
    downloader = YtDlpDownloader(binary=_write_script(tmp_path, "download", body))
    with pytest.raises(YtDlpError) as caught:
        asyncio.run(downloader.download(
            url="https://example.com/child", format_selector="native",
            output_template=str(tmp_path / "media.%(ext)s"), on_progress=lambda _: None,
            child_output=True,
        ))
    assert caught.value.kind == YtDlpFailureKind.MALFORMED_OUTPUT


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
        )

        _run(analyzer, "https://example.com/watch?v=1")

        args = args_file.read_text().splitlines()
        assert args[-1] == "https://example.com/watch?v=1"
        assert "--dump-single-json" in args
        assert "--flat-playlist" in args
        assert "--ignore-config" in args
        assert "--playlist-items" in args
        assert "1-25" in args
        assert COLLECTION_ANALYSIS_WINDOW_LIMIT == 25
        assert COLLECTION_SELECTED_ITEM_LIMIT == 200
        assert "1-200" not in args
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


class _ControlledProbeProcess:
    def __init__(self, outcome: str, exit_race: bool = False) -> None:
        self.outcome = outcome
        self.exit_race = exit_race
        self.returncode: int | None = None
        self.communicating = asyncio.Event()
        self.waiting = asyncio.Event()
        self.release_wait = asyncio.Event()
        self.kill_calls = 0
        self.wait_calls = 0
        self.wait_task: asyncio.Task[Any] | None = None
        self.reaped = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicating.set()
        if self.outcome == "timeout":
            raise TimeoutError
        if self.outcome == "cancel":
            await asyncio.Event().wait()
        return b'{"_type":"video","title":"ok"}', b""

    def kill(self) -> None:
        self.kill_calls += 1
        if self.exit_race:
            self.returncode = 0
            raise ProcessLookupError

    async def wait(self) -> int:
        self.wait_calls += 1
        self.wait_task = asyncio.current_task()
        self.waiting.set()
        await self.release_wait.wait()
        if self.returncode is None:
            self.returncode = 0 if self.outcome == "normal" else -9
        self.reaped = True
        return self.returncode


class TestChildProbe:
    def test_single_metadata_args(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        args_file = tmp_path / "args.txt"
        monkeypatch.setenv("ARGS_FILE", str(args_file))
        analyzer = YtDlpAnalyzer(binary=_write_script(tmp_path, "yt-dlp", ARGS_SCRIPT))
        result = asyncio.run(analyzer.probe_child(" https://example.com/watch?v=1&list=x "))
        assert result["title"] == "ok"
        args = args_file.read_text().splitlines()
        assert args == [
            "--ignore-config", "--no-warnings", "--dump-single-json",
            "--skip-download", "--no-playlist", "--playlist-items", "1", "--",
            "https://example.com/watch?v=1&list=x",
        ]

    @pytest.mark.parametrize("url", ["--exec=x", "file:///etc/passwd", "https://[invalid"])
    def test_invalid_url_rejected_before_spawn(self, tmp_path: Path, url: str) -> None:
        analyzer = YtDlpAnalyzer(binary=str(tmp_path / "missing"))
        with pytest.raises(YtDlpError) as exc:
            asyncio.run(analyzer.probe_child(url))
        assert exc.value.kind == YtDlpFailureKind.UNSUPPORTED_URL

    @pytest.mark.parametrize("payload", [
        '{"_type":"playlist","entries":[]}', '{"_type":"multi_video"}',
        '{"entries":[]}', 'not-json',
    ])
    def test_wrong_collection_or_malformed_output(self, tmp_path: Path, payload: str) -> None:
        binary = _write_script(tmp_path, "yt-dlp", f"#!/bin/sh\nprintf '%s' '{payload}'\n")
        with pytest.raises(YtDlpError) as exc:
            asyncio.run(YtDlpAnalyzer(binary=binary).probe_child("https://example.com/a"))
        assert exc.value.kind == YtDlpFailureKind.MALFORMED_OUTPUT

    @pytest.mark.parametrize("cancel", [False, True])
    def test_timeout_and_cancellation_reap_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
    ) -> None:
        binary = _write_script(tmp_path, "yt-dlp", "#!/bin/sh\nexec sleep 5\n")
        processes: list[asyncio.subprocess.Process] = []
        spawn = asyncio.create_subprocess_exec

        async def capture(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            process = await spawn(*args, **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

        async def run() -> None:
            analyzer = YtDlpAnalyzer(binary=binary, timeout_seconds=0.1)
            task = asyncio.create_task(analyzer.probe_child("https://example.com/a"))
            if cancel:
                while not processes:
                    await asyncio.sleep(0.001)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(YtDlpError) as exc:
                    await task
                assert exc.value.kind == YtDlpFailureKind.TIMEOUT
            assert len(processes) == 1 and processes[0].returncode is not None

        asyncio.run(run())

    @pytest.mark.parametrize("outcome", ["cancel", "timeout", "normal"])
    @pytest.mark.parametrize("exit_race", [False, True])
    @pytest.mark.parametrize("cleanup_cancels", [0, 3])
    def test_cleanup_owns_reap_through_repeated_cancellation(
        self, monkeypatch: pytest.MonkeyPatch,
        outcome: str, exit_race: bool, cleanup_cancels: int,
    ) -> None:
        async def run() -> None:
            process = _ControlledProbeProcess(outcome, exit_race)

            async def spawn(command: list[str]) -> asyncio.subprocess.Process:
                return cast(asyncio.subprocess.Process, process)

            monkeypatch.setattr("app.integrations.ytdlp._spawn", spawn)
            task = asyncio.create_task(
                YtDlpAnalyzer().probe_child("https://example.com/a")
            )
            await process.communicating.wait()
            if outcome == "cancel":
                task.cancel("original cancellation")
            await process.waiting.wait()
            assert process.wait_task is not None
            for index in range(cleanup_cancels):
                assert not process.wait_task.done()
                task.cancel(f"cleanup cancellation {index}")
                await asyncio.sleep(0)  # Deliver each cancellation while reap is blocked.
                assert not task.done()
                assert not process.wait_task.done()
            process.release_wait.set()
            if outcome == "cancel" or cleanup_cancels:
                with pytest.raises(asyncio.CancelledError) as exc:
                    await task
                expected = (
                    "original cancellation" if outcome == "cancel"
                    else "cleanup cancellation 0"
                )
                assert exc.value.args == (expected,)
            elif outcome == "timeout":
                with pytest.raises(YtDlpError) as error:
                    await task
                assert error.value.kind is YtDlpFailureKind.TIMEOUT
            else:
                assert (await task)["title"] == "ok"
            assert process.kill_calls == process.wait_calls == 1
            assert process.reaped and process.returncode is not None
            assert process.wait_task.done() and not process.wait_task.cancelled()
            assert asyncio.all_tasks() == {asyncio.current_task()}

        asyncio.run(run())

    @pytest.mark.parametrize("already_exited", [False, True])
    def test_kill_and_reap_is_idempotent(
        self, already_exited: bool,
    ) -> None:
        async def run() -> None:
            process = _ControlledProbeProcess("normal", exit_race=True)
            if already_exited:
                process.returncode = 0
            process.release_wait.set()
            for _ in range(2):
                await _kill(cast(asyncio.subprocess.Process, process))
            assert process.kill_calls == (0 if already_exited else 1)
            assert process.wait_calls == 2
            assert process.reaped and process.returncode == 0

        asyncio.run(run())


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
