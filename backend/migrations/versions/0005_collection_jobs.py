"""Persist collection parents, preferences and selected children.

Revision ID: 0005
Revises: 0004

Legacy rows remain single jobs. Counters are structural defaults (1/0/0),
not reconstructed lifecycle facts: the existing single runner does not maintain
collection counters. No status, output, child or Library row is rewritten.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("download_jobs") as batch:
        batch.add_column(
            sa.Column("job_type", sa.String(16), nullable=False, server_default="single")
        )
        batch.add_column(sa.Column("total_items", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(
            sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("collection_mode", sa.String(16), nullable=True))
        batch.add_column(sa.Column("collection_profile_json", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_download_jobs_type", "job_type IN ('single', 'collection')"
        )
        batch.create_check_constraint(
            "ck_download_jobs_collection_mode",
            "collection_mode IS NULL OR collection_mode IN ('audio', 'video')",
        )
        batch.create_check_constraint(
            "ck_download_jobs_item_counters",
            "total_items >= 0 AND completed_items >= 0 AND failed_items >= 0 "
            "AND completed_items + failed_items <= total_items",
        )
    op.create_table(
        "job_items",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(32), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("source_order", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=True),
        sa.Column("source_url", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("playlist_index", sa.Integer(), nullable=True),
        sa.Column("track_number", sa.Integer(), nullable=True),
        sa.Column("disc_number", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("progress_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("downloaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("speed_bytes_per_second", sa.BigInteger(), nullable=True),
        sa.Column("eta_seconds", sa.Integer(), nullable=True),
        sa.Column("staging_path", sa.String(1024), nullable=True),
        sa.Column("final_path", sa.String(1024), nullable=True),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["download_jobs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", "item_id", name="uq_job_items_job_item"),
        sa.UniqueConstraint("job_id", "source_order", name="uq_job_items_job_order"),
        sa.CheckConstraint("source_order >= 1", name="ck_job_items_source_order"),
    )
    op.create_index("ix_job_items_job_source_order", "job_items", ["job_id", "source_order"])


def downgrade() -> None:
    op.drop_index("ix_job_items_job_source_order", table_name="job_items")
    op.drop_table("job_items")
    with op.batch_alter_table("download_jobs") as batch:
        batch.drop_constraint("ck_download_jobs_item_counters", type_="check")
        batch.drop_constraint("ck_download_jobs_collection_mode", type_="check")
        batch.drop_constraint("ck_download_jobs_type", type_="check")
        batch.drop_column("collection_profile_json")
        batch.drop_column("collection_mode")
        batch.drop_column("failed_items")
        batch.drop_column("completed_items")
        batch.drop_column("total_items")
        batch.drop_column("job_type")
