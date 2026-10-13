"""/api/login, /api/logout, /api/password and /api/users: signing in and out, one's own
password, and the admin's accounts.

`/api/login` is the one path a browser that isn't signed in can reach (the guard lets it
through; see `lectern.core.security`). Signing in sets the cookie that names the login, and
answers with the token the page then sends in a header with every request. A new tab, which
has the cookie but not the token, asks for it with `GET /api/login`: another site can make a
browser send that request, but can't read the answer.

There is no sign-up: an admin adds people, here or with `lectern user add`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from lectern.core import accounts
from lectern.core.auth import User, current_user, require_admin
from lectern.core.passwords import MAX_LENGTH, WeakPassword
from lectern.core.security import login_cookie

router = APIRouter()
# The two routes someone who isn't signed in can reach. Mounted apart from the rest of /api,
# which asks for a user before anything else (`lectern.api.open_api_router`).
open_router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}  # an answer holding a token is never kept


class SignInGate:
    """How many sign-ins are checked at once. Checking a password takes 64 MiB and a worker
    thread, and anyone who reaches Lectern can ask: `at_once` are checked, `waiting` more
    wait their turn (on the event loop, which costs nothing), and the rest are told to come
    back. A family never notices; a flood can't take the memory or the threads."""

    def __init__(self, at_once: int = 2, waiting: int = 8) -> None:
        self._checking = asyncio.Semaphore(at_once)
        self._room = at_once + waiting
        self._here = 0

    @contextlib.asynccontextmanager
    async def turn(self) -> AsyncIterator[None]:
        if self._here >= self._room:
            raise HTTPException(
                429, "Too many sign-ins at once. Try again in a moment.", {"Retry-After": "5"}
            )
        self._here += 1
        try:
            async with self._checking:
                yield
        finally:
            self._here -= 1


class SignIn(BaseModel):
    login: str = Field(max_length=64)
    password: str = Field(max_length=MAX_LENGTH)


class PasswordChange(BaseModel):
    current: str = Field(max_length=MAX_LENGTH)
    new: str = Field(max_length=MAX_LENGTH)


class NewUser(BaseModel):
    login: str = Field(max_length=64)
    password: str = Field(max_length=MAX_LENGTH)
    admin: bool = False


class NewPassword(BaseModel):
    password: str = Field(max_length=MAX_LENGTH)


class Disabled(BaseModel):
    disabled: bool


def _cookie(request: Request) -> tuple[str, bool]:
    """The login cookie's name on the address this request came to, and whether Secure."""
    return login_cookie(request.headers.get("host", "").lower(), request.app.state.public)


def _who(user: User) -> dict:
    return {"login": user.login, "role": user.role}


def _open_login(request: Request, response: Response, user: User) -> str:
    """Sign this browser in as `user`: a new login, its id in the cookie. Returns its token.
    The login the browser had, if any, ends: an id is never kept across a sign-in."""
    sessions = request.app.state.sessions
    name, secure = _cookie(request)
    if had := request.cookies.get(name):
        accounts.end_login(sessions, had)
    login_id, token = accounts.open_login(sessions, user.id, request.app.state.clock())
    response.set_cookie(
        name,
        login_id,
        max_age=int(accounts.LOGIN_LIFE.total_seconds()),
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.headers.update(NO_STORE)
    return token


@open_router.get("/login")
def whoami(request: Request, response: Response) -> dict:
    """Whether people sign in here, who this browser is signed in as, and its token."""
    response.headers.update(NO_STORE)
    user: User | None = request.state.user
    return {
        "accounts": accounts.accounts_on(request.app.state.sessions),
        "user": _who(user) if user is not None else None,
        "token": request.state.login_token,
    }


@open_router.post("/login")
async def sign_in(body: SignIn, request: Request, response: Response) -> dict:
    sessions = request.app.state.sessions
    if not await run_in_threadpool(accounts.accounts_on, sessions):
        raise HTTPException(409, "Nobody signs in here: accounts aren't set up.")
    async with request.app.state.sign_in_gate.turn():
        try:
            user = await run_in_threadpool(
                accounts.sign_in, sessions, body.login, body.password, request.app.state.clock()
            )
        except accounts.SignInRefused as refused:
            raise HTTPException(401, str(refused)) from None
    token = await run_in_threadpool(_open_login, request, response, user)
    return {"user": _who(user), "token": token}


@router.post("/logout")
def sign_out(request: Request, response: Response) -> dict:
    name, secure = _cookie(request)
    if login_id := request.cookies.get(name):
        accounts.end_login(request.app.state.sessions, login_id)
    response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")
    return {"signed_out": True}


@router.post("/password")
def change_password(
    body: PasswordChange,
    request: Request,
    response: Response,
    user: User = Depends(current_user),  # noqa: B008 (FastAPI's way)
) -> dict:
    """One's own password. It ends every login of theirs; this browser gets a new one."""
    sessions = request.app.state.sessions
    if user.login is None:
        raise HTTPException(409, "This is the computer's own link, not an account.")
    try:
        accounts.sign_in(sessions, user.login, body.current, request.app.state.clock())
    except accounts.SignInRefused:
        raise HTTPException(403, "The current password is wrong.") from None
    try:
        accounts.set_password(sessions, user.login, body.new)
    except WeakPassword as weak:
        raise HTTPException(400, str(weak)) from None
    return {"token": _open_login(request, response, user)}


# --- the admin's ---------------------------------------------------------------------------

admin = APIRouter(dependencies=[Depends(require_admin)])


@admin.get("/users")
def list_users(request: Request) -> list[dict]:
    return accounts.describe(request.app.state.sessions)


@admin.post("/users", status_code=201)
async def add_user(body: NewUser, request: Request) -> dict:
    sessions = request.app.state.sessions
    try:
        user = await run_in_threadpool(
            accounts.add_user, sessions, body.login, body.password, admin=body.admin
        )
    except (accounts.BadName, WeakPassword) as bad:
        raise HTTPException(400, str(bad)) from None
    except (accounts.NameTaken, accounts.NotSetUp) as refused:
        raise HTTPException(409, str(refused)) from None
    return accounts.describe(sessions, user.login)[0]


@admin.post("/users/{login}/password")
def reset_password(login: str, body: NewPassword, request: Request) -> dict:
    """A new password for someone who forgot theirs. It signs them out everywhere."""
    try:
        accounts.set_password(request.app.state.sessions, login, body.password)
    except accounts.NoSuchUser as missing:
        raise HTTPException(404, str(missing)) from None
    except WeakPassword as weak:
        raise HTTPException(400, str(weak)) from None
    return {"login": login.strip().lower()}


@admin.post("/users/{login}/disabled")
def set_disabled(login: str, body: Disabled, request: Request) -> dict:
    sessions = request.app.state.sessions
    try:
        accounts.set_disabled(sessions, login, body.disabled)
    except accounts.NoSuchUser as missing:
        raise HTTPException(404, str(missing)) from None
    except accounts.LastAdmin as last:
        raise HTTPException(409, str(last)) from None
    return accounts.describe(sessions, login)[0]


router.include_router(admin)
