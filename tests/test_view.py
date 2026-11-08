"""An HTML file opened as a page in a tab of its own: the one-use link, and the sandbox it is
served in. (Someone else's file answers 404 at the POST: the sweep over every project route
in test_auth_seam.py covers it.)"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from lectern.api.view import GONE, VIEW_TICKET_SECONDS
from lectern.app import create_app
from lectern.core.auth import LOCAL_USER
from lectern.core.security import CONTENT_SECURITY_POLICY, VIEW_CSP
from lectern.projects import files

BASE = "http://127.0.0.1:8765"
PAGE = b"<!doctype html><style>h1{color:red}</style><h1>Hi</h1><script>alert(1)</script>"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 7, 12, 0, tzinfo=UTC))


@pytest.fixture
def api(paths, clock):
    app = create_app(paths, clock=clock)
    headers = {"X-Lectern-Token": app.state.token, "Origin": BASE}
    with TestClient(app, base_url=BASE) as client:
        pid = client.post("/api/projects", json={"name": "Pages"}, headers=headers).json()["id"]
        yield client, headers, app, pid


def _import(client, headers, pid, name: str, content: bytes = PAGE) -> int:
    return client.put(
        f"/api/projects/{pid}/files", params={"name": name}, content=content, headers=headers
    ).json()["id"]


def _link(client, headers, pid, aid):
    return client.post(f"/api/projects/{pid}/artifacts/{aid}/view", headers=headers)


def test_an_html_file_opens_once_sandboxed_with_its_own_bytes(api):
    client, headers, _, pid = api
    aid = _import(client, headers, pid, "page.html")

    link = _link(client, headers, pid, aid)
    assert link.status_code == 200
    url = link.json()["url"]
    assert url.startswith("/view/") and len(url) > len("/view/") + 40

    page = client.get(url)  # a new tab: no token, no Origin
    assert page.status_code == 200
    assert page.content == PAGE
    assert page.headers["content-type"] == "text/html"  # the file's own <meta charset> counts
    assert page.headers["content-security-policy"] == VIEW_CSP  # and not Lectern's as well
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "no-store"

    again = client.get(url)
    assert (again.status_code, again.text) == (404, GONE)


def test_the_sandbox_runs_the_pages_own_scripts_and_reaches_nothing():
    rules = dict(rule.strip().partition(" ")[::2] for rule in VIEW_CSP.split(";"))
    # Scripts run, in an origin of their own: no allow-same-origin, forms, popups or modals.
    assert rules["sandbox"] == "allow-scripts"
    assert rules["script-src"] == "'unsafe-inline' 'unsafe-eval'"
    assert rules["style-src"] == "'unsafe-inline'"
    # Nothing is fetched from any address: fetch, XHR and WebSocket fall back to 'none'.
    assert rules["default-src"] == "'none'"
    assert "connect-src" not in rules
    assert rules["img-src"] == "data: blob:"
    assert rules["font-src"] == "data:"
    assert rules["media-src"] == "data: blob:"
    for name in ("http", "https:", "'self'", "*", "allow-same-origin"):
        assert name not in VIEW_CSP
    assert (rules["base-uri"], rules["form-action"]) == ("'none'", "'none'")
    assert rules["frame-ancestors"] == "'none'"


def test_a_link_not_opened_within_its_minute_has_expired(api, clock):
    client, headers, _, pid = api
    url = _link(client, headers, pid, _import(client, headers, pid, "page.html")).json()["url"]

    clock.now += timedelta(seconds=VIEW_TICKET_SECONDS)

    assert client.get(url).status_code == 404


def test_a_made_up_link_opens_nothing_and_is_still_sandboxed(api):
    client, _, _, _ = api
    page = client.get("/view/not-a-ticket")
    assert (page.status_code, page.text) == (404, GONE)
    assert page.headers["content-security-policy"] == VIEW_CSP


def test_a_file_trashed_after_its_link_was_made_isnt_served(api):
    client, headers, app, pid = api
    aid = _import(client, headers, pid, "page.html")
    url = _link(client, headers, pid, aid).json()["url"]
    project = app.state.projects.get(LOCAL_USER, pid)
    with app.state.projects.db(project).begin() as session:
        files.trash(project.root, session, aid)

    assert (client.get(url).status_code, client.get(url).text) == (404, GONE)


def test_only_html_files_get_a_link(api):
    client, headers, _, pid = api
    aid = _import(client, headers, pid, "notes.txt", b"hello")
    refused = _link(client, headers, pid, aid)
    assert (refused.status_code, refused.json()) == (
        400,
        {"detail": "Only HTML files open as a page."},
    )
    assert _link(client, headers, pid, 999).status_code == 404


def test_a_link_needs_the_token_and_lecterns_own_origin(api):
    client, headers, _, pid = api
    aid = _import(client, headers, pid, "page.html")
    url = f"/api/projects/{pid}/artifacts/{aid}/view"

    assert client.post(url, headers={"Origin": BASE}).status_code == 401
    foreign = {**headers, "Origin": "https://evil.example"}
    assert client.post(url, headers=foreign).status_code == 403


def test_the_rest_of_lectern_keeps_its_own_policy(api):
    client, _, _, _ = api
    for path in ("/health", "/views", "/viewer/x"):
        assert client.get(path).headers["content-security-policy"] == CONTENT_SECURITY_POLICY
