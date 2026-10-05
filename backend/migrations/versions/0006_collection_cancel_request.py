"""Persist the internal collection Cancel request without rewriting existing rows."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("download_jobs", sa.Column(
        "collection_cancel_requested_at", sa.DateTime(timezone=True), nullable=True,
    ))


def downgrade() -> None:
    with op.batch_alter_table("download_jobs") as batch:
        batch.drop_column("collection_cancel_requested_at")
