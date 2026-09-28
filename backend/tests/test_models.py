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
