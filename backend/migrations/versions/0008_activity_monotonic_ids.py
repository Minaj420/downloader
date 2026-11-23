"""Make activity_events ids monotonic: never reuse a committed Activity id.

Revision ID: 0008
Revises: 0007

ARCHITECTURE §59 Q3 prerequisite. Migration 0003 created ``activity_events.id``
as a plain SQLite ``INTEGER PRIMARY KEY`` (rowid alias, no AUTOINCREMENT), so
after the highest rows are deleted SQLite hands their ids out again. SSE
live dedupe (``id <= replay_hwm``) and replay (``cursor < id``) assume ids are
never reused. This revision rebuilds only ``activity_events`` as
``INTEGER PRIMARY KEY AUTOINCREMENT`` (``sqlite_autoincrement``): SQLite then
allocates above the largest id ever stored in the table (tracked in
``sqlite_sequence``), even after the highest or all rows are deleted.

Preserved exactly: every column, type and nullability (``job_id`` and
``details_json`` stay nullable), every existing row with its original id
(gaps included; ids are copied, never renumbered) and the non-unique
``ix_activity_events_job_id`` index. Copying the existing ids seeds
``sqlite_sequence`` with the current ``MAX(id)``. Only automatic id allocation
changes; no other table, catalog/job/child row, DTO, API or read order changes.

Atomicity: Alembic owns the per-migration transaction (SQLite has
non-transactional DDL in Alembic, so it begins one connection transaction per
revision and commits it after the version row update). The legacy pysqlite
driver, however, emits ``BEGIN`` only before DML, so a leading ``CREATE TABLE``
would autocommit outside that transaction. The first statement is therefore a
fixed zero-row ``UPDATE activity_events``: it makes the driver open the real
transaction before any DDL, and online mode verifies that the DBAPI connection
is in a transaction before continuing (fail closed). The rebuild DDL/DML and
Alembic's version update then commit or roll back together. No PRAGMA is
changed, nothing is dropped "if exists" and no commit/rollback is issued here.
Offline (``--sql``) output emits the same statements without the online check;
``env.py`` configures offline SQLite with ``transactional_ddl=True``, so the
script wraps them and the version stamp in one explicit ``BEGIN``/``COMMIT``.
Whoever runs such a script must roll back after any error, never continue it
to ``COMMIT``.

Downgrade rebuilds the plain ``INTEGER PRIMARY KEY`` table with the same rows,
ids and index. It deliberately LOSES the monotonic guarantee: after deletes
(e.g. Q3 Clear History) old revisions may reuse ids again, and the stored
high-water mark (``sqlite_sequence``) is dropped. A later re-upgrade restarts
allocation only from the then-current ``MAX(id)``, not from the old high-water
mark, so ids deleted above it can be reused once. Do not downgrade production.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "activity_events"
_TEMP_TABLE = "activity_events_0008_rebuild"
_INDEX = "ix_activity_events_job_id"
_COLUMNS = "id, job_id, level, event_type, message, details_json, created_at"


def _begin_real_transaction() -> None:
    """Open the driver transaction before any DDL; verify it online."""
    # Fixed, zero-row DML: pysqlite (legacy transaction control) issues BEGIN
    # before INSERT/UPDATE/DELETE, never before CREATE/DROP/ALTER.
    op.execute(f"UPDATE {_TABLE} SET job_id = job_id WHERE 1 = 0")
    if context.is_offline_mode():
        return
    dbapi_connection = op.get_bind().connection.dbapi_connection
    if getattr(dbapi_connection, "in_transaction", None) is not True:
        raise RuntimeError(
            "0008 activity_events rebuild requires an open database transaction"
        )


def _rebuild(*, autoincrement: bool) -> None:
    _begin_real_transaction()
    op.create_table(
        _TEMP_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=True),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("message", sa.String(length=1024), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=autoincrement,
    )
    op.execute(
        f"INSERT INTO {_TEMP_TABLE} ({_COLUMNS}) SELECT {_COLUMNS} FROM {_TABLE} ORDER BY id"
    )
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
    op.rename_table(_TEMP_TABLE, _TABLE)
    op.create_index(_INDEX, _TABLE, ["job_id"])


def upgrade() -> None:
    _rebuild(autoincrement=True)


def downgrade() -> None:
    _rebuild(autoincrement=False)
