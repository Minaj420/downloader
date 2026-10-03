from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library import LibraryItem, MediaFile


async def add_library_item(session: AsyncSession, record: LibraryItem) -> None:
    """Stage a library catalog item in the caller's transaction.

    Flushes so generated defaults are visible inside the transaction but
    leaves the commit to the caller, keeping the catalog rows atomic with
    the job's completion transition (ARCHITECTURE.md §51).
    """
    session.add(record)
    await session.flush()


async def add_media_file(session: AsyncSession, record: MediaFile) -> None:
    """Stage a physical media file row in the caller's transaction."""
    session.add(record)
    await session.flush()


async def list_library_items(
    session: AsyncSession,
    owner_id: str,
    *,
    limit: int,
    offset: int,
) -> list[LibraryItem]:
    """One owner-scoped page of library items, deterministic newest first.

    ``created_at DESC, id DESC`` keeps equal timestamps stable. Callers
    fetch ``limit + 1`` rows to compute ``has_more`` without a count query.
    """
    result = await session.execute(
        select(LibraryItem)
        .where(LibraryItem.owner_id == owner_id)
        .order_by(LibraryItem.created_at.desc(), LibraryItem.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_library_item(
    session: AsyncSession, item_id: str, owner_id: str
) -> LibraryItem | None:
    """Fetch one library item scoped to its owner.

    A foreign-owner item is indistinguishable from an unknown id so the
    catalog never leaks existence (same 404 at the API layer).
    """
    result = await session.execute(
        select(LibraryItem).where(
            LibraryItem.id == item_id,
            LibraryItem.owner_id == owner_id,
        )
    )
    return result.scalar_one_or_none()


async def delete_library_item(
    session: AsyncSession, item_id: str, owner_id: str
) -> bool:
    """Delete one library item scoped to its owner, in the caller's transaction.

    The owner scope lives inside the mutation itself: a foreign-owner id
    matches no row, exactly like an unknown id (same 404 at the API
    layer, no existence leak). Bound ``media_files`` rows are removed by
    the catalog-level FK cascade in the same statement/transaction; the
    physical files are never touched (PRODUCT_SPEC.md §21,
    ARCHITECTURE.md §38). The caller commits or rolls back.
    """
    result = await session.execute(
        delete(LibraryItem).where(
            LibraryItem.id == item_id,
            LibraryItem.owner_id == owner_id,
        )
    )
    return cast(CursorResult[Any], result).rowcount > 0


async def get_media_file_for_item(
    session: AsyncSession, item_id: str, media_file_id: int
) -> MediaFile | None:
    """Fetch one physical copy bound to a specific library item.

    The item scoping lives inside the lookup: a copy id that belongs
    to another item (or does not exist) matches no row, so wrong
    item/copy relations are indistinguishable from unknown ids at the
    API layer (same 404, no existence leak).
    """
    result = await session.execute(
        select(MediaFile).where(
            MediaFile.id == media_file_id,
            MediaFile.library_item_id == item_id,
        )
    )
    return result.scalar_one_or_none()


async def list_media_files_excluding(
    session: AsyncSession, exclude_id: int
) -> list[MediaFile]:
    """Every physical-copy row except one, in the caller's transaction.

    Used for shared-physical-path checks (ARCHITECTURE.md §38): the
    scan is catalog-wide, not owner-scoped, because a physical file on
    disk is shared by every catalog row that references it.
    """
    result = await session.execute(
        select(MediaFile).where(MediaFile.id != exclude_id)
    )
    return list(result.scalars().all())


async def delete_media_file_row(
    session: AsyncSession, media_file_id: int
) -> bool:
    """Delete one physical-copy row in the caller's transaction.

    Staging this delete early is what holds the SQLite write lock
    across the shared-reference check and the physical file deletion,
    serializing concurrent catalog writes out of the check→delete
    window. The caller commits or rolls back.
    """
    result = await session.execute(
        delete(MediaFile).where(MediaFile.id == media_file_id)
    )
    return cast(CursorResult[Any], result).rowcount > 0


async def list_media_files_for_items(
    session: AsyncSession, item_ids: Sequence[str]
) -> list[MediaFile]:
    """All physical copies for the given items in a single IN query.

    Ordered ``created_at ASC, id ASC`` so grouping preserves a stable
    copy order and list pages never trigger per-item queries (no N+1).
    """
    if not item_ids:
        return []
    result = await session.execute(
        select(MediaFile)
        .where(MediaFile.library_item_id.in_(item_ids))
        .order_by(MediaFile.created_at.asc(), MediaFile.id.asc())
    )
    return list(result.scalars().all())
