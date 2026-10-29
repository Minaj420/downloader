from sqlalchemy import Index

from app import models  # noqa: F401  (registers ORM models)
from app.database import Base


def test_analysis_sessions_table_is_registered_on_metadata() -> None:
    table = Base.metadata.tables["analysis_sessions"]

    assert table.columns["id"] is not None
    assert table.columns["expires_at"] is not None


def test_created_at_index_matches_migration() -> None:
    table = Base.metadata.tables["analysis_sessions"]

    index_names = {index.name for index in table.indexes}
    assert "ix_analysis_sessions_created_at" in index_names
    index = next(
        i for i in table.indexes if i.name == "ix_analysis_sessions_created_at"
    )
    assert isinstance(index, Index)
    assert [column.name for column in index.columns] == ["created_at"]


def test_library_tables_are_registered_on_metadata() -> None:
    items = Base.metadata.tables["library_items"]
    files = Base.metadata.tables["media_files"]

    assert {"id", "owner_id", "job_id", "media_type", "title", "created_at"} <= set(
        items.columns.keys()
    )
    assert {
        "id",
        "library_item_id",
        "storage_type",
        "path",
        "size_bytes",
        "created_at",
    } <= set(files.columns.keys())


def test_library_indexes_match_migration() -> None:
    items = Base.metadata.tables["library_items"]
    files = Base.metadata.tables["media_files"]

    items_job_index = next(
        i for i in items.indexes if i.name == "ix_library_items_job_id"
    )
    assert [column.name for column in items_job_index.columns] == ["job_id"]

    items_owner_index = next(
        i for i in items.indexes if i.name == "ix_library_items_owner_created"
    )
    assert [column.name for column in items_owner_index.columns] == [
        "owner_id",
        "created_at",
    ]

    files_index = next(
        i for i in files.indexes if i.name == "ix_media_files_library_item_id"
    )
    assert [column.name for column in files_index.columns] == ["library_item_id"]


def test_media_files_foreign_key_references_library_items_with_cascade() -> None:
    files = Base.metadata.tables["media_files"]

    foreign_keys = list(files.columns["library_item_id"].foreign_keys)
    assert len(foreign_keys) == 1
    foreign_key = foreign_keys[0]
    assert foreign_key.column.table.name == "library_items"
    assert foreign_key.column.name == "id"
    # Catalog-level cascade only (PRODUCT_SPEC.md §21): deleting a library
    # item row removes its catalog rows, never the physical file.
    assert foreign_key.ondelete == "CASCADE"


def test_collection_tables_are_registered_with_parent_ownership_and_order() -> None:
    parent = Base.metadata.tables["download_jobs"]
    child = Base.metadata.tables["job_items"]
    assert {
        "job_type", "total_items", "completed_items", "failed_items",
        "collection_mode", "collection_profile_json",
    } <= set(parent.columns.keys())
    assert "owner_id" not in child.columns
    fk = next(iter(child.columns["job_id"].foreign_keys))
    assert fk.target_fullname == "download_jobs.id"
    assert fk.ondelete == "CASCADE"
    index = next(i for i in child.indexes if i.name == "ix_job_items_job_source_order")
    assert [column.name for column in index.columns] == ["job_id", "source_order"]


def test_output_profile_is_one_shared_nullable_parent_column() -> None:
    parent = Base.metadata.tables["download_jobs"]
    child = Base.metadata.tables["job_items"]
    column = parent.columns["output_profile"]
    assert column.nullable and column.server_default is None and column.default is None
    assert str(column.type) == "VARCHAR(16)"
    # Children inherit the parent choice; no per-child output column.
    assert "output_profile" not in child.columns
