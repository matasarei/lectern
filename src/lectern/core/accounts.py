"""Accounts and logins: who can sign in, and which browsers are signed in.

An install starts with one user, `local`, and no accounts: nobody signs in, and the page is
opened with the per-install link. `init_admin` gives that user a sign-in name and a password;
from then on `accounts_on` is true, people sign in, and the admin adds the others. There is no
sign-up.

A wrong name, a wrong password, a disabled account and one that must wait are all refused
alike (`SignInRefused`), after the same hashing work, so a refusal says nothing about which it
was. Wrong passwords are counted per account, not per address: after FREE_TRIES in a row the
next try must wait, twice as long each time, up to LONGEST_WAIT. Never for good: a stranger who
knew a name could otherwise lock its owner out.

A login is a row per browser signed in. The browser holds a random id in a cookie; the row
holds the id's SHA-256, the user, and the token that browser sends with every request.

The clock is passed in, so every wait and expiry here can be tested without waiting.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from lectern.core import passwords
from lectern.core.auth import ADMIN, MEMBER, User
from lectern.core.tables import LOCAL_USER_ID, LoginRow, UserRow

# A sign-in name is also the name of the person's folder (users/<name>/projects): lower-case
# letters, digits, dot, dash and underscore, starting with a letter or a digit.
LOGIN_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")

FREE_TRIES = 5  # wrong passwords in a row before a try must wait
FIRST_WAIT = timedelta(minutes=1)
LONGEST_WAIT = timedelta(minutes=15)

LOGIN_LIFE = timedelta(days=30)  # a browser signs in again after this, however much it's used
LOGIN_IDLE = timedelta(days=7)  # or after this long unused
SEEN_EVERY = timedelta(minutes=5)  # "last seen" is written this often, not on every request

REFUSED = (
    "Sign-in failed: the name or the password is wrong, or there were too many tries. "
    "Wait a minute before trying again."
)


class SignInRefused(Exception):
    """Nobody is signed in. One message, whatever the cause."""

    def __init__(self) -> None:
        super().__init__(REFUSED)


class AlreadySetUp(Exception):
    """Accounts are on already: the admin has a password."""


class BadName(ValueError):
    """Not a sign-in name. The message says what one is."""


class NameTaken(ValueError):
    """Somebody has that sign-in name, or that folder."""


class NoSuchUser(LookupError):
    """No account has that sign-in name."""


class LastAdmin(ValueError):
    """The only admin who can sign in can't be disabled: nobody could manage accounts."""


Sessions = sessionmaker[Session]


# --- accounts ------------------------------------------------------------------------------------


def accounts_on(sessions: Sessions) -> bool:
    """Whether people sign in here: true once any user has a password."""
    with sessions() as session:
        return (
            session.scalar(select(UserRow.id).where(UserRow.password_hash.is_not(None))) is not None
        )


def init_admin(sessions: Sessions, login: str, password: str) -> User:
    """Turn accounts on: the local user becomes the admin `login`, keeping what it owns."""
    login = _checked_name(login)
    passwords.check_new(password, login=login)
    with sessions.begin() as session:
        if session.scalar(select(UserRow.id).where(UserRow.password_hash.is_not(None))):
            raise AlreadySetUp(
                "Accounts are already set up. Change a password with `user password`."
            )
        row = session.get(UserRow, LOCAL_USER_ID)
        row.login, row.role, row.disabled = login, ADMIN, False
        row.password_hash = passwords.hash_password(password)
        return _user(row)


def add_user(sessions: Sessions, login: str, password: str, *, admin: bool = False) -> User:
    login = _checked_name(login)
    passwords.check_new(password, login=login)
    with sessions.begin() as session:
        taken = session.scalar(
            select(UserRow.id).where((UserRow.login == login) | (UserRow.name == login))
        )
        if taken is not None:
            raise NameTaken(f"The name {login!r} is already in use.")
        row = UserRow(
            name=login,
            login=login,
            role=ADMIN if admin else MEMBER,
            password_hash=passwords.hash_password(password),
        )
        session.add(row)
        session.flush()
        return _user(row)


def list_users(sessions: Sessions) -> list[User]:
    """Everyone with an account, in the order they were added."""
    with sessions() as session:
        rows = session.scalars(
            select(UserRow).where(UserRow.login.is_not(None)).order_by(UserRow.id)
        )
        return [_user(row) for row in rows]


def describe(sessions: Sessions, login: str | None = None) -> list[dict]:
    """The accounts as the Users page shows them (one, or all in the order they were added):
    never a hash."""
    with sessions() as session:
        query = select(UserRow).where(UserRow.login.is_not(None)).order_by(UserRow.id)
        if login is not None:
            query = query.where(UserRow.login == login.strip().lower())
        return [
            {"login": row.login, "role": row.role, "disabled": row.disabled}
            for row in session.scalars(query)
        ]


def set_password(sessions: Sessions, login: str, password: str) -> None:
    """A new password ends every login of that person, and any wait they were in."""
    with sessions.begin() as session:
        row = _by_login(session, login)
        passwords.check_new(password, login=row.login or "")
        row.password_hash = passwords.hash_password(password)
        row.failed_logins, row.locked_until = 0, None
        _end_logins_of(session, row.id)


def set_disabled(sessions: Sessions, login: str, disabled: bool) -> None:
    """A disabled account can't sign in, and its logins end. Its projects stay."""
    with sessions.begin() as session:
        row = _by_login(session, login)
        if disabled and row.role == ADMIN and _admins_able_to_sign_in(session, but=row.id) == 0:
            raise LastAdmin("The only admin can't be disabled.")
        row.disabled = disabled
        if disabled:
            _end_logins_of(session, row.id)


def sign_in(sessions: Sessions, login: str, password: str, now: datetime) -> User:
    """The user `login` and `password` belong to; SignInRefused for anything else."""
    name = login.strip().lower()
    # Committed whatever the outcome: a refusal still has a count to keep.
    with sessions.begin() as session:
        row = session.scalar(select(UserRow).where(UserRow.login == name))
        user = _let_in(row, password, now)
    if user is None:
        raise SignInRefused
    return user


def _let_in(row: UserRow | None, password: str, now: datetime) -> User | None:
    """The user, if `password` signs `row` in now; the row's count and wait are updated."""
    if row is None or row.password_hash is None:
        passwords.verify_nobody(password)
        return None
    if row.locked_until is not None and now < _utc(row.locked_until):
        # A try during the wait: refused whatever the password, and not counted.
        passwords.verify_nobody(password)
        return None
    if not passwords.verify(row.password_hash, password):
        row.failed_logins += 1
        if row.failed_logins >= FREE_TRIES:
            doubled = FIRST_WAIT * 2 ** min(row.failed_logins - FREE_TRIES, 10)
            row.locked_until = now + min(doubled, LONGEST_WAIT)
        return None
    if row.disabled:
        return None
    row.failed_logins, row.locked_until = 0, None
    if passwords.needs_rehash(row.password_hash):
        row.password_hash = passwords.hash_password(password)
    return _user(row)


# --- logins --------------------------------------------------------------------------------------


def open_login(sessions: Sessions, user_id: int, now: datetime) -> tuple[str, str]:
    """A new login for the user: the id for the browser's cookie, and the token it sends in a
    header with every request. Each is 256 random bits."""
    login_id, token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with sessions.begin() as session:
        session.add(
            LoginRow(
                id_hash=_fingerprint(login_id),
                user_id=user_id,
                token=token,
                created_at=now,
                last_seen_at=now,
                expires_at=now + LOGIN_LIFE,
            )
        )
    return login_id, token


def find_login(sessions: Sessions, login_id: str, now: datetime) -> tuple[User, str] | None:
    """Who the browser holding `login_id` is signed in as, and its token. None when it isn't:
    never was, signed out, expired, unused too long, or the account is disabled. A login past
    its time is removed."""
    if not login_id:
        return None
    with sessions.begin() as session:
        row = session.get(LoginRow, _fingerprint(login_id))
        if row is None:
            return None
        if now >= _utc(row.expires_at) or now - _utc(row.last_seen_at) > LOGIN_IDLE:
            session.delete(row)
            return None
        user = session.get(UserRow, row.user_id)
        if user is None or user.disabled:
            return None
        if now - _utc(row.last_seen_at) >= SEEN_EVERY:
            row.last_seen_at = now
        return _user(user), row.token


def end_login(sessions: Sessions, login_id: str) -> None:
    """Sign that browser out. Nothing to do if it wasn't signed in."""
    with sessions.begin() as session:
        row = session.get(LoginRow, _fingerprint(login_id))
        if row is not None:
            session.delete(row)


class Logins:
    """What the guard on every request asks (`lectern.core.security.Logins`), with the clock
    it runs by."""

    def __init__(
        self, sessions: Sessions, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ):
        self.sessions, self.clock = sessions, clock

    def accounts_on(self) -> bool:
        return accounts_on(self.sessions)

    def find(self, login_id: str) -> tuple[User, str] | None:
        return find_login(self.sessions, login_id, self.clock())


# --- inside ---------------------------------------------------------------------------------------


def _checked_name(login: str) -> str:
    name = login.strip().lower()
    if not LOGIN_NAME.fullmatch(name):
        raise BadName(
            "A sign-in name is 2 to 32 lower-case letters, digits, dots, dashes or "
            "underscores, starting with a letter or a digit."
        )
    return name


def _by_login(session: Session, login: str) -> UserRow:
    row = session.scalar(select(UserRow).where(UserRow.login == login.strip().lower()))
    if row is None:
        raise NoSuchUser(f"No account is named {login!r}.")
    return row


def _admins_able_to_sign_in(session: Session, *, but: int) -> int:
    return session.scalar(
        select(func.count())
        .select_from(UserRow)
        .where(
            UserRow.role == ADMIN,
            UserRow.disabled.is_(False),
            UserRow.password_hash.is_not(None),
            UserRow.id != but,
        )
    )


def _end_logins_of(session: Session, user_id: int) -> None:
    for row in session.scalars(select(LoginRow).where(LoginRow.user_id == user_id)):
        session.delete(row)


def _user(row: UserRow) -> User:
    return User(id=row.id, name=row.name, login=row.login, role=row.role)


def _fingerprint(login_id: str) -> str:
    return hashlib.sha256(login_id.encode()).hexdigest()


def _utc(moment: datetime) -> datetime:
    """SQLite hands a time back without its zone; they are all stored in UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
