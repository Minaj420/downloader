"""SQLAlchemy ORM models.

Importing the models here registers them on ``Base.metadata`` so Alembic
autogenerate and migrations see the full schema.
"""

from app.models.analysis import AnalysisSession

__all__ = ["AnalysisSession"]
