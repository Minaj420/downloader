from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.actors import Actor, get_local_actor


def get_actor() -> Actor:
    """Resolve the request actor. v0.1 always resolves to the local actor."""
    return get_local_actor()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.sessionmaker
    async with session_factory() as session:
        yield session
