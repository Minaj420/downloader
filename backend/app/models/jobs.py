from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String, Text
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
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
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
