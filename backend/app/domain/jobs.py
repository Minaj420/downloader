"""Download job domain: state machine, error taxonomy, and DTOs."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    DOWNLOADING = "downloading"
    POST_PROCESSING = "post_processing"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES = frozenset(
    {
        JobStatus.ANALYZING,
        JobStatus.DOWNLOADING,
        JobStatus.POST_PROCESSING,
        JobStatus.FINALIZING,
    }
)
TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)


class JobErrorCode(StrEnum):
    ANALYSIS_NOT_FOUND = "analysis_not_found"
    ANALYSIS_EXPIRED = "analysis_expired"
    INVALID_VARIANT = "invalid_variant"
    INVALID_DESTINATION = "invalid_destination"
    INVALID_STATE = "invalid_state"
    UNSUPPORTED_URL = "unsupported_url"
    YT_DLP_MISSING = "yt_dlp_missing"
    FFMPEG_MISSING = "ffmpeg_missing"
    AUTHENTICATION_REQUIRED = "authentication_required"
    GEO_RESTRICTED = "geo_restricted"
    MEDIA_UNAVAILABLE = "media_unavailable"
    NETWORK_ERROR = "network_error"
    EXTRACTOR_FAILED = "extractor_failed"
    TIMEOUT = "timeout"
    DISK_WRITE_FAILED = "disk_write_failed"
    POST_PROCESSING_FAILED = "post_processing_failed"
    MALFORMED_OUTPUT = "malformed_output"
    CANCELLED = "cancelled"
    INTERRUPTED_BY_RESTART = "interrupted_by_restart"
    INTERNAL_ERROR = "internal_error"


_RETRYABLE_JOB_CODES = frozenset(
    {
        JobErrorCode.NETWORK_ERROR,
        JobErrorCode.TIMEOUT,
        JobErrorCode.EXTRACTOR_FAILED,
        JobErrorCode.MALFORMED_OUTPUT,
        JobErrorCode.INTERNAL_ERROR,
        JobErrorCode.POST_PROCESSING_FAILED,
        JobErrorCode.ANALYSIS_EXPIRED,
        JobErrorCode.INTERRUPTED_BY_RESTART,
    }
)

_HTTP_STATUS_BY_CODE: dict[JobErrorCode, int] = {
    JobErrorCode.ANALYSIS_NOT_FOUND: 404,
    JobErrorCode.ANALYSIS_EXPIRED: 422,
    JobErrorCode.INVALID_VARIANT: 422,
    JobErrorCode.INVALID_DESTINATION: 422,
    JobErrorCode.INVALID_STATE: 409,
}

_MESSAGES_BY_CODE: dict[JobErrorCode, str] = {
    JobErrorCode.ANALYSIS_NOT_FOUND: "This analysis session does not exist.",
    JobErrorCode.ANALYSIS_EXPIRED: "This analysis has expired. Analyze the URL again.",
    JobErrorCode.INVALID_VARIANT: (
        "The selected format is not available for this analysis. "
        "Analyze the URL again and pick one of the offered formats."
    ),
    JobErrorCode.INVALID_DESTINATION: "This destination is not supported yet.",
    JobErrorCode.INVALID_STATE: "This job has already finished.",
    JobErrorCode.UNSUPPORTED_URL: "The source is no longer supported for download.",
    JobErrorCode.YT_DLP_MISSING: (
        "The download engine (yt-dlp) is not available on the server."
    ),
    JobErrorCode.FFMPEG_MISSING: (
        "FFmpeg is not available on the server, which is required for this format."
    ),
    JobErrorCode.AUTHENTICATION_REQUIRED: (
        "This media requires authentication, which is not supported yet."
    ),
    JobErrorCode.GEO_RESTRICTED: "This media is not available in the server's region.",
    JobErrorCode.MEDIA_UNAVAILABLE: (
        "The media could not be found or is unavailable at the source."
    ),
    JobErrorCode.NETWORK_ERROR: "The server could not reach the media source.",
    JobErrorCode.EXTRACTOR_FAILED: (
        "The download failed. The site may have changed or yt-dlp may need an update."
    ),
    JobErrorCode.TIMEOUT: "The download took too long and was stopped.",
    JobErrorCode.DISK_WRITE_FAILED: (
        "The file could not be written. The disk may be full or a path is not writable."
    ),
    JobErrorCode.POST_PROCESSING_FAILED: (
        "Post-processing the downloaded media failed."
    ),
    JobErrorCode.MALFORMED_OUTPUT: "The download engine returned an unexpected result.",
    JobErrorCode.CANCELLED: "The download was cancelled.",
    JobErrorCode.INTERRUPTED_BY_RESTART: (
        "The application restarted while this job was running. You can retry it."
    ),
    JobErrorCode.INTERNAL_ERROR: "Something went wrong while running this download.",
}


class JobError(Exception):
    """A user-meaningful job failure.

    ``detail`` carries technical context for logs and Activity; ``message`` is
    the user-facing text.
    """

    def __init__(
        self,
        code: JobErrorCode,
        detail: str | None = None,
        *,
        retryable: bool | None = None,
    ) -> None:
        self.code = code
        self.message = _MESSAGES_BY_CODE[code]
        self.detail = detail
        self.http_status = _HTTP_STATUS_BY_CODE.get(code, 500)
        self.retryable = (
            code in _RETRYABLE_JOB_CODES if retryable is None else retryable
        )
        self.job_id: str | None = None
        super().__init__(self.message)


def job_error_code_retryable(code: str) -> bool:
    try:
        return JobErrorCode(code) in _RETRYABLE_JOB_CODES
    except ValueError:
        return False


class JobProgress(BaseModel):
    percent: float = 0.0
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed_bytes_per_second: int | None = None
    eta_seconds: int | None = None


class JobErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool = False


class JobDto(BaseModel):
    id: str
    analysis_id: str
    variant_id: str
    status: JobStatus
    title: str
    source_url: str
    destination: str
    progress: JobProgress = Field(default_factory=JobProgress)
    final_path: str | None = None
    error: JobErrorBody | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
