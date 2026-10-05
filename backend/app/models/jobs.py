from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DownloadJob(Base):
    """A persistent download job (ARCHITECTURE.md §15–18, §23).

    The database is the source of truth for queue state; the in-process
    QueueManager only accelerates execution.
    """

    __tablename__ = "download_jobs"
    __table_args__ = (
        Index("ix_download_jobs_status_created", "status", "created_at"),
        Index("ix_download_jobs_owner_created", "owner_id", "created_at"),
        CheckConstraint("job_type IN ('single', 'collection')", name="ck_download_jobs_type"),
        CheckConstraint(
            "collection_mode IS NULL OR collection_mode IN ('audio', 'video')",
            name="ck_download_jobs_collection_mode",
        ),
        CheckConstraint(
            "total_items >= 0 AND completed_items >= 0 AND failed_items >= 0 "
            "AND completed_items + failed_items <= total_items",
            name="ck_download_jobs_item_counters",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    job_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="single", server_default="single"
    )
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    completed_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    collection_mode: Mapped[str | None] = mapped_column(String(16))
    # NULL is Original / Best Available; non-null preferences are opaque here.
    # Supported profiles and source-aware resolution belong to later services.
    collection_profile_json: Mapped[str | None] = mapped_column(Text)
    # Internal durable request for this attempt; retained through terminal settlement.
    collection_cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    variant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    destination: Mapped[str] = mapped_column(String(32), nullable=False)
    conflict_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    keep_app_copy: Mapped[bool] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    progress_percent: Mapped[float] = mapped_column(Float, nullable=False)
    downloaded_bytes: Mapped[int | None] = mapped_column(BigInteger)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    speed_bytes_per_second: Mapped[int | None] = mapped_column(BigInteger)
    eta_seconds: Mapped[int | None] = mapped_column(Integer)
    staging_path: Mapped[str | None] = mapped_column(String(1024))
    final_path: Mapped[str | None] = mapped_column(String(1024))
    error_code: Mapped[str | None] = mapped_column(String(48))
    error_message: Mapped[str | None] = mapped_column(String(512))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobItem(Base):
    """Selected snapshot entry; ownership is inherited exclusively from its parent."""

    __tablename__ = "job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "item_id", name="uq_job_items_job_item"),
        UniqueConstraint("job_id", "source_order", name="uq_job_items_job_order"),
        Index("ix_job_items_job_source_order", "job_id", "source_order"),
        CheckConstraint("source_order >= 1", name="ck_job_items_source_order"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("download_jobs.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128))
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    playlist_index: Mapped[int | None] = mapped_column(Integer)
    track_number: Mapped[int | None] = mapped_column(Integer)
    disc_number: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    progress_percent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0"
    )
    downloaded_bytes: Mapped[int | None] = mapped_column(BigInteger)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    speed_bytes_per_second: Mapped[int | None] = mapped_column(BigInteger)
    eta_seconds: Mapped[int | None] = mapped_column(Integer)
    staging_path: Mapped[str | None] = mapped_column(String(1024))
    final_path: Mapped[str | None] = mapped_column(String(1024))
    error_code: Mapped[str | None] = mapped_column(String(48))
    error_message: Mapped[str | None] = mapped_column(String(512))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActivityEvent(Base):
    """Structured activity history backing the UI and SSE replay."""

    __tablename__ = "activity_events"
    __table_args__ = (Index("ix_activity_events_job_id", "job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(String(32))
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(String(1024), nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
