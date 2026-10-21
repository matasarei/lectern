"""The web search switch: everyone sees it, only an admin changes it."""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.core import accounts

LOCAL = "http://127.0.0.1:8765"
WEB = "https://lectern.example"
PASSWORD = "we walked to the lake in may"  # noqa: S105 (made up for the tests)
KEY = "BSA-test-key-not-real"  # made up for the tests


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
        "brave_key": False,
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


# --- the Brave Search API key ---------------------------------------------------------------


def test_an_admin_sets_a_key_and_only_its_presence_comes_back(app, paths):
    client = _local(app)
    answer = client.put("/api/web/key", json={"key": f"  {KEY}  "})
    assert answer.status_code == 200
    assert answer.json()["brave_key"] is True
    assert answer.json()["hosts"][0] == "api.search.brave.com"  # asked first
    for response in (answer, client.get("/api/web/settings")):
        assert KEY not in response.text
    assert paths.web_search_key_file.read_text() == KEY  # trimmed
    if sys.platform != "win32":
        assert os.stat(paths.web_search_key_file).st_mode & 0o777 == 0o600


def test_a_new_key_replaces_the_old_and_removing_it_goes_back_to_the_free_engines(app, paths):
    client = _local(app)
    client.put("/api/web/key", json={"key": "first-key-123"})
    client.put("/api/web/key", json={"key": KEY})
    assert paths.web_search_key_file.read_text() == KEY
    answer = client.delete("/api/web/key")
    assert answer.json()["brave_key"] is False
    assert answer.json()["hosts"] == ["duckduckgo.com", "search.yahoo.com", "wikipedia.org"]
    assert not paths.web_search_key_file.exists()
    assert client.delete("/api/web/key").status_code == 200  # no key to remove: no error


@pytest.mark.parametrize("key", ["", "short", "two words here", "tab\tinside", "x" * 201])
def test_a_key_that_cant_be_one_is_refused(app, paths, key):
    assert _local(app).put("/api/web/key", json={"key": key}).status_code == 422
    assert not paths.web_search_key_file.exists()


def test_only_an_admin_sets_or_removes_the_key(app, paths):
    _accounts(app)
    admin, member = _signed_in(app, "yevhen"), _signed_in(app, "olena")
    stranger = TestClient(app, base_url=WEB, headers={"Origin": WEB})

    assert member.put("/api/web/key", json={"key": KEY}).status_code == 403
    assert stranger.put("/api/web/key", json={"key": KEY}).status_code == 401
    assert not paths.web_search_key_file.exists()
    assert admin.put("/api/web/key", json={"key": KEY}).status_code == 200
    assert member.delete("/api/web/key").status_code == 403
    assert stranger.delete("/api/web/key").status_code == 401
    assert member.get("/api/web/settings").json()["brave_key"] is True  # a member sees there is one
    assert KEY not in member.get("/api/web/settings").text
    assert paths.web_search_key_file.exists()


def test_with_a_key_the_next_turns_search_asks_brave_first(app):
    client = _local(app)
    client.put("/api/web/settings", json={"enabled": True})

    def search_order() -> list[str]:
        search = app.state.turns._registry().get("web_search")
        return search.confirm_text(None, search.validate({"query": "q"}))

    assert "api.search.brave.com" not in search_order()
    client.put("/api/web/key", json={"key": KEY})
    assert search_order().endswith(
        "The query goes to api.search.brave.com, or, if it's busy, duckduckgo.com or "
        "search.yahoo.com."
    )
    client.delete("/api/web/key")
    assert "api.search.brave.com" not in search_order()


def test_the_key_isnt_read_while_web_search_is_off(app, monkeypatch):
    _local(app).put("/api/web/key", json={"key": KEY})
    read = []
    monkeypatch.setattr("lectern.app.read_secret", lambda path: read.append(path))
    app.state.turns._registry()
    assert read == []
