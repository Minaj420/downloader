"""Persist the job output profile; NULL keeps existing rows Original."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("download_jobs", sa.Column("output_profile", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("download_jobs") as batch:
        batch.drop_column("output_profile")
