"""Lightweight request actor boundary.

v0.1 has no authentication, so every request resolves to the local actor.
Replacing ``get_actor`` later with real identity resolution should not
require changes to domain services or persistence.
"""

from dataclasses import dataclass

LOCAL_ACTOR_ID = "local"


@dataclass(frozen=True)
class Actor:
    id: str = LOCAL_ACTOR_ID


def get_local_actor() -> Actor:
    return Actor()
