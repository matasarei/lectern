"""Who is asking. In the MVP it is always the single local user; login comes after the MVP.

Every /api route depends on `current_user`, so adding real accounts later means changing
this one function, not the routes.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from starlette.requests import HTTPConnection

from lectern.core.tables import LOCAL_USER_ID, LOCAL_USER_NAME

ADMIN = "admin"  # may also manage models, settings and accounts
MEMBER = "member"  # their own projects, and a read-only view of the rest


@dataclass(frozen=True)
class User:
    id: int
    name: str  # the folder their projects are under
    login: str | None = None  # what they sign in as; None where nobody signs in
    role: str = ADMIN


# The one user of an install without accounts. It owns the computer, so it is an admin.
LOCAL_USER = User(id=LOCAL_USER_ID, name=LOCAL_USER_NAME)


def current_user(connection: HTTPConnection) -> User:
    """FastAPI dependency for HTTP and WebSocket routes alike: whoever the guard
    (`lectern.core.security.LocalGuard`) found the request to be from. Nobody: refused, so a
    route the guard somehow didn't cover is closed, not open."""
    user = getattr(connection.state, "user", None)
    if user is None:
        raise HTTPException(401, "Not signed in")
    return user
