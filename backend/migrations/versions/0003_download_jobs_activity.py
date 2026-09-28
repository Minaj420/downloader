"""download jobs and activity events

Creates the download_jobs and activity_events tables for the first download
milestone: persistent queue state plus structured activity history for the
UI and SSE replay.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "download_jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("analysis_id", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("variant_id", sa.String(length=128), nullable=False),
        sa.Column("destination", sa.String(length=32), nullable=False),
        sa.Column("conflict_policy", sa.String(length=16), nullable=False),
        sa.Column("keep_app_copy", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress_percent", sa.Float(), nullable=False),
        sa.Column("downloaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("speed_bytes_per_second", sa.BigInteger(), nullable=True),
        sa.Column("eta_seconds", sa.Integer(), nullable=True),
        sa.Column("staging_path", sa.String(length=1024), nullable=True),
        sa.Column("final_path", sa.String(length=1024), nullable=True),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_download_jobs_status_created", "download_jobs", ["status", "created_at"]
    )
    op.create_index(
        "ix_download_jobs_owner_created", "download_jobs", ["owner_id", "created_at"]
    )

    op.create_table(
        "activity_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=True),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("message", sa.String(length=1024), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_activity_events_job_id", "activity_events", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_activity_events_job_id", table_name="activity_events")
    op.drop_table("activity_events")
    op.drop_index("ix_download_jobs_owner_created", table_name="download_jobs")
    op.drop_index("ix_download_jobs_status_created", table_name="download_jobs")
    op.drop_table("download_jobs")
