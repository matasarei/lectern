"""Accounts and logins (`lectern.core.accounts`), with the clock passed in."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lectern.core import accounts
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.core.tables import LoginRow, UserRow

NOON = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
PASSWORD = "we walked to the lake in may"  # noqa: S105 (made up for the tests)
OTHER = "and came back before the rain"


@pytest.fixture
def db(paths):
    engine = make_engine(paths.app_db)
    migrate(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def family(db):
    """Accounts are on: the admin `yevhen` (the local user) and a member `olena`."""
    accounts.init_admin(db, "yevhen", PASSWORD)
    accounts.add_user(db, "olena", PASSWORD)
    return db


def _user(db, login: str) -> UserRow:
    with db() as session:
        return session.query(UserRow).filter_by(login=login).one()


# --- turning accounts on -------------------------------------------------------------------------


def test_nobody_signs_in_until_the_local_user_is_given_a_name_and_a_password(db):
    assert not accounts.accounts_on(db)
    with pytest.raises(accounts.SignInRefused):
        accounts.sign_in(db, "local", PASSWORD, NOON)

    admin = accounts.init_admin(db, "Yevhen", PASSWORD)

    assert accounts.accounts_on(db)
    # The same user as before, so its projects are still its own; the folder name stays.
    assert (admin.id, admin.name, admin.login, admin.role) == (1, "local", "yevhen", "admin")
    assert accounts.sign_in(db, "yevhen", PASSWORD, NOON) == admin


def test_accounts_are_set_up_once(family):
    with pytest.raises(accounts.AlreadySetUp):
        accounts.init_admin(family, "someone", PASSWORD)


# --- adding people -------------------------------------------------------------------------------


def test_a_person_added_is_a_member_with_a_folder_of_their_own_name(family):
    olena = accounts.sign_in(family, "olena", PASSWORD, NOON)
    assert (olena.name, olena.login, olena.role) == ("olena", "olena", "member")
    assert accounts.add_user(family, "taras", PASSWORD, admin=True).role == "admin"
    assert [u.login for u in accounts.list_users(family)] == ["yevhen", "olena", "taras"]


@pytest.mark.parametrize("login", ["", "a", "has space", "../etc", "ol/ena", "x" * 33, ".hidden"])
def test_a_sign_in_name_that_couldnt_be_a_folder_is_refused(family, login: str):
    with pytest.raises(accounts.BadName):
        accounts.add_user(family, login, PASSWORD)


@pytest.mark.parametrize("login", ["olena", "OLENA", "yevhen", "local"])  # "local": user 1's folder
def test_a_name_already_in_use_is_refused(family, login: str):
    with pytest.raises(accounts.NameTaken):
        accounts.add_user(family, login, PASSWORD)


def test_a_weak_password_is_refused_when_set(family):
    from lectern.core.passwords import WeakPassword

    with pytest.raises(WeakPassword):
        accounts.add_user(family, "taras", "too short")
    with pytest.raises(WeakPassword):
        accounts.set_password(family, "olena", "olena-olena-olena")


# --- signing in ----------------------------------------------------------------------------------


def test_the_name_is_typed_in_any_case_and_the_password_exactly(family):
    assert accounts.sign_in(family, " Olena ", PASSWORD, NOON).login == "olena"
    with pytest.raises(accounts.SignInRefused):
        accounts.sign_in(family, "olena", PASSWORD.upper(), NOON)


def test_every_refusal_reads_the_same(family):
    accounts.set_disabled(family, "olena", True)
    messages = set()
    for login, password in [("olena", PASSWORD), ("yevhen", OTHER), ("nobody", PASSWORD)]:
        with pytest.raises(accounts.SignInRefused) as refused:
            accounts.sign_in(family, login, password, NOON)
        messages.add(str(refused.value))
    assert len(messages) == 1  # disabled, wrong password, no such name


def test_after_five_wrong_passwords_the_account_waits_longer_each_time(family):
    for _ in range(4):
        with pytest.raises(accounts.SignInRefused):
            accounts.sign_in(family, "olena", OTHER, NOON)
    assert accounts.sign_in(family, "olena", PASSWORD, NOON)  # four slips are forgiven…
    assert _user(family, "olena").failed_logins == 0  # …and forgotten

    for _ in range(5):
        with pytest.raises(accounts.SignInRefused):
            accounts.sign_in(family, "olena", OTHER, NOON)
    # The fifth starts a one-minute wait: even the right password is refused during it.
    with pytest.raises(accounts.SignInRefused):
        accounts.sign_in(family, "olena", PASSWORD, NOON + timedelta(seconds=59))
    assert _user(family, "olena").failed_logins == 5  # a try during the wait isn't counted

    # After it, one more wrong one doubles the wait.
    with pytest.raises(accounts.SignInRefused):
        accounts.sign_in(family, "olena", OTHER, NOON + timedelta(seconds=61))
    with pytest.raises(accounts.SignInRefused):
        accounts.sign_in(family, "olena", PASSWORD, NOON + timedelta(seconds=61 + 119))
    assert accounts.sign_in(family, "olena", PASSWORD, NOON + timedelta(seconds=61 + 121))


def test_the_wait_never_grows_past_a_quarter_of_an_hour(family):
    now = NOON
    for _ in range(30):
        with pytest.raises(accounts.SignInRefused):
            accounts.sign_in(family, "olena", OTHER, now)
        now += timedelta(minutes=16)  # past any wait: each try counts
    assert accounts.sign_in(family, "olena", PASSWORD, now)  # never locked for good


def test_one_persons_wrong_passwords_dont_hold_up_another(family):
    for _ in range(6):
        with pytest.raises(accounts.SignInRefused):
            accounts.sign_in(family, "olena", OTHER, NOON)
    assert accounts.sign_in(family, "yevhen", PASSWORD, NOON)


def test_a_hash_made_with_old_settings_is_renewed_at_the_next_sign_in(family):
    from argon2 import PasswordHasher

    weak = PasswordHasher(memory_cost=8192, time_cost=1, parallelism=1).hash(PASSWORD)
    with family.begin() as session:
        session.query(UserRow).filter_by(login="olena").one().password_hash = weak

    accounts.sign_in(family, "olena", PASSWORD, NOON)

    renewed = _user(family, "olena").password_hash
    assert renewed != weak and "m=65536" in renewed
    assert accounts.sign_in(family, "olena", PASSWORD, NOON)


# --- logins --------------------------------------------------------------------------------------


def test_a_login_names_its_user_until_it_is_ended(family):
    olena = accounts.sign_in(family, "olena", PASSWORD, NOON)
    login_id, token = accounts.open_login(family, olena.id, NOON)
    other_id, other_token = accounts.open_login(family, olena.id, NOON)

    assert accounts.find_login(family, login_id, NOON) == (olena, token)
    assert (login_id, token) != (other_id, other_token) and login_id != token
    assert accounts.find_login(family, "not-a-login", NOON) is None
    assert accounts.find_login(family, "", NOON) is None

    accounts.end_login(family, login_id)
    assert accounts.find_login(family, login_id, NOON) is None
    assert accounts.find_login(family, other_id, NOON) == (olena, other_token)  # the other browser


def test_the_table_holds_no_id_a_browser_could_use(family):
    olena = accounts.sign_in(family, "olena", PASSWORD, NOON)
    login_id, _ = accounts.open_login(family, olena.id, NOON)
    with family() as session:
        (row,) = session.query(LoginRow).all()
    assert row.id_hash != login_id and len(row.id_hash) == 64
    assert accounts.find_login(family, row.id_hash, NOON) is None  # the hash isn't the key


def test_a_login_ends_after_a_week_unused_or_thirty_days_in_all(family):
    olena = accounts.sign_in(family, "olena", PASSWORD, NOON)
    login_id, _ = accounts.open_login(family, olena.id, NOON)

    day = timedelta(days=1)
    for used_on in (6, 12, 18, 24, 29):  # used every six days: never a week idle
        assert accounts.find_login(family, login_id, NOON + used_on * day) is not None
    assert accounts.find_login(family, login_id, NOON + 30 * day) is None  # thirty days in all
    with family() as session:
        assert session.query(LoginRow).count() == 0  # and it is gone, not kept

    unused, _ = accounts.open_login(family, olena.id, NOON)
    assert accounts.find_login(family, unused, NOON + 7 * day + timedelta(seconds=1)) is None


def test_a_new_password_or_a_disabled_account_ends_that_persons_logins(family):
    olena = accounts.sign_in(family, "olena", PASSWORD, NOON)
    yevhen = accounts.sign_in(family, "yevhen", PASSWORD, NOON)
    hers, _ = accounts.open_login(family, olena.id, NOON)
    his, _ = accounts.open_login(family, yevhen.id, NOON)

    accounts.set_password(family, "olena", OTHER)
    assert accounts.find_login(family, hers, NOON) is None
    assert accounts.find_login(family, his, NOON) is not None
    assert accounts.sign_in(family, "olena", OTHER, NOON)

    hers, _ = accounts.open_login(family, olena.id, NOON)
    accounts.set_disabled(family, "olena", True)
    assert accounts.find_login(family, hers, NOON) is None
    accounts.set_disabled(family, "olena", False)
    assert accounts.sign_in(family, "olena", OTHER, NOON)


def test_the_last_admin_cant_be_disabled(family):
    with pytest.raises(accounts.LastAdmin):
        accounts.set_disabled(family, "yevhen", True)
    accounts.add_user(family, "taras", PASSWORD, admin=True)
    accounts.set_disabled(family, "yevhen", True)  # now there is another


def test_someone_who_isnt_there_cant_be_changed(family):
    with pytest.raises(accounts.NoSuchUser):
        accounts.set_password(family, "nobody", PASSWORD)
    with pytest.raises(accounts.NoSuchUser):
        accounts.set_disabled(family, "nobody", True)
