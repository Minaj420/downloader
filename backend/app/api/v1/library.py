from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from app.api.dependencies import get_actor
from app.domain.actors import Actor
from app.domain.library import (
    DeleteMediaFileOutcome,
    LibraryErrorEnvelope,
    LibraryErrorResponse,
    LibraryItemDto,
    LibraryListResponse,
)
from app.services.library_service import LibraryService

router = APIRouter(tags=["library"])


def _not_found_response() -> JSONResponse:
    """Same shape for unknown and foreign-owner items so the catalog
    never leaks another owner's item existence (jobs endpoint pattern)."""
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "library_item_not_found",
                "message": "This library item does not exist.",
                "retryable": False,
                "item_id": None,
            }
        },
    )


def _outcome_error_response(
    status_code: int, code: str, message: str, retryable: bool
) -> JSONResponse:
    """Stable typed body for media-file deletion outcomes (§43 style)."""
    body = LibraryErrorResponse(
        error=LibraryErrorEnvelope(
            code=code, message=message, retryable=retryable
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


def _media_file_not_found_response() -> JSONResponse:
    """Same shape for unknown copies and copies bound to another item,
    mirroring the item-level 404 (no existence leak)."""
    return _outcome_error_response(
        404,
        "media_file_not_found",
        "This media file does not exist for this library item.",
        retryable=False,
    )


def _item_not_found_response() -> JSONResponse:
    """Item-level 404 on the media-file endpoint: same code/message as
    the other library endpoints, carried in the typed envelope."""
    return _outcome_error_response(
        404,
        "library_item_not_found",
        "This library item does not exist.",
        retryable=False,
    )


# Blocked/failed deletions map to stable, user-showable bodies. Every
# entry except CATALOG_COMMIT_FAILED guarantees no database and no
# filesystem mutation happened; CATALOG_COMMIT_FAILED explicitly says
# the file may be gone so the body never misreports the data state.
_DELETE_ERROR_BODIES: dict[DeleteMediaFileOutcome, tuple[int, str, str, bool]] = {
    DeleteMediaFileOutcome.SHARED_PATH: (
        409,
        "media_file_shared_path",
        "This file is referenced by another library entry, so it was not deleted.",
        False,
    ),
    DeleteMediaFileOutcome.REFERENCE_UNVERIFIABLE: (
        409,
        "media_file_reference_unverifiable",
        "Another library entry uses a storage location that cannot be "
        "verified (for example unconfigured storage or an unsafe path); "
        "the file cannot be deleted until it can be verified as "
        "unreferenced.",
        False,
    ),
    DeleteMediaFileOutcome.FILE_MISSING: (
        409,
        "media_file_missing",
        "The catalogued file is missing from storage, so its record was "
        "left unchanged.",
        False,
    ),
    DeleteMediaFileOutcome.INVALID_TARGET: (
        409,
        "media_file_invalid_target",
        "The catalogued file location is invalid (unconfigured storage, "
        "outside the storage root, or not a regular file), so nothing was "
        "deleted.",
        False,
    ),
    DeleteMediaFileOutcome.TARGET_CHANGED: (
        409,
        "media_file_target_changed",
        "The file changed while the deletion was in progress; nothing was "
        "deleted. Try again.",
        True,
    ),
    DeleteMediaFileOutcome.STORAGE_ERROR: (
        500,
        "storage_delete_failed",
        "The file could not be deleted from storage; the library was left "
        "unchanged.",
        True,
    ),
    DeleteMediaFileOutcome.DATABASE_BUSY: (
        503,
        "database_busy",
        "The library database is busy; nothing was changed. Try again "
        "shortly.",
        True,
    ),
    DeleteMediaFileOutcome.DATABASE_ERROR: (
        500,
        "library_database_failed",
        "The library database failed; nothing was changed. It is safe "
        "to try again.",
        True,
    ),
    DeleteMediaFileOutcome.CATALOG_COMMIT_FAILED: (
        500,
        "media_file_catalog_commit_failed",
        "The file was deleted, but updating the library catalog failed; "
        "the library entry may be out of sync with storage.",
        False,
    ),
}


@router.get("/library", response_model=LibraryListResponse)
async def list_library(
    http_request: Request,
    actor: Actor = Depends(get_actor),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LibraryListResponse:
    """One owner-scoped, newest-first page of the Library catalog.

    Read-only: this endpoint never touches the filesystem or writes to
    the database. Invalid ``limit``/``offset`` fail FastAPI validation
    and surface the generic 422 error shape.
    """
    service: LibraryService = http_request.app.state.library_service
    return await service.list_items(actor, limit=limit, offset=offset)


@router.get("/library/{item_id}", response_model=LibraryItemDto)
async def get_library_item(
    item_id: str, http_request: Request, actor: Actor = Depends(get_actor)
) -> LibraryItemDto | JSONResponse:
    """Full catalog detail for one library item, owner-scoped.

    Unknown and foreign-owner ids return the same 404; physical copies
    come from the catalog, not the filesystem.
    """
    service: LibraryService = http_request.app.state.library_service
    item = await service.get_item(actor, item_id)
    if item is None:
        return _not_found_response()
    return item


@router.delete("/library/{item_id}", status_code=204)
async def remove_library_item(
    item_id: str, http_request: Request, actor: Actor = Depends(get_actor)
) -> Response:
    """Remove one library item's catalog records, owner-scoped (record only).

    Deletes the ``library_items`` row and, through the FK cascade, its
    bound ``media_files`` rows in a single transaction. The physical
    media files stay untouched on disk (PRODUCT_SPEC.md §21,
    ARCHITECTURE.md §38); deletion is by library id only, never by
    filesystem path. Unknown and foreign-owner ids return the same 404
    as the detail endpoint.
    """
    service: LibraryService = http_request.app.state.library_service
    removed = await service.remove_item(actor, item_id)
    if not removed:
        return _not_found_response()
    return Response(status_code=204)


@router.delete(
    "/library/{item_id}/media-files/{media_file_id}",
    status_code=204,
    responses={
        404: {
            "model": LibraryErrorResponse,
            "description": "Unknown or foreign item, or unknown/mismatched copy.",
        },
        409: {
            "model": LibraryErrorResponse,
            "description": "Deletion blocked: shared or unverifiable reference, "
            "missing/invalid/changed target.",
        },
        500: {
            "model": LibraryErrorResponse,
            "description": "Storage failure (nothing changed), library "
            "database failure (nothing changed, safe to retry), or catalog "
            "commit failure after deletion (entry may be out of sync).",
        },
        503: {
            "model": LibraryErrorResponse,
            "description": "Library database busy; nothing changed.",
        },
    },
)
async def delete_library_media_file(
    item_id: str,
    media_file_id: int,
    http_request: Request,
    actor: Actor = Depends(get_actor),
) -> Response:
    """Delete one physical media file and its catalog row (Delete File).

    Owner- and copy-scoped by catalog ids only — the client never
    supplies a filesystem path; the backend resolves the physical file
    through the catalog's storage mapping and the configured storage
    roots (PRODUCT_SPEC.md §21, ARCHITECTURE.md §37–38). On success the
    physical file and the selected copy row are deleted, and the parent
    item goes too when this was its last copy (204, empty body).

    A shared physical path — another catalog row resolving to the same
    file through any storage mapping — blocks the deletion with a
    non-retryable 409 and mutates neither the database nor the file.
    Unknown/foreign items and unknown or mismatched copies share the
    same 404s as the other library endpoints. Confirmation is a
    frontend concern and is deliberately not modelled here.
    """
    service: LibraryService = http_request.app.state.library_service
    outcome = await service.delete_media_file(actor, item_id, media_file_id)
    if outcome is DeleteMediaFileOutcome.DELETED:
        return Response(status_code=204)
    if outcome is DeleteMediaFileOutcome.ITEM_NOT_FOUND:
        return _item_not_found_response()
    if outcome is DeleteMediaFileOutcome.MEDIA_FILE_NOT_FOUND:
        return _media_file_not_found_response()
    status, code, message, retryable = _DELETE_ERROR_BODIES[outcome]
    return _outcome_error_response(status, code, message, retryable)
