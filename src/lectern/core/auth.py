"""Who is asking: the one local user until `lectern user init` has run, an account after.

Every /api route depends on `current_user`, which returns whoever the guard signed the
request in as; the routes didn't change when accounts came. `require_admin` is for what
changes the install.
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


def require_admin(connection: HTTPConnection) -> User:
    """FastAPI dependency for what changes the install itself: its models, its settings, its
    setup, its accounts. A member may look at those, not change them."""
    user = current_user(connection)
    if user.role != ADMIN:
        raise HTTPException(403, "Only an admin can do that.")
    return user
