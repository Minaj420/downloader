"""analysis sessions

Creates the analysis_sessions table used by URL analysis (Milestone 1).
Sessions persist normalized results so later milestones can create download
jobs from a stored analysis.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_sessions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("platform_hint", sa.String(length=32), nullable=True),
        sa.Column("detected_platform", sa.String(length=32), nullable=True),
        sa.Column("extractor", sa.String(length=128), nullable=True),
        sa.Column("media_type", sa.String(length=16), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("normalized_result_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analysis_sessions_created_at",
        "analysis_sessions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_sessions_created_at", table_name="analysis_sessions")
    op.drop_table("analysis_sessions")
