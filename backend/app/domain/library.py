"""Library catalog domain vocabulary (ARCHITECTURE.md §23, §37)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class MediaStorageType(StrEnum):
    """Physical destinations a media file copy can live in."""

    APP_LIBRARY = "app_library"
    PLEX_MUSIC = "plex_music"
    PLEX_VIDEO = "plex_video"


class LibraryMediaType(StrEnum):
    """Closed set of logical media kinds in the catalog."""

    AUDIO = "audio"
    VIDEO = "video"


class LibraryMediaFileDto(BaseModel):
    """One physical copy of a library item.

    ``path`` is the storage-controlled relative path from the catalog,
    never a client-supplied path (ARCHITECTURE.md §37, §49). The enums
    validate catalog values at the mapping boundary and still serialize
    as plain JSON strings.
    """

    id: int
    storage_type: MediaStorageType
    path: str
    display_path: str | None = None
    size_bytes: int
    created_at: datetime


class LibraryListItemDto(BaseModel):
    """Compact Library row (PRODUCT_SPEC.md §20 columns).

    Carries the physical copies so Name/Type/Size/Date/Format/Location/
    Path render from one page; one logical item may have several copies.
    """

    id: str
    job_id: str
    title: str
    media_type: LibraryMediaType
    format: str | None = None
    created_at: datetime
    media_files: list[LibraryMediaFileDto] = Field(default_factory=list)


class LibraryItemDto(BaseModel):
    """Full Library item detail (PRODUCT_SPEC.md §20).

    Only fields with an authoritative catalog source are present; music
    metadata stays nullable until Milestone 6 and artwork/playlist data
    is not invented (not in persistence yet).
    """

    id: str
    job_id: str
    title: str
    media_type: LibraryMediaType
    source_url: str
    source_id: str | None = None
    platform: str | None = None
    artist: str | None = None
    album: str | None = None
    created_at: datetime
    format: str | None = None
    codec: str | None = None
    bitrate: float | None = None
    resolution: str | None = None
    duration: float | None = None
    media_files: list[LibraryMediaFileDto] = Field(default_factory=list)


class LibraryListResponse(BaseModel):
    """One page of the owner's Library, newest first."""

    items: list[LibraryListItemDto] = Field(default_factory=list)
    limit: int
    offset: int
    has_more: bool


class DeleteMediaFileOutcome(StrEnum):
    """Typed results of "Delete File" for one physical copy.

    The service reports these; the API layer maps each to a stable
    error body (or 204). Blocked outcomes guarantee that neither the
    database nor the filesystem was mutated (PRODUCT_SPEC.md §21,
    ARCHITECTURE.md §38), except ``CATALOG_COMMIT_FAILED`` where the
    file is gone and the body must not claim otherwise.
    """

    DELETED = "deleted"
    ITEM_NOT_FOUND = "item_not_found"
    MEDIA_FILE_NOT_FOUND = "media_file_not_found"
    SHARED_PATH = "shared_path"
    REFERENCE_UNVERIFIABLE = "reference_unverifiable"
    FILE_MISSING = "file_missing"
    INVALID_TARGET = "invalid_target"
    TARGET_CHANGED = "target_changed"
    STORAGE_ERROR = "storage_error"
    DATABASE_BUSY = "database_busy"
    DATABASE_ERROR = "database_error"
    CATALOG_COMMIT_FAILED = "catalog_commit_failed"


class LibraryErrorEnvelope(BaseModel):
    """Stable library error fields (ARCHITECTURE.md §43 error model)."""

    code: str
    message: str
    retryable: bool
    item_id: str | None = None
    media_file_id: int | None = None


class LibraryErrorResponse(BaseModel):
    """Typed error body published in OpenAPI for library mutations."""

    error: LibraryErrorEnvelope
