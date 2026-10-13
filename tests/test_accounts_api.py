"""Signing in and out, changing a password, and the admin's Users routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.core import accounts

WEB = "https://lectern.example"  # through a tunnel or a proxy with TLS
LAN = "http://lectern.lan"  # the home network, without TLS
PASSWORD = "we walked to the lake in may"  # noqa: S105 (made up for the tests)
OTHER = "and came back before the rain"
REFUSED = {"detail": accounts.REFUSED}
NOON = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


class Clock:
    """The time the app is told it is: a test moves it instead of waiting."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(NOON)


@pytest.fixture
def app(paths, clock):
    app = create_app(paths, public_urls=[WEB, LAN], clock=clock)
    accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
    accounts.add_user(app.state.sessions, "olena", PASSWORD)
    return app


def _browser(app, base: str = WEB) -> TestClient:
    return TestClient(app, base_url=base, headers={"Origin": base})


def _sign_in(app, login: str = "olena", password: str = PASSWORD, base: str = WEB):  # noqa: ANN202
    browser = _browser(app, base)
    answer = browser.post("/api/login", json={"login": login, "password": password})
    assert answer.status_code == 200, answer.text
    browser.headers["X-Lectern-Token"] = answer.json()["token"]
    return browser, answer


def _cookie_line(answer) -> str:  # noqa: ANN001
    (line,) = answer.headers.get_list("set-cookie")
    return line


# --- signing in ----------------------------------------------------------------------------------


def test_signing_in_gives_a_cookie_no_script_can_read_and_a_token_for_the_page(app):
    browser, answer = _sign_in(app)

    assert answer.json() == {
        "user": {"login": "olena", "role": "member"},
        "token": browser.headers["X-Lectern-Token"],
    }
    line = _cookie_line(answer)
    name, value = line.split(";")[0].split("=", 1)
    assert name == "__Host-lectern" and len(value) >= 40
    parts = {p.strip().lower() for p in line.split(";")[1:]}
    assert {"httponly", "secure", "samesite=lax", "path=/"} <= parts
    assert not any(p.startswith("domain") for p in parts)  # this host only
    assert f"max-age={30 * 24 * 3600}" in parts
    assert answer.headers["cache-control"] == "no-store"
    assert value not in answer.text  # the id is the cookie's alone

    assert browser.get("/api/projects").status_code == 200  # and it is a login


def test_without_tls_the_cookie_is_plain(app):
    _, answer = _sign_in(app, base=LAN)
    line = _cookie_line(answer).lower()
    assert line.startswith("lectern=") and "secure" not in line and "httponly" in line


@pytest.mark.parametrize(
    ("login", "password"),
    [("olena", OTHER), ("nobody", PASSWORD), ("", ""), ("olena", "")],
)
def test_a_refused_sign_in_says_one_thing_and_gives_no_cookie(app, login: str, password: str):
    answer = _browser(app).post("/api/login", json={"login": login, "password": password})
    assert (answer.status_code, answer.json()) == (401, REFUSED)
    assert "set-cookie" not in answer.headers


def test_each_sign_in_is_a_new_login_and_ends_the_one_the_browser_had(app):
    browser, first = _sign_in(app)
    old_cookie = browser.cookies["__Host-lectern"]
    old_token = browser.headers["X-Lectern-Token"]

    again = browser.post("/api/login", json={"login": "olena", "password": PASSWORD})

    assert browser.cookies["__Host-lectern"] != old_cookie
    assert again.json()["token"] != old_token
    stale = TestClient(app, base_url=WEB, cookies={"__Host-lectern": old_cookie})
    assert stale.get("/api/projects", headers={"X-Lectern-Token": old_token}).status_code == 401


def test_a_sign_in_lasts_thirty_days_in_use_and_a_week_unused_by_the_apps_clock(app, clock):
    browser, _ = _sign_in(app)
    for used_on in (6, 12, 18, 24, 29):  # never a week idle
        clock.now = NOON + timedelta(days=used_on)
        assert browser.get("/api/projects").status_code == 200
    clock.now = NOON + timedelta(days=30)  # thirty days in all
    assert browser.get("/api/projects").status_code == 401

    unused, _ = _sign_in(app)
    clock.now += timedelta(days=7, seconds=1)
    assert unused.get("/api/projects").status_code == 401


def test_five_wrong_passwords_make_the_account_wait_a_minute_by_the_apps_clock(app, clock):
    browser = _browser(app)
    for _ in range(5):
        wrong = browser.post("/api/login", json={"login": "olena", "password": OTHER})
        assert (wrong.status_code, wrong.json()) == (401, REFUSED)

    held = browser.post("/api/login", json={"login": "olena", "password": PASSWORD})
    assert (held.status_code, held.json()) == (401, REFUSED)  # the right one, too soon

    clock.now += timedelta(seconds=61)
    waited = browser.post("/api/login", json={"login": "olena", "password": PASSWORD})
    assert waited.status_code == 200


def test_signing_in_from_another_site_is_refused(app):
    answer = TestClient(app, base_url=WEB).post(
        "/api/login",
        json={"login": "olena", "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert answer.status_code == 403


def test_where_nobody_signs_in_there_is_nothing_to_sign_in_to(paths):
    app = create_app(paths)  # no `lectern user init`
    here = TestClient(
        app, base_url="http://127.0.0.1:8765", headers={"Origin": "http://127.0.0.1:8765"}
    )
    token = {"X-Lectern-Token": app.state.token}
    assert here.get("/api/login", headers=token).json() == {
        "accounts": False,
        "user": {"login": None, "role": "admin"},
        "token": None,
    }
    answer = here.post("/api/login", json={"login": "x", "password": PASSWORD}, headers=token)
    assert answer.status_code == 409
    # Nor is anyone added yet: the admin's own account comes first.
    added = here.post("/api/users", json={"login": "olena", "password": PASSWORD}, headers=token)
    assert added.status_code == 409 and "lectern user init" in added.json()["detail"]
    assert here.get("/api/login").status_code == 401  # and without the link, as before


# --- who am I ------------------------------------------------------------------------------------


def test_a_page_asks_whether_it_is_signed_in(app):
    stranger = _browser(app).get("/api/login")
    assert stranger.json() == {"accounts": True, "user": None, "token": None}

    browser, _ = _sign_in(app)
    token = browser.headers.pop("X-Lectern-Token")  # a new tab: the cookie, no token yet
    mine = browser.get("/api/login")
    assert mine.json() == {
        "accounts": True,
        "user": {"login": "olena", "role": "member"},
        "token": token,
    }
    assert mine.headers["cache-control"] == "no-store"


# --- signing out ---------------------------------------------------------------------------------


def test_signing_out_ends_the_login_on_the_server_and_clears_the_cookie(app):
    browser, _ = _sign_in(app)
    cookie, token = browser.cookies["__Host-lectern"], browser.headers["X-Lectern-Token"]

    out = browser.post("/api/logout")

    assert out.status_code == 200
    assert "max-age=0" in _cookie_line(out).lower()
    kept = TestClient(app, base_url=WEB, cookies={"__Host-lectern": cookie})  # a copy of it
    assert kept.get("/api/projects", headers={"X-Lectern-Token": token}).status_code == 401


# --- one's own password --------------------------------------------------------------------------


def test_changing_a_password_keeps_this_browser_signed_in_and_signs_the_others_out(app):
    phone, _ = _sign_in(app)
    laptop, _ = _sign_in(app)

    changed = laptop.post("/api/password", json={"current": PASSWORD, "new": OTHER})

    assert changed.status_code == 200
    laptop.headers["X-Lectern-Token"] = changed.json()["token"]
    assert laptop.get("/api/projects").status_code == 200
    assert phone.get("/api/projects").status_code == 401
    assert (
        _browser(app).post("/api/login", json={"login": "olena", "password": PASSWORD}).status_code
        == 401
    )
    _sign_in(app, password=OTHER)


@pytest.mark.parametrize(
    ("current", "new", "status", "says"),
    [
        (OTHER, "a perfectly good new one", 403, "current password is wrong"),
        (PASSWORD, "too short", 400, "at least 15 characters"),
        (PASSWORD, "olena olena olena", 400, "sign-in name"),
    ],
)
def test_a_password_change_that_isnt_right_changes_nothing(app, current, new, status, says):
    browser, _ = _sign_in(app)
    answer = browser.post("/api/password", json={"current": current, "new": new})
    assert answer.status_code == status and says in answer.json()["detail"]
    _sign_in(app)  # the old one still works


# --- the admin's Users ---------------------------------------------------------------------------


def test_an_admin_adds_people_resets_passwords_and_disables_accounts(app):
    admin, _ = _sign_in(app, "yevhen")

    added = admin.post("/api/users", json={"login": "Taras", "password": PASSWORD})
    assert (added.status_code, added.json()) == (
        201,
        {"login": "taras", "role": "member", "disabled": False},
    )
    assert [u["login"] for u in admin.get("/api/users").json()] == ["yevhen", "olena", "taras"]
    taras, _ = _sign_in(app, "taras")

    assert admin.post("/api/users/taras/password", json={"password": OTHER}).status_code == 200
    assert taras.get("/api/projects").status_code == 401  # signed out by the reset
    taras, _ = _sign_in(app, "taras", OTHER)

    off = admin.post("/api/users/taras/disabled", json={"disabled": True})
    assert off.json() == {"login": "taras", "role": "member", "disabled": True}
    assert taras.get("/api/projects").status_code == 401
    assert (
        _browser(app).post("/api/login", json={"login": "taras", "password": OTHER}).status_code
        == 401
    )
    admin.post("/api/users/taras/disabled", json={"disabled": False})
    _sign_in(app, "taras", OTHER)


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "says"),
    [
        ("post", "/api/users", {"login": "olena", "password": PASSWORD}, 409, "already in use"),
        ("post", "/api/users", {"login": "../etc", "password": PASSWORD}, 400, "sign-in name is"),
        ("post", "/api/users", {"login": "taras", "password": "short"}, 400, "at least 15"),
        ("post", "/api/users/nobody/password", {"password": PASSWORD}, 404, "No account"),
        ("post", "/api/users/nobody/disabled", {"disabled": True}, 404, "No account"),
        ("post", "/api/users/yevhen/disabled", {"disabled": True}, 409, "only admin"),
    ],
)
def test_the_users_routes_say_why_they_refuse(app, method, path, body, status, says):
    admin, _ = _sign_in(app, "yevhen")
    answer = admin.request(method.upper(), path, json=body)
    assert answer.status_code == status and says in answer.json()["detail"]


def test_users_are_the_admins_to_see(app):
    member, _ = _sign_in(app)
    assert member.get("/api/users").status_code == 403


def test_no_password_or_id_is_sent_back(app):
    admin, signed = _sign_in(app, "yevhen")
    listed = admin.get("/api/users").text
    assert "argon2" not in listed and "password" not in listed
    assert admin.cookies["__Host-lectern"] not in signed.text


# --- how many sign-ins are checked at once --------------------------------------------------------


def test_only_a_few_sign_ins_are_checked_at_once_and_a_crowd_is_turned_away():
    """Checking a password takes 64 MiB. Anyone can reach the sign-in route, so without a
    limit forty requests at once would take 2.5 GB and every worker thread."""
    import asyncio

    from fastapi import HTTPException

    from lectern.api.accounts import SignInGate

    async def crowd() -> tuple[list[int], int, list[int]]:
        gate = SignInGate(at_once=2, waiting=3)
        release = asyncio.Event()
        checking: list[int] = []

        async def one(n: int) -> None:
            async with gate.turn():
                checking.append(n)
                await release.wait()

        people = [asyncio.create_task(one(n)) for n in range(5)]
        for _ in range(3):
            await asyncio.sleep(0)  # let them reach the gate
        at_once = list(checking)  # two are in; three wait their turn
        with pytest.raises(HTTPException) as turned_away:
            async with gate.turn():  # a sixth, while five are at the gate
                pass
        release.set()
        await asyncio.gather(*people)
        async with gate.turn():  # and afterwards there is room again
            pass
        return at_once, turned_away.value.status_code, checking

    at_once, status, everyone = asyncio.run(crowd())
    assert at_once == [0, 1]
    assert status == 429
    assert sorted(everyone) == [0, 1, 2, 3, 4]  # those who waited got their turn


def test_the_sign_in_route_goes_through_the_gate(app):
    from lectern.api.accounts import SignInGate

    app.state.sign_in_gate = SignInGate(at_once=1, waiting=-1)  # no room at all
    answer = _browser(app).post("/api/login", json={"login": "olena", "password": PASSWORD})
    assert answer.status_code == 429
    assert "Too many sign-ins at once" in answer.json()["detail"]
    assert answer.headers["retry-after"] == "5" and "set-cookie" not in answer.headers
