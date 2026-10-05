"""Real migrations against isolated temporary SQLite databases only."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    CheckConstraint,
    DefaultClause,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import Session

from app import models  # noqa: F401
from app.database import MIGRATIONS_DIR, Base
from app.models.jobs import DownloadJob


def upgrade(database: Path, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, revision)


def test_upgrade_0004_preserves_every_legacy_single_field(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite"
    upgrade(database, "0004")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        for status in ("queued", "failed", "completed", "cancelled"):
            connection.execute(text("""
                INSERT INTO download_jobs (
                    id, owner_id, analysis_id, source_url, title, variant_id,
                    destination, conflict_policy, keep_app_copy, status,
                    progress_percent, downloaded_bytes, total_bytes,
                    speed_bytes_per_second, eta_seconds, staging_path, final_path,
                    error_code, error_message, error_detail, created_at,
                    updated_at, started_at, completed_at
                ) VALUES (
                    :status, 'owner', 'analysis', 'https://example.com/video',
                    'Legacy title', 'audio-original', 'app_library', 'keep_both',
                    1, :status, 87.5, 42, 100, 4, 9, 'staging', 'final',
                    'network_error', 'message', 'detail', '2026-01-01 00:00:00',
                    '2026-01-02 00:00:00', '2026-01-01 01:00:00',
                    '2026-01-02 01:00:00'
                )
            """), {"status": status})
        before = [dict(row) for row in connection.execute(
            text("SELECT * FROM download_jobs ORDER BY id")
        ).mappings()]
    upgrade(database, "0006")
    with Session(engine) as session:
        for old in before:
            job = session.get(DownloadJob, old["id"])
            assert job is not None
            assert job.status == old["status"] and job.variant_id == old["variant_id"]
            assert job.final_path == old["final_path"] and job.job_type == "single"
    with engine.connect() as connection:
        after = [dict(row) for row in connection.execute(
            text("SELECT * FROM download_jobs ORDER BY id")
        ).mappings()]
        for old, new in zip(before, after, strict=True):
            assert {key: new[key] for key in old} == old
            assert new["job_type"] == "single"
            assert (new["total_items"], new["completed_items"], new["failed_items"]) == (1, 0, 0)
            assert new["collection_mode"] is None
            assert new["collection_profile_json"] is None
        assert connection.scalar(text("SELECT count(*) FROM job_items")) == 0
        assert connection.scalar(text("SELECT count(*) FROM library_items")) == 0
        assert connection.scalar(text("SELECT count(*) FROM media_files")) == 0
        assert all(row["collection_cancel_requested_at"] is None for row in after)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
    engine.dispose()


def test_0006_preserves_0005_collections_children_catalog_and_downgrades(tmp_path: Path) -> None:
    database = tmp_path / "collection-legacy.sqlite"
    upgrade(database, "0005")
    engine = create_engine(f"sqlite:///{database}")
    tables = ("download_jobs", "job_items", "library_items", "media_files", "activity_events")
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO download_jobs (id, owner_id, analysis_id, source_url, title,
                variant_id, destination, conflict_policy, keep_app_copy, status,
                progress_percent, created_at, updated_at, job_type, total_items,
                completed_items, failed_items)
            VALUES ('parent', 'owner', 'analysis', 'url', 'title', '', 'app_library',
                'keep_both', 1, 'analyzing', 50, '2026-01-01', '2026-01-01', 'collection', 2, 1, 1),
                ('single', 'owner', 'analysis', 'url', 'single', 'native', 'app_library',
                'keep_both', 1, 'completed', 100, '2026-01-01', '2026-01-01', 'single', 1, 0, 0)
        """))
        connection.execute(text("""
            INSERT INTO job_items (id, job_id, item_id, source_order, source_url, status,
                final_path, error_code, created_at, updated_at)
            VALUES ('done', 'parent', 'item1', 1, 'url', 'completed', 'kept.opus', NULL,
                    '2026-01-01', '2026-01-01'),
                ('failed', 'parent', 'item2', 2, 'url', 'failed', NULL, 'network_error',
                    '2026-01-01', '2026-01-01')
        """))
        connection.execute(text("""
            INSERT INTO library_items
                (id, job_id, owner_id, source_url, title, media_type, created_at)
            VALUES ('done', 'parent', 'owner', 'url', 'kept', 'audio', '2026-01-01')
        """))
        connection.execute(text("""
            INSERT INTO media_files (library_item_id, storage_type, path, size_bytes, created_at)
            VALUES ('done', 'app_library', 'kept.opus', 10, '2026-01-01')
        """))
        before = {table: [dict(row) for row in connection.execute(
            text(f"SELECT * FROM {table}")
        ).mappings()] for table in tables}
    upgrade(database, "0006")
    with engine.connect() as connection:
        after = {table: [dict(row) for row in connection.execute(
            text(f"SELECT * FROM {table}")
        ).mappings()] for table in tables}
        for row in after["download_jobs"]:
            assert row.pop("collection_cancel_requested_at") is None
        assert after == before
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.downgrade(config, "0005")
    with engine.connect() as connection:
        assert {table: [dict(row) for row in connection.execute(
            text(f"SELECT * FROM {table}")
        ).mappings()] for table in tables} == before
    engine.dispose()


@pytest.mark.parametrize("table_name", ["download_jobs", "job_items"])
def test_clean_migration_matches_orm_schema(tmp_path: Path, table_name: str) -> None:
    database = tmp_path / "clean.sqlite"
    upgrade(database, "0006")
    engine = create_engine(f"sqlite:///{database}")
    inspector = inspect(engine)
    table = Base.metadata.tables[table_name]
    columns = {column["name"]: column for column in inspector.get_columns(table_name)}
    assert set(columns) == set(table.columns.keys())
    for column in table.columns:
        actual = columns[column.name]
        assert actual["nullable"] == column.nullable
        assert str(actual["type"]) == str(column.type)
        expected = (
            str(column.server_default.arg)
            if isinstance(column.server_default, DefaultClause) else None
        )
        default = actual["default"]
        assert (default.strip("'()") if default is not None else None) == expected
    assert {
        (index["name"], tuple(index["column_names"]), bool(index["unique"]))
        for index in inspector.get_indexes(table_name)
    } == {
        (index.name, tuple(column.name for column in index.columns), bool(index.unique))
        for index in table.indexes
    }
    assert {
        (constraint["name"], tuple(constraint["column_names"]))
        for constraint in inspector.get_unique_constraints(table_name)
    } == {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert {
        (constraint["name"], constraint["sqltext"])
        for constraint in inspector.get_check_constraints(table_name)
    } == {
        (constraint.name, str(constraint.sqltext))
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert inspector.get_pk_constraint(table_name)["constrained_columns"] == [
        column.name for column in table.primary_key.columns
    ]
    actual_fks = inspector.get_foreign_keys(table_name)
    assert len(actual_fks) == len(table.foreign_key_constraints)
    if table_name == "job_items":
        assert actual_fks[0]["constrained_columns"] == ["job_id"]
        assert actual_fks[0]["referred_table"] == "download_jobs"
        assert actual_fks[0]["referred_columns"] == ["id"]
        assert actual_fks[0]["options"]["ondelete"] == "CASCADE"
    engine.dispose()
