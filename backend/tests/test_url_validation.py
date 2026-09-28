import pytest

from app.domain.analysis import AnalysisError, AnalysisErrorCode
from app.utils.urls import validate_media_url


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/watch?v=1",
        "http://example.com/audio",
        "  https://example.com/padded  ",
    ],
)
def test_accepts_http_urls(raw: str) -> None:
    assert validate_media_url(raw) == raw.strip()


@pytest.mark.parametrize(
    "raw",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "unix:///tmp/socket",
        "javascript:alert(1)",
        "/etc/passwd",
        "C:\\Users\\someone\\file.mp4",
        "example.com/no-scheme",
        "   ",
    ],
)
def test_rejects_unsafe_urls(raw: str) -> None:
    with pytest.raises(AnalysisError) as exc_info:
        validate_media_url(raw)
    assert exc_info.value.code is AnalysisErrorCode.INVALID_URL
    assert exc_info.value.http_status == 400
