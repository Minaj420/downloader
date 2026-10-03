"""Mapping from ORM library catalog rows to API-facing DTOs.

The ORM columns are plain strings; the mapping translates them into the
domain's closed vocabularies, so an unexpected catalog value fails here
instead of leaking into API contracts.
"""

from datetime import UTC, datetime

from app.domain.library import (
    LibraryItemDto,
    LibraryListItemDto,
    LibraryMediaFileDto,
    LibraryMediaType,
    MediaStorageType,
)
from app.models.library import LibraryItem, MediaFile


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def media_file_to_dto(record: MediaFile) -> LibraryMediaFileDto:
    return LibraryMediaFileDto(
        id=record.id,
        storage_type=MediaStorageType(record.storage_type),
        path=record.path,
        display_path=record.display_path,
        size_bytes=record.size_bytes,
        created_at=_as_utc(record.created_at),
    )


def library_item_list_dto(
    record: LibraryItem, media_files: list[MediaFile]
) -> LibraryListItemDto:
    return LibraryListItemDto(
        id=record.id,
        job_id=record.job_id,
        title=record.title,
        media_type=LibraryMediaType(record.media_type),
        format=record.format,
        created_at=_as_utc(record.created_at),
        media_files=[media_file_to_dto(media) for media in media_files],
    )


def library_item_detail_dto(
    record: LibraryItem, media_files: list[MediaFile]
) -> LibraryItemDto:
    return LibraryItemDto(
        id=record.id,
        job_id=record.job_id,
        title=record.title,
        media_type=LibraryMediaType(record.media_type),
        source_url=record.source_url,
        source_id=record.source_id,
        platform=record.platform,
        artist=record.artist,
        album=record.album,
        created_at=_as_utc(record.created_at),
        format=record.format,
        codec=record.codec,
        bitrate=record.bitrate,
        resolution=record.resolution,
        duration=record.duration,
        media_files=[media_file_to_dto(media) for media in media_files],
    )
