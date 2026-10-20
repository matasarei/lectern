"""The web search switch: everyone sees it, only an admin changes it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.core import accounts

LOCAL = "http://127.0.0.1:8765"
WEB = "https://lectern.example"
PASSWORD = "we walked to the lake in may"  # noqa: S105 (made up for the tests)


@pytest.fixture
def app(paths):
    return create_app(paths, public_urls=[WEB])


def _local(app) -> TestClient:
    """The one local user, before accounts are on: an admin of their own install."""
    return TestClient(
        app, base_url=LOCAL, headers={"X-Lectern-Token": app.state.token, "Origin": LOCAL}
    )


def _signed_in(app, login: str) -> TestClient:
    browser = TestClient(app, base_url=WEB, headers={"Origin": WEB})
    answer = browser.post("/api/login", json={"login": login, "password": PASSWORD})
    assert answer.status_code == 200, answer.text
    browser.headers["X-Lectern-Token"] = answer.json()["token"]
    return browser


def _accounts(app) -> None:
    accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
    accounts.add_user(app.state.sessions, "olena", PASSWORD)


def test_web_search_is_off_until_switched_on(app):
    answer = _local(app).get("/api/web/settings")
    assert answer.status_code == 200
    assert answer.json() == {
        "values": {"enabled": False},
        "hosts": ["duckduckgo.com", "search.yahoo.com", "wikipedia.org"],
    }


def test_the_local_user_switches_it_on_and_off(app):
    client = _local(app)
    assert client.put("/api/web/settings", json={"enabled": True}).json()["values"] == {
        "enabled": True
    }
    assert client.get("/api/web/settings").json()["values"] == {"enabled": True}
    assert client.put("/api/web/settings", json={"enabled": False}).json()["values"] == {
        "enabled": False
    }


def test_an_admin_switches_it_and_a_member_sees_it_but_cant(app):
    _accounts(app)
    admin, member = _signed_in(app, "yevhen"), _signed_in(app, "olena")

    assert admin.put("/api/web/settings", json={"enabled": True}).status_code == 200
    assert member.get("/api/web/settings").json()["values"] == {"enabled": True}
    refused = member.put("/api/web/settings", json={"enabled": False})
    assert refused.status_code == 403
    assert admin.get("/api/web/settings").json()["values"] == {"enabled": True}  # unchanged


def test_someone_not_signed_in_gets_neither_route(app):
    _accounts(app)
    stranger = TestClient(app, base_url=WEB, headers={"Origin": WEB})
    assert stranger.get("/api/web/settings").status_code == 401
    assert stranger.put("/api/web/settings", json={"enabled": True}).status_code == 401
    admin = _signed_in(app, "yevhen")
    assert admin.get("/api/web/settings").json()["values"] == {"enabled": False}


@pytest.mark.parametrize("body", [{}, {"enabled": "yes please"}, {"enabled": None}])
def test_a_body_that_isnt_a_switch_is_refused(app, body):
    answer = _local(app).put("/api/web/settings", json=body)
    assert answer.status_code == 422
    assert _local(app).get("/api/web/settings").json()["values"] == {"enabled": False}


def test_switching_it_on_gives_the_next_turn_the_web_tools(app):
    names = lambda: {t.wire_name for t in app.state.turns._registry().all()}  # noqa: E731
    assert not names() & {"web_search", "wiki_lookup", "web_fetch"}
    _local(app).put("/api/web/settings", json={"enabled": True})
    assert {"web_search", "wiki_lookup", "web_fetch"} <= names()
