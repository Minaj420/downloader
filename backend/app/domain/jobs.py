"""Download job domain: state machine, error taxonomy, and DTOs."""

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    INVALID_SELECTION = "invalid_selection"
    INVALID_PROFILE = "invalid_profile"
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
    PARTIAL_FAILURE = "partial_failure"
    ALL_CHILDREN_FAILED = "all_children_failed"


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
    JobErrorCode.INVALID_SELECTION: 422,
    JobErrorCode.INVALID_PROFILE: 422,
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
    JobErrorCode.INVALID_SELECTION: (
        "Select 1 to 200 distinct available items from this analysis snapshot."
    ),
    JobErrorCode.INVALID_PROFILE: "This output preference is not supported.",
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
    JobErrorCode.PARTIAL_FAILURE: "Some selected items failed; completed items are preserved.",
    JobErrorCode.ALL_CHILDREN_FAILED: "All selected items failed.",
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


def collection_child_retryable(status: str, error_code: str | None) -> bool:
    """Persisted child policy; incomplete rows are used only after attempt drain."""
    return (
        status in {JobStatus.CANCELLED, JobStatus.QUEUED, *ACTIVE_JOB_STATUSES}
        or (status == JobStatus.FAILED and job_error_code_retryable(error_code or ""))
    )


def collection_retryable(
    parent_status: str, children: Iterable[tuple[str, str | None]],
) -> bool:
    """Terminal parent envelope plus eligible-child existence, never summary code."""
    return parent_status in {JobStatus.FAILED, JobStatus.CANCELLED} and any(
        collection_child_retryable(status, code) for status, code in children
    )


# Output format choice, separate from the source variant / quality preference.
# Exact codec/bitrate presets are not defined here.
OutputProfile = Literal["original", "mp3", "wav", "m4a", "opus", "flac", "mp4"]
ORIGINAL_OUTPUT_PROFILE: OutputProfile = "original"
AUDIO_OUTPUT_PROFILES: frozenset[str] = frozenset(
    {"original", "mp3", "wav", "m4a", "opus", "flac"}
)
VIDEO_OUTPUT_PROFILES: frozenset[str] = frozenset({"original", "mp4"})


def output_profile_allowed(media_kind: str, profile: OutputProfile) -> bool:
    """Whether ``profile`` belongs to the audio or video output set."""
    allowed = AUDIO_OUTPUT_PROFILES if media_kind == "audio" else VIDEO_OUTPUT_PROFILES
    return profile in allowed


def require_runnable_output_profile(stored: str | None) -> None:
    """Temporary fail-closed gate until conversion runtime exists (M6.4b).

    Only NULL (legacy) and ``original`` run; every other stored value, known
    or not, fails with INVALID_PROFILE before any analysis or media I/O.
    """
    if stored is not None and stored != ORIGINAL_OUTPUT_PROFILE:
        raise JobError(
            JobErrorCode.INVALID_PROFILE,
            detail=f"output profile {stored!r} is not runnable yet",
        )


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


class CollectionProfile(BaseModel):
    """Native source-aware preference, not a conversion or quality guarantee."""

    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["original_best_available"] = "original_best_available"
    max_height: int | None = Field(default=None, gt=0)


class JobItemDto(BaseModel):
    id: str
    item_id: str
    source_order: int
    source_id: str | None
    source_url: str
    title: str | None
    playlist_index: int | None
    track_number: int | None
    disc_number: int | None
    status: JobStatus
    progress: JobProgress
    final_path: str | None
    error: JobErrorBody | None


class JobDto(BaseModel):
    id: str
    analysis_id: str
    variant_id: str
    status: JobStatus
    title: str
    source_url: str
    destination: str
    job_type: Literal["single", "collection"] = "single"
    collection_mode: Literal["audio", "video"] | None = None
    collection_profile: CollectionProfile | None = None
    output_profile: OutputProfile = ORIGINAL_OUTPUT_PROFILE
    # Single migration counters are structural defaults, not runtime facts.
    total_items: int | None = None
    completed_items: int | None = None
    failed_items: int | None = None
    items: list[JobItemDto] | None = None
    progress: JobProgress = Field(default_factory=JobProgress)
    final_path: str | None = None
    error: JobErrorBody | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
