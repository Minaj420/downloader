from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AnalysisSession(Base):
    """A persisted URL analysis result.

    Kept reusable for Milestone 2: a download job can reference the session id
    and re-read the normalized result instead of re-analyzing the URL.
    """

    __tablename__ = "analysis_sessions"
    __table_args__ = (Index("ix_analysis_sessions_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    platform_hint: Mapped[str | None] = mapped_column(String(32))
    detected_platform: Mapped[str | None] = mapped_column(String(32))
    extractor: Mapped[str | None] = mapped_column(String(128))
    media_type: Mapped[str | None] = mapped_column(String(16))
    title: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(48))
    error_message: Mapped[str | None] = mapped_column(String(512))
    normalized_result_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
