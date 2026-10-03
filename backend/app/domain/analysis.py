"""Analysis domain models, error taxonomy, and the normalized result shape.

The frontend consumes these normalized models and never sees raw yt-dlp JSON.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class MediaFormatKind(StrEnum):
    VIDEO_ONLY = "video_only"
    AUDIO_ONLY = "audio_only"
    MUXED = "muxed"


class AnalysisErrorCode(StrEnum):
    INVALID_URL = "invalid_url"
    UNSUPPORTED_URL = "unsupported_url"
    AUTHENTICATION_REQUIRED = "authentication_required"
    GEO_RESTRICTED = "geo_restricted"
    MEDIA_UNAVAILABLE = "media_unavailable"
    EXTRACTOR_FAILED = "extractor_failed"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    MALFORMED_EXTRACTOR_OUTPUT = "malformed_extractor_output"
    INTERNAL_ERROR = "internal_error"


class _ErrorSpec(BaseModel):
    http_status: int
    retryable: bool
    message: str


_ERROR_SPECS: dict[AnalysisErrorCode, _ErrorSpec] = {
    AnalysisErrorCode.INVALID_URL: _ErrorSpec(
        http_status=400,
        retryable=False,
        message="Please enter a valid http(s) media URL.",
    ),
    AnalysisErrorCode.UNSUPPORTED_URL: _ErrorSpec(
        http_status=422,
        retryable=False,
        message="This URL is not supported. The site may not be supported by yt-dlp.",
    ),
    AnalysisErrorCode.AUTHENTICATION_REQUIRED: _ErrorSpec(
        http_status=422,
        retryable=False,
        message="This media requires authentication, which is not supported yet.",
    ),
    AnalysisErrorCode.GEO_RESTRICTED: _ErrorSpec(
        http_status=422,
        retryable=False,
        message="This media is not available in the server's region.",
    ),
    AnalysisErrorCode.MEDIA_UNAVAILABLE: _ErrorSpec(
        http_status=422,
        retryable=False,
        message="The media could not be found or is unavailable at the source.",
    ),
    AnalysisErrorCode.EXTRACTOR_FAILED: _ErrorSpec(
        http_status=422,
        retryable=True,
        message=(
            "The source could not be analyzed. The site may have changed "
            "or yt-dlp may need an update."
        ),
    ),
    AnalysisErrorCode.NETWORK_ERROR: _ErrorSpec(
        http_status=422,
        retryable=True,
        message="The server could not reach the media source.",
    ),
    AnalysisErrorCode.TIMEOUT: _ErrorSpec(
        http_status=422,
        retryable=True,
        message="The analysis took too long and was stopped.",
    ),
    AnalysisErrorCode.MALFORMED_EXTRACTOR_OUTPUT: _ErrorSpec(
        http_status=500,
        retryable=True,
        message="The analyzer returned an unexpected response.",
    ),
    AnalysisErrorCode.INTERNAL_ERROR: _ErrorSpec(
        http_status=500,
        retryable=True,
        message="Something went wrong while analyzing this URL.",
    ),
}


class AnalysisError(Exception):
    """A user-meaningful analysis failure.

    ``detail`` carries technical context for logs and a future Activity view;
    ``message`` is the user-facing text and must stay understandable.
    """

    def __init__(
        self,
        code: AnalysisErrorCode,
        detail: str | None = None,
        *,
        retryable: bool | None = None,
    ) -> None:
        spec = _ERROR_SPECS[code]
        self.code = code
        self.message = spec.message
        self.detail = detail
        self.http_status = spec.http_status
        self.retryable = spec.retryable if retryable is None else retryable
        self.analysis_id: str | None = None
        super().__init__(self.message)


def error_retryable(code: str) -> bool:
    """Single source of truth for retryability, shared by POST and GET."""
    try:
        return _ERROR_SPECS[AnalysisErrorCode(code)].retryable
    except ValueError:
        return False


class NormalizedFormat(BaseModel):
    """One meaningful, source-derived format option.

    ``variant_id`` is the application-level, stable identifier clients use to
    select a format. ``format_id`` is the raw yt-dlp id kept for the
    backend-side variant→format mapping stored in the analysis session; the
    UI never displays it. ``label`` is the application-level display label
    for the primary choices list (e.g. ``Best Available (1080p)`` or
    ``1080p``); it describes the chosen output, not the low-level stream.
    """

    variant_id: str
    format_id: str
    label: str | None = None
    kind: MediaFormatKind
    ext: str | None = None
    resolution_label: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    bitrate_kbps: float | None = None
    filesize_bytes: int | None = None
    filesize_approx_bytes: int | None = None


class CollectionItem(BaseModel):
    """Preview entry for a playlist/album item."""

    source_id: str | None = None
    url: str | None = None
    title: str | None = None
    duration_seconds: float | None = None
    position: int | None = None


class CollectionInfo(BaseModel):
    title: str | None = None
    total_items: int
    preview_items: list[CollectionItem] = Field(default_factory=list)
    truncated: bool = False


class NormalizedAnalysis(BaseModel):
    """The normalized analysis result returned to the frontend."""

    analysis_id: str
    original_url: str
    source_id: str | None = None
    detected_platform: str
    detected_platform_label: str
    extractor: str | None = None
    title: str | None = None
    uploader: str | None = None
    duration_seconds: float | None = None
    thumbnail_url: str | None = None
    content_type: Literal["single", "collection"]
    formats: list[NormalizedFormat] = Field(default_factory=list)
    collection: CollectionInfo | None = None
    platform_hint: str | None = None
    platform_mismatch: bool = False


class AnalysisErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool = False
    analysis_id: str | None = None
