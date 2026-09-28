from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import AnalysisSession


async def add_analysis_session(session: AsyncSession, record: AnalysisSession) -> None:
    session.add(record)
    await session.commit()


async def get_analysis_session(
    session: AsyncSession, analysis_id: str, owner_id: str
) -> AnalysisSession | None:
    result = await session.execute(
        select(AnalysisSession).where(
            AnalysisSession.id == analysis_id,
            AnalysisSession.owner_id == owner_id,
        )
    )
    return result.scalar_one_or_none()
