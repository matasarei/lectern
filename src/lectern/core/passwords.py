"""Passwords: how one is stored, checked, and what a new one must be.

Stored as an Argon2id hash (argon2-cffi), which carries its own salt and settings: a hash made
with weaker settings still verifies, and `needs_rehash` says it is due for a new one. The
library's defaults (64 MiB, 3 passes) are above the least OWASP accepts (19 MiB, 2 passes).

A new password is judged by its length alone, as NIST and OWASP advise for an account with no
second factor: 15 characters, no rules about digits or symbols. It isn't checked against lists
of breached passwords: that would send it, or a part of its hash, off this computer.
"""

from __future__ import annotations

from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 15
MAX_LENGTH = 256  # room for a passphrase; a bound on what is hashed

_hasher = PasswordHasher()


class WeakPassword(ValueError):
    """A new password that isn't accepted. The message says why, for the person."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify(stored: str, password: str) -> bool:
    """Whether `password` is the one `stored` was made from. Anything that isn't a hash
    opens for nothing."""
    try:
        return _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    return _hasher.check_needs_rehash(stored)


def verify_nobody(password: str) -> bool:
    """Spend the time a real check takes, for a name no account has: how long a refusal
    takes then doesn't say whether the account exists. Always False."""
    verify(_nobodys_hash(), password)
    return False


@cache
def _nobodys_hash() -> str:
    return hash_password("no account has this password")


def check_new(password: str, *, login: str) -> None:
    """Raise WeakPassword unless `password` can be somebody's new password."""
    if len(password.strip()) < MIN_LENGTH:
        raise WeakPassword(f"A password needs at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise WeakPassword(f"A password can have at most {MAX_LENGTH} characters.")
    if login and login.casefold() in password.casefold():
        raise WeakPassword("A password can't contain your sign-in name.")
