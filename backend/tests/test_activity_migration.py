"""Migration 0008 (monotonic Activity ids) against temporary SQLite files only."""

import asyncio
import io
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, event, exc, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app import models  # noqa: F401
from app.database import MIGRATIONS_DIR, Base
from app.models.jobs import ActivityEvent
from app.repositories import activity as activity_repo

_TEMP_TABLE = "activity_events_0008_rebuild"
_ACTIVITY_OBJECTS = {"activity_events", "ix_activity_events_job_id", "sqlite_sequence"}

# Seeded legacy ids keep gaps (3, 4, 6..8) and mix NULL/non-NULL job/details.
_SEEDED_ACTIVITY = [
    (1, "job", "info", "job", "Queued", None, "2026-01-01 00:00:00.000000"),
    (2, None, "warning", "system", "Startup recovery", '{"count": 2}',
     "2026-01-01 00:00:01.000000"),
    (5, "job", "error", "job", "Failed: x", '{"code": "network_error"}',
     "2026-01-02 00:00:00.123456"),
    (9, "parent", "info", "job", "Retry queued", None, "2026-01-03 00:00:00.000000"),
]


def _config(database: Path, buffer: io.StringIO | None = None) -> Config:
    config = Config(output_buffer=buffer) if buffer is not None else Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def _engine(database: Path) -> Engine:
    return create_engine(f"sqlite:///{database}")


def _schema(connection: Connection) -> dict[str, tuple[str, str, str | None]]:
    rows = connection.execute(text("SELECT type, name, tbl_name, sql FROM sqlite_master"))
    return {name: (kind, table, sql) for kind, name, table, sql in rows}


def _rows(connection: Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        text(f"SELECT * FROM {table} ORDER BY rowid")
    ).mappings()]


def _companion_tables(connection: Connection) -> list[str]:
    return sorted(
        name for (name,) in connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN "
            "('activity_events', 'sqlite_sequence')"
        ))
    )


def _snapshot(connection: Connection) -> dict[str, Any]:
    """Everything outside activity_events: schema entries plus every row."""
    schema = {
        name: entry for name, entry in _schema(connection).items()
        if name not in _ACTIVITY_OBJECTS
    }
    rows = {table: _rows(connection, table) for table in _companion_tables(connection)
            if table != "alembic_version"}
    return {"schema": schema, "rows": rows}


def _activity_sql(connection: Connection) -> str:
    sql = connection.scalar(text(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'activity_events'"
    ))
    assert isinstance(sql, str)
    return sql


def _sequence(connection: Connection) -> int | None:
    if connection.scalar(text(
        "SELECT count(*) FROM sqlite_master WHERE name = 'sqlite_sequence'"
    )) == 0:
        return None
    value = connection.scalar(
        text("SELECT seq FROM sqlite_sequence WHERE name = 'activity_events'")
    )
    return int(value) if value is not None else None


def _indexes(connection: Connection) -> set[tuple[str, tuple[str, ...], bool]]:
    return {
        (index["name"], tuple(str(c) for c in index["column_names"]), bool(index["unique"]))
        for index in inspect(connection).get_indexes("activity_events")
        if index["name"] is not None
    }


def _version(connection: Connection) -> str | None:
    value = connection.scalar(text("SELECT version_num FROM alembic_version"))
    return str(value) if value is not None else None


def _insert_raw(connection: Connection) -> int:
    result = connection.execute(text(
        "INSERT INTO activity_events (job_id, level, event_type, message, created_at) "
        "VALUES (NULL, 'info', 'job', 'raw', '2026-01-04 00:00:00.000000')"
    ))
    assert result.lastrowid is not None
    return int(result.lastrowid)


def _seed_0007(database: Path) -> None:
    command.upgrade(_config(database), "0007")
    engine = _engine(database)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO download_jobs (id, owner_id, analysis_id, source_url, title,
                variant_id, destination, conflict_policy, keep_app_copy, status,
                progress_percent, created_at, updated_at, job_type, total_items,
                completed_items, failed_items, output_profile)
            VALUES ('parent', 'owner', 'analysis', 'url', 'title', '', 'app_library',
                'keep_both', 1, 'completed', 100, '2026-01-01', '2026-01-01',
                'collection', 1, 1, 0, NULL),
                ('job', 'owner', 'analysis', 'url', 'single', 'native', 'app_library',
                'skip', 0, 'failed', 40, '2026-01-01', '2026-01-02', 'single', 1, 0, 0,
                'mp3')
        """))
        connection.execute(text("""
            INSERT INTO job_items (id, job_id, item_id, source_order, source_url, status,
                final_path, created_at, updated_at)
            VALUES ('child', 'parent', 'item1', 1, 'url1', 'completed', 'a.opus',
                '2026-01-01', '2026-01-01')
        """))
        connection.execute(text("""
            INSERT INTO library_items
                (id, job_id, owner_id, source_url, title, media_type, created_at)
            VALUES ('child', 'parent', 'owner', 'url1', 'A', 'audio', '2026-01-01')
        """))
        connection.execute(text("""
            INSERT INTO media_files (library_item_id, storage_type, path, size_bytes, created_at)
            VALUES ('child', 'app_library', 'a.opus', 10, '2026-01-01')
        """))
        for row in _SEEDED_ACTIVITY:
            connection.execute(text("""
                INSERT INTO activity_events
                    (id, job_id, level, event_type, message, details_json, created_at)
                VALUES (:id, :job_id, :level, :event_type, :message, :details, :created_at)
            """), dict(zip(
                ("id", "job_id", "level", "event_type", "message", "details", "created_at"),
                row, strict=True,
            )))
    engine.dispose()


async def _add_via_repository(database: Path, *, commit: bool = True) -> int:
    """Allocate through the real repository writer (flush), then commit/rollback."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            record = await activity_repo.add_activity_event(
                session, job_id=None, level="info", event_type="job", message="new"
            )
            event_id = record.id
            if commit:
                await session.commit()
            else:
                await session.rollback()
        return event_id
    finally:
        await engine.dispose()


def test_activity_id_upgrade_preserves_seeded_rows_gaps_index_and_companions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "seeded.sqlite"
    _seed_0007(database)
    engine = _engine(database)
    with engine.connect() as connection:
        before_activity = _rows(connection, "activity_events")
        before_companions = _snapshot(connection)
        before_indexes = _indexes(connection)
        assert "AUTOINCREMENT" not in _activity_sql(connection).upper()

    command.upgrade(_config(database), "0008")

    with engine.connect() as connection:
        assert _rows(connection, "activity_events") == before_activity
        assert [row["id"] for row in before_activity] == [1, 2, 5, 9]
        assert _snapshot(connection) == before_companions
        assert _indexes(connection) == before_indexes == {
            ("ix_activity_events_job_id", ("job_id",), False)
        }
        assert "AUTOINCREMENT" in _activity_sql(connection).upper()
        assert _sequence(connection) == 9
        assert _TEMP_TABLE not in _schema(connection)
        columns = {c["name"]: c for c in inspect(connection).get_columns("activity_events")}
        assert {name: c["nullable"] for name, c in columns.items()} == {
            "id": False, "job_id": True, "level": False, "event_type": False,
            "message": False, "details_json": True, "created_at": False,
        }
        assert _version(connection) == "0008"
    with Session(engine) as session:
        events = session.query(ActivityEvent).order_by(ActivityEvent.id).all()
        assert [
            (e.id, e.job_id, e.level, e.event_type, e.message, e.details_json, e.created_at)
            for e in events
        ] == [
            (1, "job", "info", "job", "Queued", None, datetime(2026, 1, 1)),
            (2, None, "warning", "system", "Startup recovery", '{"count": 2}',
             datetime(2026, 1, 1, 0, 0, 1)),
            (5, "job", "error", "job", "Failed: x", '{"code": "network_error"}',
             datetime(2026, 1, 2, 0, 0, 0, 123456)),
            (9, "parent", "info", "job", "Retry queued", None, datetime(2026, 1, 3)),
        ]
    engine.dispose()


def test_activity_id_empty_upgrade_matches_orm_create_all(tmp_path: Path) -> None:
    migrated = tmp_path / "migrated.sqlite"
    command.upgrade(_config(migrated), "0007")
    command.upgrade(_config(migrated), "head")
    orm = tmp_path / "orm.sqlite"
    orm_engine = _engine(orm)
    Base.metadata.create_all(orm_engine, tables=[Base.metadata.tables["activity_events"]])
    migrated_engine = _engine(migrated)
    allocated: dict[str, list[int]] = {}
    for label, engine in (("migrated", migrated_engine), ("orm", orm_engine)):
        with engine.begin() as connection:
            assert "AUTOINCREMENT" in _activity_sql(connection).upper()
            # The empty INSERT ... SELECT copy may seed a 0 row; allocation is equal.
            assert _sequence(connection) in (None, 0)
            first = _insert_raw(connection)
            second = _insert_raw(connection)
            connection.execute(text("DELETE FROM activity_events WHERE id = :id"),
                               {"id": second})
            allocated[label] = [first, second, _insert_raw(connection)]
    assert allocated == {"migrated": [1, 2, 3], "orm": [1, 2, 3]}

    def describe(engine: Engine) -> dict[str, Any]:
        inspector = inspect(engine)
        return {
            "columns": [
                (c["name"], str(c["type"]), c["nullable"], c["default"])
                for c in inspector.get_columns("activity_events")
            ],
            "pk": inspector.get_pk_constraint("activity_events")["constrained_columns"],
            "indexes": sorted(
                (str(i["name"]), tuple(str(c) for c in i["column_names"]), bool(i["unique"]))
                for i in inspector.get_indexes("activity_events")
            ),
            "fks": inspector.get_foreign_keys("activity_events"),
        }

    assert describe(migrated_engine) == describe(orm_engine)
    head = ScriptDirectory.from_config(_config(migrated)).get_current_head()
    with migrated_engine.connect() as connection:
        assert _version(connection) == head
    migrated_engine.dispose()
    orm_engine.dispose()


def test_activity_id_never_reused_after_deleting_highest_and_all_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reuse.sqlite"
    _seed_0007(database)
    command.upgrade(_config(database), "0008")
    engine = _engine(database)

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM activity_events WHERE id = 9"))
    # Plain rowid would hand out 6 (MAX 5 + 1); the old high-water is 9.
    assert asyncio.run(_add_via_repository(database)) == 10

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM activity_events"))
    assert asyncio.run(_add_via_repository(database)) == 11

    # A rolled-back allocation was never committed or published; only the
    # committed high-water is promised, not that the rolled-back id is skipped.
    asyncio.run(_add_via_repository(database, commit=False))
    engine.dispose()

    reopened = _engine(database)
    with reopened.connect() as connection:
        assert _sequence(connection) == 11
        assert [row["id"] for row in _rows(connection, "activity_events")] == [11]
    next_id = asyncio.run(_add_via_repository(database))
    assert next_id > 11
    with reopened.connect() as connection:
        assert [row["id"] for row in _rows(connection, "activity_events")] == [11, next_id]
        assert _sequence(connection) == next_id
    reopened.dispose()


def test_activity_id_upgrade_failure_after_rebuild_rolls_back_atomically(
    tmp_path: Path,
) -> None:
    database = tmp_path / "failure.sqlite"
    _seed_0007(database)
    engine = _engine(database)
    with engine.begin() as connection:
        # Fires on Alembic's version stamp, i.e. after the revision's DROP,
        # RENAME and CREATE INDEX ran but before the version/commit completed.
        connection.execute(text("""
            CREATE TRIGGER inject_0008_failure BEFORE UPDATE ON alembic_version
            BEGIN SELECT RAISE(ABORT, 'injected 0008 failure'); END
        """))
    with engine.connect() as connection:
        before_schema = _schema(connection)
        before_activity = _rows(connection, "activity_events")
        before_companions = _snapshot(connection)

    statements: list[tuple[str, bool]] = []

    def record(conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        in_transaction = bool(conn.connection.dbapi_connection.in_transaction)
        statements.append((" ".join(statement.split()), in_transaction))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        with pytest.raises(exc.IntegrityError, match="injected 0008 failure"):
            command.upgrade(_config(database), "0008")
    finally:
        event.remove(Engine, "before_cursor_execute", record)

    executed = [sql for sql, _ in statements]
    begin_index = executed.index("UPDATE activity_events SET job_id = job_id WHERE 1 = 0")
    ddl = [
        index for index, sql in enumerate(executed)
        if sql.startswith((f"CREATE TABLE {_TEMP_TABLE}", f"INSERT INTO {_TEMP_TABLE}",
                           "DROP INDEX", "DROP TABLE", "ALTER TABLE", "CREATE INDEX"))
    ]
    assert len(ddl) == 6 and begin_index < min(ddl)
    stamp_index = next(
        index for index, sql in enumerate(executed) if sql.startswith("UPDATE alembic_version")
    )
    assert max(ddl) < stamp_index
    # The zero-row UPDATE opened the real driver transaction before any DDL.
    assert all(statements[index][1] for index in ddl)
    assert statements[stamp_index][1]

    with engine.connect() as connection:
        assert _version(connection) == "0007"
        assert _schema(connection) == before_schema
        assert _rows(connection, "activity_events") == before_activity
        assert _snapshot(connection) == before_companions
        assert _TEMP_TABLE not in _schema(connection)
        assert "AUTOINCREMENT" not in _activity_sql(connection).upper()
        assert _sequence(connection) is None
        assert _indexes(connection) == {("ix_activity_events_job_id", ("job_id",), False)}
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER inject_0008_failure"))

    command.upgrade(_config(database), "0008")
    with engine.connect() as connection:
        assert _version(connection) == "0008"
        assert _rows(connection, "activity_events") == before_activity
        assert "AUTOINCREMENT" in _activity_sql(connection).upper()
        assert _sequence(connection) == 9
    engine.dispose()


def test_activity_id_downgrade_keeps_rows_and_index_but_loses_high_water(
    tmp_path: Path,
) -> None:
    database = tmp_path / "downgrade.sqlite"
    _seed_0007(database)
    command.upgrade(_config(database), "0008")
    engine = _engine(database)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM activity_events WHERE id = 9"))
        assert _insert_raw(connection) == 10
        connection.execute(text("DELETE FROM activity_events WHERE id = 10"))
    with engine.connect() as connection:
        rows = _rows(connection, "activity_events")
        companions = _snapshot(connection)

    command.downgrade(_config(database), "0007")
    with engine.connect() as connection:
        assert _version(connection) == "0007"
        assert _rows(connection, "activity_events") == rows
        assert _snapshot(connection) == companions
        assert _indexes(connection) == {("ix_activity_events_job_id", ("job_id",), False)}
        assert "AUTOINCREMENT" not in _activity_sql(connection).upper()
        assert _sequence(connection) is None
        assert _TEMP_TABLE not in _schema(connection)

    # Documented guarantee loss: re-upgrade restarts from the current MAX (5),
    # not from the old high-water mark (10), so deleted ids 6..10 can recur.
    command.upgrade(_config(database), "0008")
    with engine.begin() as connection:
        assert _sequence(connection) == 5
        assert _insert_raw(connection) == 6
    engine.dispose()


def _offline_sql(database: Path, revisions: str, *, downgrade: bool = False) -> str:
    buffer = io.StringIO()
    config = _config(database, buffer)
    if downgrade:
        command.downgrade(config, revisions, sql=True)
    else:
        command.upgrade(config, revisions, sql=True)
    return buffer.getvalue()


def _assert_wrapped(sql: str, stamp: str) -> None:
    """One explicit BEGIN before every statement, COMMIT after the version stamp."""
    statements = [
        line.strip() for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    assert statements[0] == "BEGIN;"
    assert statements[-1] == "COMMIT;"
    assert sql.count("BEGIN;") == 1 and sql.count("COMMIT;") == 1
    assert sql.index("BEGIN;") < sql.index(stamp) < sql.index("COMMIT;")


def test_activity_id_offline_upgrade_script_applies_atomically(tmp_path: Path) -> None:
    script_db = tmp_path / "unused.sqlite"
    sql = _offline_sql(script_db, "0007:0008")
    assert not script_db.exists()
    stamp = "UPDATE alembic_version SET version_num='0008'"
    _assert_wrapped(sql, stamp)
    assert sql.index("UPDATE activity_events SET job_id = job_id WHERE 1 = 0") < sql.index(
        f"CREATE TABLE {_TEMP_TABLE}"
    )
    assert "AUTOINCREMENT" in sql
    assert sql.index("CREATE INDEX ix_activity_events_job_id") < sql.index(stamp)

    database = tmp_path / "offline.sqlite"
    _seed_0007(database)
    engine = _engine(database)
    with engine.connect() as connection:
        before_activity = _rows(connection, "activity_events")
        before_companions = _snapshot(connection)
    engine.dispose()
    connection_raw = sqlite3.connect(database)
    try:
        connection_raw.executescript(sql)
        assert not connection_raw.in_transaction
    finally:
        connection_raw.close()

    engine = _engine(database)
    with engine.begin() as connection:
        assert _version(connection) == "0008"
        assert _rows(connection, "activity_events") == before_activity
        assert _snapshot(connection) == before_companions
        assert _indexes(connection) == {("ix_activity_events_job_id", ("job_id",), False)}
        assert "AUTOINCREMENT" in _activity_sql(connection).upper()
        assert _TEMP_TABLE not in _schema(connection)
        assert _sequence(connection) == 9
        connection.execute(text("DELETE FROM activity_events WHERE id = 9"))
        assert _insert_raw(connection) == 10
    engine.dispose()


def test_activity_id_offline_upgrade_script_failure_rolls_back(tmp_path: Path) -> None:
    sql = _offline_sql(tmp_path / "unused.sqlite", "0007:0008")
    database = tmp_path / "offline-failure.sqlite"
    _seed_0007(database)
    engine = _engine(database)
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TRIGGER inject_0008_failure BEFORE UPDATE ON alembic_version
            BEGIN SELECT RAISE(ABORT, 'injected offline 0008 failure'); END
        """))
    with engine.connect() as connection:
        before_schema = _schema(connection)
        before_activity = _rows(connection, "activity_events")
        before_companions = _snapshot(connection)

    connection_raw = sqlite3.connect(database)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected offline 0008 failure"):
            connection_raw.executescript(sql)
        # The script stopped at the stamp inside its open BEGIN. The runner
        # must roll back here; continuing the script to COMMIT is not allowed.
        assert connection_raw.in_transaction
        connection_raw.rollback()
    finally:
        connection_raw.close()

    with engine.connect() as connection:
        assert _version(connection) == "0007"
        assert _schema(connection) == before_schema
        assert _rows(connection, "activity_events") == before_activity
        assert _snapshot(connection) == before_companions
        assert _indexes(connection) == {("ix_activity_events_job_id", ("job_id",), False)}
        assert "AUTOINCREMENT" not in _activity_sql(connection).upper()
        assert _TEMP_TABLE not in _schema(connection)
        assert _sequence(connection) is None
    engine.dispose()


def test_activity_id_offline_downgrade_script_is_wrapped(tmp_path: Path) -> None:
    sql = _offline_sql(tmp_path / "unused.sqlite", "0008:0007", downgrade=True)
    _assert_wrapped(sql, "UPDATE alembic_version SET version_num='0007'")
    assert "AUTOINCREMENT" not in sql

    database = tmp_path / "offline-downgrade.sqlite"
    _seed_0007(database)
    command.upgrade(_config(database), "0008")
    engine = _engine(database)
    with engine.connect() as connection:
        before_activity = _rows(connection, "activity_events")
    engine.dispose()
    connection_raw = sqlite3.connect(database)
    try:
        connection_raw.executescript(sql)
    finally:
        connection_raw.close()
    engine = _engine(database)
    with engine.connect() as connection:
        assert _version(connection) == "0007"
        assert _rows(connection, "activity_events") == before_activity
        assert "AUTOINCREMENT" not in _activity_sql(connection).upper()
        assert _sequence(connection) is None
        assert _TEMP_TABLE not in _schema(connection)
    engine.dispose()
