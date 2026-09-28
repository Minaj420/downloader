"""Mapping from ORM job rows to API-facing DTOs."""

from datetime import UTC, datetime

from app.domain.jobs import JobDto, JobErrorBody, JobProgress, JobStatus, job_error_code_retryable
from app.models.jobs import DownloadJob


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def job_to_dto(record: DownloadJob) -> JobDto:
    return JobDto(
        id=record.id,
        analysis_id=record.analysis_id,
        variant_id=record.variant_id,
        status=JobStatus(record.status),
        title=record.title,
        source_url=record.source_url,
        destination=record.destination,
        progress=JobProgress(
            percent=record.progress_percent,
            downloaded_bytes=record.downloaded_bytes,
            total_bytes=record.total_bytes,
            speed_bytes_per_second=record.speed_bytes_per_second,
            eta_seconds=record.eta_seconds,
        ),
        final_path=record.final_path,
        error=JobErrorBody(
            code=record.error_code or "internal_error",
            message=record.error_message or "",
            retryable=job_error_code_retryable(record.error_code or ""),
        )
        if record.error_code
        else None,
        created_at=_as_utc(record.created_at),
        updated_at=_as_utc(record.updated_at),
        started_at=_as_utc(record.started_at) if record.started_at else None,
        completed_at=_as_utc(record.completed_at) if record.completed_at else None,
    )
