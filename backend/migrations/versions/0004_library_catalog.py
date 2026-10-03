"""library catalog

Creates the library_items and media_files tables for the Library milestone
(Milestone 3, ARCHITECTURE.md §23/§37): a database-backed catalog of media
known to the application, with one row per physical copy.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "library_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=True),
        sa.Column("platform", sa.String(length=32), nullable=True),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("artist", sa.String(length=512), nullable=True),
        sa.Column("album", sa.String(length=512), nullable=True),
        sa.Column("album_artist", sa.String(length=512), nullable=True),
        sa.Column("track_number", sa.Integer(), nullable=True),
        sa.Column("disc_number", sa.Integer(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("thumbnail_path", sa.String(length=1024), nullable=True),
        sa.Column("format", sa.String(length=32), nullable=True),
        sa.Column("codec", sa.String(length=64), nullable=True),
        sa.Column("bitrate", sa.Float(), nullable=True),
        sa.Column("resolution", sa.String(length=32), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_library_items_job_id", "library_items", ["job_id"])
    op.create_index(
        "ix_library_items_owner_created", "library_items", ["owner_id", "created_at"]
    )

    op.create_table(
        "media_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("library_item_id", sa.String(length=32), nullable=False),
        sa.Column("storage_type", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("display_path", sa.String(length=1024), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_optional", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # Catalog-level cascade only: deleting a library_items row removes its
        # catalog rows, never the physical file (PRODUCT_SPEC.md §21).
        sa.ForeignKeyConstraint(
            ["library_item_id"],
            ["library_items.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_media_files_library_item_id", "media_files", ["library_item_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_media_files_library_item_id", table_name="media_files")
    op.drop_table("media_files")
    op.drop_index("ix_library_items_owner_created", table_name="library_items")
    op.drop_index("ix_library_items_job_id", table_name="library_items")
    op.drop_table("library_items")
