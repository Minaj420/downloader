"""M6-Q19: raw yt-dlp failure detail reaches the container log (single and child).

Real runner catch flows are driven with the existing stub adapters; only the
log output is new. Failure classification and persistence stay as before.
"""

import logging
from pathlib import Path
from typing import Any

import pytest

from app.domain.jobs import JobError, JobErrorCode
from app.integrations.ytdlp import YtDlpError, YtDlpFailureKind
from tests.test_collection_child_runner import Harness
from tests.test_single_conversion import AUDIO, SingleRun, fmt

RAW_DETAIL = "ERROR: [extractor] raw stderr line\nsecond line"
NORMALIZED_DETAIL = "ERROR: [extractor] raw stderr line second line"
SMOKE_RAW = (
    "[download]   3.1% of 10.00MiB at 1.00MiB/s ETA 00:09\r"
    "PROG 3 10 NA 1000000 7\r\n"
    "\x1b[0;31mERROR:\x1b[0m \r[download] Got error: [Errno -3] "
    "Temporary failure in name resolution. Giving up after 10 retries\n"
)
SMOKE_LINE = (
    "ERROR: [download] Got error: [Errno -3] Temporary failure in name resolution. "
    "Giving up after 10 retries"
)
SINGLE_LOGGER = "app.services.job_runner"
CHILD_LOGGER = "app.services.collection_child_runner"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _failure_records(
    caplog: pytest.LogCaptureFixture, name: str
) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == name and record.levelno >= logging.ERROR
    ]


def _raiser(error: YtDlpError) -> Any:
    async def download(*args: Any, **kwargs: Any) -> list[str]:
        raise error

    return download


@pytest.mark.anyio
async def test_single_failure_logs_raw_detail_and_job_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run = SingleRun(tmp_path)
    try:
        await run.seed([fmt("a", AUDIO, audio="opus", bitrate=160.0)], "a", None)
        run.media.download = _raiser(  # type: ignore[method-assign]
            YtDlpError(YtDlpFailureKind.NETWORK_ERROR, RAW_DETAIL)
        )
        with caplog.at_level(logging.INFO):
            await run.run()
        facts = await run.facts()
    finally:
        await run.engine.dispose()

    [record] = _failure_records(caplog, SINGLE_LOGGER)
    message = record.getMessage()
    assert NORMALIZED_DETAIL in message
    assert "\n" not in message and "\r" not in message
    assert "job_id=job" in message
    assert "kind=network_error" in message
    assert record.exc_info is None
    assert record.msg == "yt-dlp job failed: job_id=%s kind=%s detail=%s"
    # Existing failure behaviour is unchanged.
    assert facts.record.status == "failed"
    assert facts.record.error_code == JobErrorCode.NETWORK_ERROR.value
    assert facts.record.error_detail == NORMALIZED_DETAIL


REPEATED_RAW = ("\x1b[0;31mERROR:\x1b[0m \r" + SMOKE_LINE.removeprefix("ERROR: ") + "\r") * 30
LONG_RAW = "ERROR: " + "y" * 800
LONG_LINE = LONG_RAW[:499] + "…"


@pytest.mark.anyio
async def test_single_repeated_and_long_detail_deduped_and_capped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for raw, expected in ((REPEATED_RAW, SMOKE_LINE), (LONG_RAW, LONG_LINE)):
        caplog.clear()
        case_dir = tmp_path / str(len(expected))
        case_dir.mkdir()
        run = SingleRun(case_dir)
        try:
            await run.seed([fmt("a", AUDIO, audio="opus", bitrate=160.0)], "a", None)
            run.media.download = _raiser(  # type: ignore[method-assign]
                YtDlpError(YtDlpFailureKind.NETWORK_ERROR, raw)
            )
            with caplog.at_level(logging.INFO):
                await run.run()
            facts = await run.facts()
        finally:
            await run.engine.dispose()

        [record] = _failure_records(caplog, SINGLE_LOGGER)
        assert record.args == ("job", "network_error", expected)
        assert facts.record.error_detail == expected
        assert len(expected) <= 500


@pytest.mark.anyio
async def test_child_repeated_and_long_detail_deduped_and_capped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    for raw, expected in ((REPEATED_RAW, SMOKE_LINE), (LONG_RAW, LONG_LINE)):
        caplog.clear()
        case_dir = tmp_path / str(len(expected))
        case_dir.mkdir()
        h = Harness(case_dir)
        try:
            await h.seed("audio")
            monkeypatch.setattr(
                h.adapters, "download",
                _raiser(YtDlpError(YtDlpFailureKind.NETWORK_ERROR, raw)),
            )
            with caplog.at_level(logging.INFO), pytest.raises(JobError) as caught:
                await h.run()
        finally:
            await h.engine.dispose()

        [record] = _failure_records(caplog, CHILD_LOGGER)
        assert expected in record.getMessage()
        assert caught.value.detail == expected
        assert len(expected) <= 500


@pytest.mark.anyio
async def test_single_smoke_detail_is_one_line_in_log_and_db(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run = SingleRun(tmp_path)
    try:
        await run.seed([fmt("a", AUDIO, audio="opus", bitrate=160.0)], "a", None)
        run.media.download = _raiser(  # type: ignore[method-assign]
            YtDlpError(YtDlpFailureKind.NETWORK_ERROR, SMOKE_RAW)
        )
        with caplog.at_level(logging.INFO):
            await run.run()
        facts = await run.facts()
    finally:
        await run.engine.dispose()

    [record] = _failure_records(caplog, SINGLE_LOGGER)
    assert record.args == ("job", "network_error", SMOKE_LINE)
    assert "\n" not in record.getMessage() and "\r" not in record.getMessage()
    assert facts.record.error_code == JobErrorCode.NETWORK_ERROR.value
    assert facts.record.error_detail == SMOKE_LINE


@pytest.mark.anyio
async def test_single_failure_without_detail_logs_kind(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run = SingleRun(tmp_path)
    try:
        await run.seed([fmt("a", AUDIO, audio="opus", bitrate=160.0)], "a", None)
        run.media.download = _raiser(  # type: ignore[method-assign]
            YtDlpError(YtDlpFailureKind.TIMEOUT)
        )
        with caplog.at_level(logging.INFO):
            await run.run()
        facts = await run.facts()
    finally:
        await run.engine.dispose()

    [record] = _failure_records(caplog, SINGLE_LOGGER)
    message = record.getMessage()
    assert "kind=timeout" in message
    assert "job_id=job" in message
    assert "detail=<none>" in message
    assert (facts.record.status, facts.record.error_code) == (
        "failed", JobErrorCode.TIMEOUT.value)


@pytest.mark.anyio
async def test_single_success_has_no_failure_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run = SingleRun(tmp_path)
    try:
        await run.seed([fmt("a", AUDIO, audio="opus", bitrate=160.0)], "a", None)
        with caplog.at_level(logging.INFO):
            await run.run()
        facts = await run.facts()
    finally:
        await run.engine.dispose()

    assert facts.record.status == "completed"
    assert _failure_records(caplog, SINGLE_LOGGER) == []
    assert not [r for r in caplog.records if "yt-dlp" in r.getMessage()]


@pytest.mark.anyio
async def test_child_failure_logs_raw_detail_parent_and_child_ids(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        monkeypatch.setattr(
            h.adapters, "download",
            _raiser(YtDlpError(YtDlpFailureKind.GEO_RESTRICTED, RAW_DETAIL)),
        )
        with caplog.at_level(logging.INFO), pytest.raises(JobError) as caught:
            await h.run()
    finally:
        await h.engine.dispose()

    [record] = _failure_records(caplog, CHILD_LOGGER)
    message = record.getMessage()
    assert NORMALIZED_DETAIL in message
    assert "\n" not in message and "\r" not in message
    assert "job_id=parent" in message
    assert "child_id=child-2" in message
    assert "kind=geo_restricted" in message
    assert record.exc_info is None
    # Existing mapping is unchanged.
    assert caught.value.code == JobErrorCode.GEO_RESTRICTED
    assert caught.value.detail == NORMALIZED_DETAIL


@pytest.mark.anyio
async def test_child_smoke_detail_is_one_line_in_log_and_job_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        monkeypatch.setattr(
            h.adapters, "download",
            _raiser(YtDlpError(YtDlpFailureKind.NETWORK_ERROR, SMOKE_RAW)),
        )
        with caplog.at_level(logging.INFO), pytest.raises(JobError) as caught:
            await h.run()
    finally:
        await h.engine.dispose()

    [record] = _failure_records(caplog, CHILD_LOGGER)
    assert SMOKE_LINE in record.getMessage()
    assert "\n" not in record.getMessage() and "\r" not in record.getMessage()
    assert caught.value.code == JobErrorCode.NETWORK_ERROR
    assert caught.value.detail == SMOKE_LINE


@pytest.mark.anyio
async def test_child_failure_without_detail_logs_kind(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        monkeypatch.setattr(
            h.adapters, "download", _raiser(YtDlpError(YtDlpFailureKind.MALFORMED_OUTPUT))
        )
        with caplog.at_level(logging.INFO), pytest.raises(JobError) as caught:
            await h.run()
    finally:
        await h.engine.dispose()

    [record] = _failure_records(caplog, CHILD_LOGGER)
    message = record.getMessage()
    assert "kind=malformed_output" in message
    assert "detail=<none>" in message
    assert caught.value.code == JobErrorCode.MALFORMED_OUTPUT


@pytest.mark.anyio
async def test_child_success_has_no_failure_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h = Harness(tmp_path)
    try:
        await h.seed("audio")
        with caplog.at_level(logging.INFO):
            await h.run()
    finally:
        await h.engine.dispose()

    assert _failure_records(caplog, CHILD_LOGGER) == []
    assert not [r for r in caplog.records if "yt-dlp" in r.getMessage()]
