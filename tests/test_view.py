"""An HTML file opened as a page in a tab of its own: the one-use link, and the sandbox it is
served in. (Someone else's file answers 404 at the POST: the sweep over every project route
in test_auth_seam.py covers it.)"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from lectern.api.view import GONE, VIEW_TICKET_SECONDS
from lectern.app import create_app
from lectern.core.auth import LOCAL_USER
from lectern.core.security import CONTENT_SECURITY_POLICY, VIEW_CSP, VIEW_WRAPPER_CSP
from lectern.projects import files, html_page
from lectern.projects.tables import ArtifactRow

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


FRAME = re.compile(r'<iframe sandbox="allow-scripts" src="(/view/[\w-]+/page)"></iframe>')


def _frame_src(wrapper) -> str:
    assert wrapper.status_code == 200, wrapper.text
    found = FRAME.search(wrapper.text)
    assert found, wrapper.text
    return found.group(1)


def _open(client, url):
    """What a browser gets for the file tab's link: the wrapper, then the page in its frame."""
    return client.get(_frame_src(client.get(url)))


def test_an_html_file_opens_once_sandboxed_with_its_own_bytes(api):
    client, headers, _, pid = api
    aid = _import(client, headers, pid, "page.html")

    link = _link(client, headers, pid, aid)
    assert link.status_code == 200
    url = link.json()["url"]
    assert url.startswith("/view/") and len(url) > len("/view/") + 40

    wrapper = client.get(url)  # a new tab: no token, no Origin
    assert wrapper.headers["content-type"] == "text/html; charset=utf-8"
    assert wrapper.headers["content-security-policy"] == VIEW_WRAPPER_CSP  # not Lectern's too
    assert wrapper.headers["cache-control"] == "no-store"
    assert "<title>page.html</title>" in wrapper.text
    frame = _frame_src(wrapper)

    page = client.get(frame)  # the wrapper's frame: no token, no Origin either
    assert page.status_code == 200
    assert page.content == PAGE
    assert page.headers["content-type"] == "text/html"  # the file's own <meta charset> counts
    assert page.headers["content-security-policy"] == VIEW_CSP  # and not Lectern's as well
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "no-store"

    for used in (url, frame):
        again = client.get(used)
        assert (again.status_code, again.text) == (404, GONE)


def test_the_wrapper_holds_the_page_in_a_sandboxed_frame_named_for_the_file(api):
    client, headers, _, pid = api
    aid = _import(client, headers, pid, "R&D notes.html")

    wrapper = client.get(_link(client, headers, pid, aid).json()["url"])

    assert "<title>R&amp;D notes.html</title>" in wrapper.text
    assert _frame_src(wrapper).startswith("/view/")
    assert "<script" not in wrapper.text and "allow-same-origin" not in wrapper.text


def test_each_ticket_opens_only_what_it_was_made_for(api):
    client, headers, _, pid = api
    aid = _import(client, headers, pid, "page.html")
    tab = _link(client, headers, pid, aid).json()["url"]

    # The file tab's ticket can't open the page outside the wrapper, where it could navigate.
    assert client.get(f"{tab}/page").status_code == 404
    assert client.get(tab).status_code == 404  # and is spent by trying

    frame = _frame_src(client.get(_link(client, headers, pid, aid).json()["url"]))
    wrapper_by_frame = frame.removesuffix("/page")
    assert (client.get(wrapper_by_frame).status_code, client.get(frame).status_code) == (404, 404)


def test_a_frames_ticket_expires_like_the_tabs(api, clock):
    client, headers, _, pid = api
    url = _link(client, headers, pid, _import(client, headers, pid, "page.html")).json()["url"]
    frame = _frame_src(client.get(url))

    clock.now += timedelta(seconds=VIEW_TICKET_SECONDS)

    assert client.get(frame).status_code == 404


def test_the_sandbox_runs_the_pages_own_scripts_and_reaches_nothing():
    rules = dict(rule.strip().partition(" ")[::2] for rule in VIEW_CSP.split(";"))
    # Scripts run, in an origin of their own: no allow-same-origin, forms, popups or modals.
    assert rules["sandbox"] == "allow-scripts"
    assert rules["script-src"] == "'unsafe-inline' 'unsafe-eval' data:"
    assert rules["style-src"] == "'unsafe-inline'"
    # Nothing is fetched from any address: fetch, XHR and WebSocket fall back to 'none'.
    assert rules["default-src"] == "'none'"
    assert "connect-src" not in rules
    assert rules["img-src"] == "data: blob:"
    assert rules["font-src"] == "data:"
    assert rules["media-src"] == "data: blob:"
    for name in ("http", "https:", "*", "allow-same-origin"):
        assert name not in VIEW_CSP
    # 'self' only says which frame may hold it; no rule loads anything from Lectern.
    assert [d for d, sources in rules.items() if "'self'" in sources] == ["frame-ancestors"]
    assert (rules["base-uri"], rules["form-action"]) == ("'none'", "'none'")
    assert rules["frame-ancestors"] == "'self'"  # only inside the wrapper


def test_the_wrapper_runs_nothing_and_frames_only_lectern():
    rules = dict(rule.strip().partition(" ")[::2] for rule in VIEW_WRAPPER_CSP.split(";"))
    assert rules["default-src"] == "'none'"
    assert "script-src" not in rules  # no scripts of its own: default-src 'none'
    # Its frame's navigations go to Lectern's address or nowhere. Not sandboxed itself, so
    # that 'self' means Lectern; it runs nothing, so that costs nothing.
    assert rules["frame-src"] == "'self'"
    assert "sandbox" not in rules
    assert rules["style-src"] == "'unsafe-inline'"
    assert (rules["frame-ancestors"], rules["base-uri"], rules["form-action"]) == (
        "'none'",
        "'none'",
        "'none'",
    )


def test_a_link_not_opened_within_its_minute_has_expired(api, clock):
    client, headers, _, pid = api
    url = _link(client, headers, pid, _import(client, headers, pid, "page.html")).json()["url"]

    clock.now += timedelta(seconds=VIEW_TICKET_SECONDS)

    assert client.get(url).status_code == 404


@pytest.mark.parametrize(
    ("path", "policy"),
    [
        ("/view/not-a-ticket", VIEW_WRAPPER_CSP),
        ("/view/not-a-ticket/page", VIEW_CSP),
        ("/view/not/a/ticket", VIEW_WRAPPER_CSP),  # anything else: the policy that runs nothing
    ],
)
def test_a_made_up_link_opens_nothing_and_is_still_sandboxed(api, path, policy):
    client, _, _, _ = api
    page = client.get(path)
    assert page.status_code == 404
    assert page.headers["content-security-policy"] == policy


def test_a_file_trashed_after_its_link_was_made_isnt_served(api):
    client, headers, app, pid = api
    aid = _import(client, headers, pid, "page.html")
    url = _link(client, headers, pid, aid).json()["url"]
    project = app.state.projects.get(LOCAL_USER, pid)
    with app.state.projects.db(project).begin() as session:
        files.trash(project.root, session, aid)

    assert (client.get(url).status_code, client.get(url).text) == (404, GONE)


def test_a_file_trashed_while_its_wrapper_opens_isnt_framed(api):
    client, headers, app, pid = api
    aid = _import(client, headers, pid, "page.html")
    frame = _frame_src(client.get(_link(client, headers, pid, aid).json()["url"]))
    project = app.state.projects.get(LOCAL_USER, pid)
    with app.state.projects.db(project).begin() as session:
        files.trash(project.root, session, aid)

    assert (client.get(frame).status_code, client.get(frame).text) == (404, GONE)


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


SIDE_PAGE = (
    b'<!doctype html><head><link rel="stylesheet" href="style.css">'
    b'<script src="app.js" defer></script></head><body><img src="logo.png"></body>'
)


def _project(app, pid):
    return app.state.projects.get(LOCAL_USER, pid)


def test_a_page_comes_with_the_files_it_links_to_inside_it(api):
    client, headers, app, pid = api
    _import(client, headers, pid, "style.css", b"h1{color:red}")
    _import(client, headers, pid, "app.js", b"document.title = 'drawn';")
    _import(client, headers, pid, "logo.png", b"\x89PNG\r\n\x1a\nlogo")
    aid = _import(client, headers, pid, "page.html", SIDE_PAGE)

    page = _open(client, _link(client, headers, pid, aid).json()["url"])

    assert page.status_code == 200
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    assert page.headers["content-security-policy"] == VIEW_CSP
    assert page.headers["cache-control"] == "no-store"
    body = page.text
    assert "<style>h1{color:red}</style>" in body
    script = base64.b64encode(b"document.title = 'drawn';").decode()
    assert f'<script src="data:text/javascript;base64,{script}" defer>' in body
    assert "data:image/png;base64," in body
    assert 'href="style.css"' not in body and 'src="logo.png"' not in body


def test_only_the_owners_files_still_in_the_project_go_in(api):
    client, headers, app, pid = api
    project = _project(app, pid)
    (project.root.parent / "outside.css").write_bytes(b"p{color:blue}")
    _import(client, headers, pid, "notes.md", b"# secret")
    trashed = _import(client, headers, pid, "trashed.css", b"p{color:green}")
    theirs = _import(client, headers, pid, "theirs.css", b"p{color:pink}")
    (project.root / "loose.css").write_bytes(b"p{color:gray}")  # on disk, never imported
    with app.state.projects.db(project).begin() as session:
        files.trash(project.root, session, trashed)
        session.get(ArtifactRow, theirs).owner_id = LOCAL_USER.id + 1
    other = client.post("/api/projects", json={"name": "Other"}, headers=headers).json()["id"]
    _import(client, headers, other, "style.css", b"p{color:red}")
    other_dir = _project(app, other).root.name
    links = [
        "../outside.css",
        "notes.md",
        "trashed.css",
        "theirs.css",
        "loose.css",
        f"../{other_dir}/style.css",
        "../../../../etc/hosts.css",
        ".lectern/project.sqlite.css",
    ]
    page = "".join(f'<link rel="stylesheet" href="{href}">' for href in links)
    aid = _import(client, headers, pid, "page.html", page.encode())

    served = _open(client, _link(client, headers, pid, aid).json()["url"])

    assert served.status_code == 200
    assert served.content == page.encode()  # nothing went in: the raw bytes, as before
    assert served.headers["content-type"] == "text/html"


def test_a_side_file_over_the_limit_stays_out(api, monkeypatch):
    client, headers, _, pid = api
    monkeypatch.setattr(html_page, "FILE_LIMIT", 4)
    _import(client, headers, pid, "style.css", b"h1{color:red}")
    aid = _import(client, headers, pid, "page.html", b'<link rel="stylesheet" href="style.css">')

    served = _open(client, _link(client, headers, pid, aid).json()["url"])

    assert served.content == b'<link rel="stylesheet" href="style.css">'


def test_a_page_too_big_to_parse_is_sent_as_it_is(api, monkeypatch):
    client, headers, _, pid = api
    _import(client, headers, pid, "style.css", b"h1{color:red}")
    page = b'<link rel="stylesheet" href="style.css">'
    monkeypatch.setattr(html_page, "PAGE_LIMIT", len(page) - 1)
    aid = _import(client, headers, pid, "page.html", page)

    served = _open(client, _link(client, headers, pid, aid).json()["url"])

    assert (served.content, served.headers["content-type"]) == (page, "text/html")
