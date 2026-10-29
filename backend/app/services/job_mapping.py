"""Mapping from ORM job rows to API-facing DTOs."""

from datetime import UTC, datetime
from typing import Literal, cast

from app.domain.jobs import (
    ORIGINAL_OUTPUT_PROFILE,
    CollectionProfile,
    JobDto,
    JobErrorBody,
    JobItemDto,
    JobProgress,
    JobStatus,
    OutputProfile,
    collection_child_retryable,
    collection_retryable,
    job_error_code_retryable,
)
from app.models.jobs import DownloadJob, JobItem


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def job_to_dto(record: DownloadJob, items: list[JobItem] | None = None) -> JobDto:
    retryable = job_error_code_retryable(record.error_code or "")
    if record.job_type == "collection":
        facts = ([(item.status, item.error_code) for item in items] if items is not None
                 else getattr(record, "_collection_retry_children", None))
        if facts is None and record.status in {"failed", "cancelled"} and record.error_code:
            raise ValueError("Collection snapshots require preloaded child retry facts")
        retryable = collection_retryable(record.status, facts or [])
    return JobDto(
        id=record.id,
        analysis_id=record.analysis_id,
        variant_id=record.variant_id,
        status=JobStatus(record.status),
        title=record.title,
        source_url=record.source_url,
        destination=record.destination,
        job_type=cast(Literal["single", "collection"], record.job_type),
        collection_mode=cast(Literal["audio", "video"] | None, record.collection_mode),
        collection_profile=(
            CollectionProfile.model_validate_json(record.collection_profile_json)
            if record.collection_profile_json else CollectionProfile()
        ) if record.job_type == "collection" else None,
        # Only NULL means legacy Original; any other stored value is validated
        # as-is, so corrupt values fail instead of silently reading Original.
        output_profile=cast(
            OutputProfile,
            ORIGINAL_OUTPUT_PROFILE if record.output_profile is None else record.output_profile,
        ),
        total_items=record.total_items if record.job_type == "collection" else None,
        completed_items=record.completed_items if record.job_type == "collection" else None,
        failed_items=record.failed_items if record.job_type == "collection" else None,
        items=[item_to_dto(item) for item in items] if items is not None else None,
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
            retryable=retryable,
        )
        if record.error_code
        else None,
        created_at=_as_utc(record.created_at),
        updated_at=_as_utc(record.updated_at),
        started_at=_as_utc(record.started_at) if record.started_at else None,
        completed_at=_as_utc(record.completed_at) if record.completed_at else None,
    )


def item_to_dto(record: JobItem) -> JobItemDto:
    return JobItemDto(
        id=record.id,
        item_id=record.item_id,
        source_order=record.source_order,
        source_id=record.source_id,
        source_url=record.source_url,
        title=record.title,
        playlist_index=record.playlist_index,
        track_number=record.track_number,
        disc_number=record.disc_number,
        status=JobStatus(record.status),
        progress=JobProgress(
            percent=record.progress_percent,
            downloaded_bytes=record.downloaded_bytes,
            total_bytes=record.total_bytes,
            speed_bytes_per_second=record.speed_bytes_per_second,
            eta_seconds=record.eta_seconds,
        ),
        # Same read-only path boundary as the existing parent API. No file operations.
        final_path=record.final_path,
        error=JobErrorBody(
            code=record.error_code,
            message=record.error_message or "",
            retryable=collection_child_retryable(record.status, record.error_code),
        ) if record.error_code else None,
    )
