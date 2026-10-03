"""Owner-scoped Library catalog access (Milestone 3).

Reads go only to the database catalog (ARCHITECTURE.md §37): no
filesystem access, and list pages load their items and all physical
copies with a bounded number of queries (item page + one IN media
query) so pages never degrade into per-item lookups.

Mutations: record-only removal (PRODUCT_SPEC.md §21 "Remove from
Library") deletes catalog rows in one transaction and never touches
physical media files; "Delete File" deletes one physical copy — the
catalog rows for that copy (and its parent item when it was the last
copy) plus the physical file through the storage boundary, with a
shared-physical-path block that mutates nothing (§21, §38). Catalog
writes that insert ``media_files`` rows go through
``stage_media_catalog_reference`` so a concurrent Delete File can never
leave a dangling committed reference (§38, §51).
"""

import asyncio
import sqlite3
from contextlib import suppress

from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.actors import Actor
from app.domain.library import (
    DeleteMediaFileOutcome,
    LibraryItemDto,
    LibraryListResponse,
)
from app.models.library import LibraryItem, MediaFile
from app.repositories import library as library_repo
from app.services.library_mapping import (
    library_item_detail_dto,
    library_item_list_dto,
)
from app.services.storage_service import (
    MediaFileMissingError,
    MediaFileNotRegularError,
    MediaFileTargetChangedError,
    PreparedMediaFile,
    StorageDeleteFailedError,
    StoragePathError,
    StorageService,
    StorageTargetError,
)


async def _run_to_completion[T](operation: asyncio.Task[T]) -> T:
    """Await a started filesystem operation to completion (§51).

    Mirrors ``StorageService.place_final``: once an irreversible
    filesystem step (target preparation, unlink) has started, its
    result — or its typed failure — must be observed so the database
    commit/rollback decision stays deterministic even if the calling
    coroutine is cancelled; the worker thread must never finish late
    and silently unlink a file whose transaction already rolled back.
    """
    while True:
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()


async def _rollback_safely(session: AsyncSession) -> None:
    """Roll back without letting cleanup failures shadow the outcome.

    A SQLAlchemyError raised by the rollback itself is suppressed (the
    session close still undoes the transaction); programming errors
    propagate instead of being blindly swallowed.
    """
    with suppress(SQLAlchemyError):
        await session.rollback()


def _operational_error_outcome(error: OperationalError) -> DeleteMediaFileOutcome:
    """Only native SQLite BUSY/LOCKED failures are lock contention.

    The primary byte also covers extended result codes. When a native
    exception lacks a code, accept only SQLite's exact lock messages,
    never substrings or messages from another provider.
    """
    original = error.orig
    if isinstance(original, sqlite3.OperationalError):
        code = getattr(original, "sqlite_errorcode", None)
        if code is not None:
            busy = isinstance(code, int) and (code & 0xFF) in {
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
            }
        else:
            busy = str(original) in {
                "database is locked",
                "database table is locked",
                "database schema is locked",
            }
        if busy:
            return DeleteMediaFileOutcome.DATABASE_BUSY
    return DeleteMediaFileOutcome.DATABASE_ERROR


async def stage_media_catalog_reference(
    session: AsyncSession,
    storage: StorageService,
    item: LibraryItem,
    media_file: MediaFile,
    *,
    placement_identity: tuple[int, int],
) -> None:
    """Stage a finalized copy's catalog rows under the safe write protocol.

    This is the one production protocol for committing ``media_files``
    rows (the M3-LIB-6-R1 fix, ARCHITECTURE.md §38, §51): the caller's
    transaction must already hold the SQLite write lock — for job
    completion that is the staged completion update — because a Delete
    File only unlinks inside its own write lock. This then stages the
    item row, stages the physical-copy row, and re-verifies the
    physical target through the storage boundary before any commit.

    A concurrent Delete File that won the lock and removed (or
    replaced) the file while this writer was waiting therefore surfaces
    here as the storage boundary's typed error, and the caller's whole
    transaction — the completion transition included — rolls back
    instead of committing a dangling catalog row or a completed job
    whose file is gone. Verification inside the held lock is also what
    makes it sound: no delete can unlink between the re-verification
    and this transaction's commit. Never commits; touches the
    filesystem only through the storage boundary.
    """
    await library_repo.add_library_item(session, item)
    await library_repo.add_media_file(session, media_file)
    await asyncio.to_thread(
        storage.verify_media_file,
        media_file.storage_type,
        media_file.path,
        placement_identity,
    )


class LibraryService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage: StorageService,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage

    async def list_items(
        self, actor: Actor, *, limit: int, offset: int
    ) -> LibraryListResponse:
        """One newest-first page of the owner's library items.

        Fetches ``limit + 1`` rows so ``has_more`` is decided without a
        total count, then loads every page item's physical copies with a
        single IN query.
        """
        async with self._session_factory() as session:
            rows = await library_repo.list_library_items(
                session, actor.id, limit=limit + 1, offset=offset
            )
            has_more = len(rows) > limit
            page = rows[:limit]
            files_by_item = await _load_media_files(
                session, [record.id for record in page]
            )
        return LibraryListResponse(
            items=[
                library_item_list_dto(
                    record, files_by_item.get(record.id, [])
                )
                for record in page
            ],
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

    async def get_item(self, actor: Actor, item_id: str) -> LibraryItemDto | None:
        """Full detail for one owner-scoped item, or ``None`` for 404.

        Unknown and foreign-owner items are indistinguishable here; the
        API layer maps both to the same not-found response.
        """
        async with self._session_factory() as session:
            record = await library_repo.get_library_item(
                session, item_id, owner_id=actor.id
            )
            if record is None:
                return None
            media_files = await library_repo.list_media_files_for_items(
                session, [record.id]
            )
        return library_item_detail_dto(record, media_files)

    async def remove_item(self, actor: Actor, item_id: str) -> bool:
        """Remove one owner-scoped catalog record (record-only removal).

        Returns ``True`` when the ``library_items`` row was deleted; its
        bound ``media_files`` rows go with the FK cascade in the same
        committed transaction. Unknown and foreign-owner ids match no
        row and return ``False`` (the API layer maps both to the same
        404). Nothing but catalog rows is ever removed — the physical
        media files stay on disk (PRODUCT_SPEC.md §21,
        ARCHITECTURE.md §38).
        """
        async with self._session_factory() as session:
            deleted = await library_repo.delete_library_item(
                session, item_id, owner_id=actor.id
            )
            if not deleted:
                # Nothing matched: drop the no-op transaction explicitly
                # so failure paths never leave a half-applied catalog
                # change behind.
                await session.rollback()
                return False
            await session.commit()
        return True

    async def delete_media_file(
        self, actor: Actor, item_id: str, media_file_id: int
    ) -> DeleteMediaFileOutcome:
        """Delete one physical copy: its file, its row, and an empty parent.

        Owner-scoped and copy-scoped by catalog ids only — the client
        never supplies a filesystem path (ARCHITECTURE.md §37–38).
        Outcomes other than ``DELETED``/``CATALOG_COMMIT_FAILED``
        guarantee that neither the database nor the filesystem was
        mutated.

        Ordering (the closest to atomic the DB/filesystem split allows,
        ARCHITECTURE.md §51): the target copy's row delete is staged
        immediately after owner/item/copy validation — taking the
        SQLite write lock — and the catalog-wide reference scan runs
        inside that same transaction, so a concurrent same-path
        catalog write either serializes before the scan (visible →
        blocked) or cannot commit until this transaction ends. The
        physical target is prepared once (descriptor-anchored, shared
        identity and unlink on the same handle) and deleted only after
        the scan passes; storage failures roll the catalog back, and
        the rare commit failure after a successful unlink returns the
        distinct ``CATALOG_COMMIT_FAILED`` outcome instead of a silent
        success.
        """
        async with self._session_factory() as session:
            try:
                if (
                    await library_repo.get_library_item(
                        session, item_id, owner_id=actor.id
                    )
                    is None
                ):
                    return DeleteMediaFileOutcome.ITEM_NOT_FOUND
                target = await library_repo.get_media_file_for_item(
                    session, item_id, media_file_id
                )
                if target is None:
                    return DeleteMediaFileOutcome.MEDIA_FILE_NOT_FOUND
            except OperationalError as error:
                await _rollback_safely(session)
                return _operational_error_outcome(error)
            except SQLAlchemyError:
                await _rollback_safely(session)
                return DeleteMediaFileOutcome.DATABASE_ERROR

            # Take the write lock before the reference scan: a concurrent
            # same-path write either lands before this transaction (the
            # scan sees it and blocks) or waits for its commit.
            try:
                staged = await library_repo.delete_media_file_row(
                    session, media_file_id
                )
            except OperationalError as error:
                await _rollback_safely(session)
                return _operational_error_outcome(error)
            except SQLAlchemyError:
                await _rollback_safely(session)
                return DeleteMediaFileOutcome.DATABASE_ERROR
            if not staged:
                await _rollback_safely(session)
                return DeleteMediaFileOutcome.MEDIA_FILE_NOT_FOUND

            prepared: PreparedMediaFile | None = None
            try:
                try:
                    prepared = await _run_to_completion(
                        asyncio.create_task(
                            asyncio.to_thread(
                                self._storage.prepare_media_file,
                                target.storage_type,
                                target.path,
                            )
                        )
                    )
                except (StorageTargetError, StoragePathError, MediaFileNotRegularError):
                    await _rollback_safely(session)
                    return DeleteMediaFileOutcome.INVALID_TARGET
                except MediaFileMissingError:
                    await _rollback_safely(session)
                    return DeleteMediaFileOutcome.FILE_MISSING
                except StorageDeleteFailedError:
                    await _rollback_safely(session)
                    return DeleteMediaFileOutcome.STORAGE_ERROR
                assert prepared is not None

                # Catalog-wide scan (physical files are shared across items
                # and owners): a row that cannot be resolved — unconfigured
                # or unavailable storage root, symlinked or escaping path —
                # is treated as an unverifiable potential reference, never
                # skipped.
                try:
                    others = await library_repo.list_media_files_excluding(
                        session, media_file_id
                    )
                except OperationalError as error:
                    await _rollback_safely(session)
                    return _operational_error_outcome(error)
                except SQLAlchemyError:
                    await _rollback_safely(session)
                    return DeleteMediaFileOutcome.DATABASE_ERROR
                resolved_others = await asyncio.to_thread(
                    self._storage.try_resolve_media_files,
                    [(row.storage_type, row.path) for row in others],
                )
                for _row, resolved_row in zip(others, resolved_others, strict=True):
                    if resolved_row is None:
                        await _rollback_safely(session)
                        return DeleteMediaFileOutcome.REFERENCE_UNVERIFIABLE
                    if (
                        resolved_row.resolved_path == prepared.resolved_path
                        or resolved_row.identity == prepared.identity
                    ):
                        await _rollback_safely(session)
                        return DeleteMediaFileOutcome.SHARED_PATH

                # The last copy takes its parent item with it so no empty
                # logical item is left behind; the owner scope lives in
                # the delete itself.
                item_has_other_copies = any(
                    row.library_item_id == item_id for row in others
                )
                if not item_has_other_copies:
                    try:
                        parent_removed = await library_repo.delete_library_item(
                            session, item_id, owner_id=actor.id
                        )
                    except OperationalError as error:
                        await _rollback_safely(session)
                        return _operational_error_outcome(error)
                    except SQLAlchemyError:
                        await _rollback_safely(session)
                        return DeleteMediaFileOutcome.DATABASE_ERROR
                    if not parent_removed:
                        # Invariant: the row was read in this transaction and
                        # no other writer can hold the lock — refuse loudly
                        # rather than guess (programming error, propagate).
                        raise RuntimeError(
                            "library item row disappeared inside the "
                            "deletion transaction"
                        )

                try:
                    await _run_to_completion(
                        asyncio.create_task(asyncio.to_thread(prepared.delete))
                    )
                except MediaFileMissingError:
                    await _rollback_safely(session)
                    return DeleteMediaFileOutcome.FILE_MISSING
                except MediaFileTargetChangedError:
                    await _rollback_safely(session)
                    return DeleteMediaFileOutcome.TARGET_CHANGED
                except MediaFileNotRegularError:
                    await _rollback_safely(session)
                    return DeleteMediaFileOutcome.INVALID_TARGET
                except StorageDeleteFailedError:
                    await _rollback_safely(session)
                    return DeleteMediaFileOutcome.STORAGE_ERROR

                try:
                    await _run_to_completion(
                        asyncio.create_task(session.commit())
                    )
                except SQLAlchemyError:
                    # The unlink already happened; never claim "unchanged".
                    # Any database-level commit failure (lock, disk, driver)
                    # is the same catalog-consistency outcome; coding errors
                    # are not SQLAlchemyError and still propagate.
                    await _rollback_safely(session)
                    return DeleteMediaFileOutcome.CATALOG_COMMIT_FAILED
                return DeleteMediaFileOutcome.DELETED
            finally:
                if prepared is not None:
                    prepared.close()


async def _load_media_files(
    session: AsyncSession, item_ids: list[str]
) -> dict[str, list[MediaFile]]:
    """Group one ordered media-files query by library item id."""
    media_files = await library_repo.list_media_files_for_items(session, item_ids)
    files_by_item: dict[str, list[MediaFile]] = {}
    for media in media_files:
        files_by_item.setdefault(media.library_item_id, []).append(media)
    return files_by_item
