"""Who is asking. In the MVP it is always the single local user; login comes after the MVP.

Every /api route depends on `current_user`, so adding real accounts later means changing
this one function, not the routes.
"""

from __future__ import annotations

from dataclasses import dataclass

from starlette.requests import HTTPConnection

from lectern.core.tables import LOCAL_USER_ID, LOCAL_USER_NAME


@dataclass(frozen=True)
class User:
    id: int
    name: str


LOCAL_USER = User(id=LOCAL_USER_ID, name=LOCAL_USER_NAME)


def current_user(connection: HTTPConnection) -> User:
    """FastAPI dependency for HTTP and WebSocket routes alike."""
    del connection  # the MVP has one user; after login this reads the session
    return LOCAL_USER
