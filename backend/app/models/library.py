from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LibraryItem(Base):
    """A logical downloaded media item (ARCHITECTURE.md §23, §37).

    The Library is a database-backed catalog, not a directory listing.
    Fields without an authoritative source stay null until the milestone
    that derives them (music metadata: Milestone 6, playlists: Milestone 5).
    """

    __tablename__ = "library_items"
    __table_args__ = (
        Index("ix_library_items_job_id", "job_id"),
        Index("ix_library_items_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128))
    platform: Mapped[str | None] = mapped_column(String(32))
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    artist: Mapped[str | None] = mapped_column(String(512))
    album: Mapped[str | None] = mapped_column(String(512))
    album_artist: Mapped[str | None] = mapped_column(String(512))
    track_number: Mapped[int | None] = mapped_column(Integer)
    disc_number: Mapped[int | None] = mapped_column(Integer)
    year: Mapped[int | None] = mapped_column(Integer)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024))
    format: Mapped[str | None] = mapped_column(String(32))
    codec: Mapped[str | None] = mapped_column(String(64))
    bitrate: Mapped[float | None] = mapped_column(Float)
    resolution: Mapped[str | None] = mapped_column(String(32))
    duration: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MediaFile(Base):
    """One physical copy of a library item (ARCHITECTURE.md §23, §37).

    ``path`` is the storage-controlled path for the copy (relative to the
    destination root for the App Library), never a client-supplied path.

    The foreign key cascades on database deletes so removing a Library
    catalog record (PRODUCT_SPEC.md §21 "Remove from Library") cleans up
    its catalog rows without ever touching the physical file.
    """

    __tablename__ = "media_files"
    __table_args__ = (Index("ix_media_files_library_item_id", "library_item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    library_item_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("library_items.id", ondelete="CASCADE"), nullable=False
    )
    storage_type: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    display_path: Mapped[str | None] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_optional: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
