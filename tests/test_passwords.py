from __future__ import annotations

import pytest

from lectern.core import passwords

GOOD = "correct horse battery staple"


def test_a_password_is_stored_as_an_argon2id_hash_that_only_it_opens():
    stored = passwords.hash_password(GOOD)

    assert stored.startswith("$argon2id$")
    assert GOOD not in stored
    assert passwords.verify(stored, GOOD)
    assert not passwords.verify(stored, GOOD + " ")
    assert passwords.hash_password(GOOD) != stored  # its own salt each time


def test_the_hash_is_at_least_as_strong_as_owasp_asks():
    # Argon2id with 19 MiB of memory and 2 passes is the least OWASP accepts.
    settings = dict(
        part.split("=") for part in passwords.hash_password(GOOD).split("$")[3].split(",")
    )
    assert int(settings["m"]) >= 19456 and int(settings["t"]) >= 2


@pytest.mark.parametrize("stored", ["", "not a hash", "$argon2id$v=19$m=1,t=1,p=1$c2FsdA$broken"])
def test_something_that_isnt_a_hash_opens_for_nothing(stored: str):
    assert not passwords.verify(stored, GOOD)


def test_a_hash_made_with_weaker_settings_is_due_for_rehashing():
    from argon2 import PasswordHasher

    weak = PasswordHasher(memory_cost=8192, time_cost=1, parallelism=1).hash(GOOD)
    assert passwords.verify(weak, GOOD)
    assert passwords.needs_rehash(weak)
    assert not passwords.needs_rehash(passwords.hash_password(GOOD))


@pytest.mark.parametrize(
    ("password", "why"),
    [
        ("fourteen chars", "at least 15 characters"),
        ("x" * 257, "at most 256"),
        ("olena-olena-olena", "your sign-in name"),
        ("               ", "at least 15 characters"),  # spaces alone aren't a password
    ],
)
def test_a_new_password_that_is_too_weak_is_refused_and_says_why(password: str, why: str):
    with pytest.raises(passwords.WeakPassword, match=why):
        passwords.check_new(password, login="Olena")


def test_a_long_passphrase_needs_no_digits_or_symbols():
    passwords.check_new("we walked to the lake in may", login="olena")  # no error
    passwords.check_new("пароль з українських слів", login="olena")


def test_an_unknown_name_costs_a_hash_like_a_known_one():
    """So the time a refusal takes doesn't say whether the account exists."""
    assert passwords.verify_nobody(GOOD) is False
