from __future__ import annotations

import stat
import sys
import threading

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lectern.app import create_app
from lectern.core.security import CONTENT_SECURITY_POLICY, load_or_create_token

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = BASE


# Real endpoints: a GET that answers 200, and a POST that answers 404 once it gets past
# the guard (the model doesn't exist), so 403 can only come from the Origin check.
API_GET = "/api/models"
API_POST = "/api/models/no-such-model/download"


@pytest.fixture
def app(paths):
    return create_app(paths, port=PORT)


@pytest.fixture
def client(app):
    return TestClient(app, base_url=BASE)


@pytest.fixture
def token(app):
    return app.state.token


def test_api_without_token_is_401(client):
    assert client.get(API_GET).status_code == 401


def test_api_with_wrong_token_is_401(client):
    assert client.get(API_GET, headers={"X-Lectern-Token": "nope"}).status_code == 401


def test_api_with_token_passes(client, token):
    response = client.get(API_GET, headers={"X-Lectern-Token": token})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/", "/health", API_GET])
def test_foreign_host_is_421(client, token, path):
    response = client.get(path, headers={"Host": "evil.example", "X-Lectern-Token": token})
    assert response.status_code == 421


def test_localhost_name_is_allowed(app):
    client = TestClient(app, base_url=f"http://localhost:{PORT}")
    assert client.get("/health").status_code == 200


def test_post_with_foreign_origin_is_403(client, token):
    response = client.post(
        API_POST, headers={"X-Lectern-Token": token, "Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_post_with_own_origin_passes(client, token):
    response = client.post(API_POST, headers={"X-Lectern-Token": token, "Origin": ORIGIN})
    assert response.status_code == 404  # past the guard; the model just doesn't exist


@pytest.mark.parametrize("path", ["/health", API_GET, "/nowhere"])
def test_every_response_has_csp(client, path):
    response = client.get(path)
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_the_api_isnt_described_to_whoever_asks(client, path):
    """FastAPI serves a description of every route by default, outside /api and so to anyone
    who reaches the host. The page doesn't use it."""
    response = client.get(path)
    assert "openapi" not in response.text and "paths" not in response.text
    assert response.headers["content-type"].startswith("text/html")  # the page itself


def test_rejections_also_have_csp(client):
    response = client.get("/health", headers={"Host": "evil.example"})
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_token_is_stable_across_restarts(paths):
    first = load_or_create_token(paths.token_file)
    assert load_or_create_token(paths.token_file) == first
    assert len(first) >= 40


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_token_file_is_owner_only(paths):
    load_or_create_token(paths.token_file)
    assert stat.S_IMODE(paths.token_file.stat().st_mode) == 0o600


def _ws(client, *, origin, subprotocols, host=f"127.0.0.1:{PORT}"):
    # Starlette's TestClient sends "testserver" as the WebSocket Host unless told otherwise.
    headers = {"Host": host}
    if origin:
        headers["Origin"] = origin
    return client.websocket_connect("/api/ws/ping", headers=headers, subprotocols=subprotocols)


def test_ws_accepted_with_origin_and_token(client, token):
    with _ws(client, origin=ORIGIN, subprotocols=["lectern.v1", f"lectern.token.{token}"]) as ws:
        assert ws.accepted_subprotocol == "lectern.v1"
        assert ws.receive_text() == "pong"


@pytest.mark.parametrize(
    ("origin", "use_token"),
    [
        (None, True),  # no Origin at all
        ("https://evil.example", True),  # foreign Origin
        (ORIGIN, False),  # no token subprotocol
    ],
)
def test_ws_refused(client, token, origin, use_token):
    subprotocols = ["lectern.v1"] + ([f"lectern.token.{token}"] if use_token else [])
    with pytest.raises(WebSocketDisconnect), _ws(client, origin=origin, subprotocols=subprotocols):
        pass


def test_ws_foreign_host_refused(client, token):
    with (
        pytest.raises(WebSocketDisconnect),
        _ws(
            client,
            origin=ORIGIN,
            subprotocols=["lectern.v1", f"lectern.token.{token}"],
            host="evil.example",
        ),
    ):
        pass


def test_ws_wrong_token_refused(client):
    with (
        pytest.raises(WebSocketDisconnect),
        _ws(client, origin=ORIGIN, subprotocols=["lectern.v1", "lectern.token.nope"]),
    ):
        pass


# --- behind a reverse proxy: the public URL it's reached by (M4) ---------------------------

PUBLIC = "http://lectern.lan"


@pytest.fixture
def proxied(paths):
    app = create_app(paths, port=PORT, public_urls=[PUBLIC, "https://192.168.1.20:8443"])
    return app, TestClient(app, base_url=PUBLIC)


def test_the_public_host_and_origin_are_accepted_and_the_token_still_needed(proxied):
    app, client = proxied
    token = app.state.token
    assert client.get("/health").status_code == 200
    assert client.get(API_GET).status_code == 401  # still no way in without the token
    assert client.get(API_GET, headers={"X-Lectern-Token": token}).status_code == 200
    posted = client.post(API_POST, headers={"X-Lectern-Token": token, "Origin": PUBLIC})
    assert posted.status_code == 404  # past the guard
    upper = client.get("/health", headers={"Host": "Lectern.LAN"})
    assert upper.status_code == 200  # host names aren't case-sensitive
    with_port = client.get("/health", headers={"Host": "192.168.1.20:8443"})
    assert with_port.status_code == 200


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("lectern.lan:8080", PUBLIC),  # the right name on another port
        ("evil.example", PUBLIC),
        ("192.168.1.20", PUBLIC),  # the second URL's host without its port
        ("lectern.lan", "https://lectern.lan"),  # another scheme
        ("lectern.lan", "http://evil.example"),
    ],
)
def test_anything_else_is_still_refused(proxied, host, origin):
    app, client = proxied
    response = client.post(API_POST, headers={"Host": host, "Origin": origin,
                                              "X-Lectern-Token": app.state.token})  # fmt: skip
    assert response.status_code in (403, 421)


def test_websockets_take_the_public_url_too(proxied):
    app, client = proxied
    protocols = ["lectern.v1", f"lectern.token.{app.state.token}"]
    with _ws(client, origin=PUBLIC, subprotocols=protocols, host="lectern.lan") as ws:
        assert ws.receive_text() == "pong"
    foreign = _ws(client, origin="http://evil.example", subprotocols=protocols, host="lectern.lan")
    with pytest.raises(WebSocketDisconnect), foreign:
        pass


@pytest.mark.parametrize(
    "bad",
    ["lectern.lan", "ftp://lectern.lan", "http://lectern.lan/app", "http://user@lectern.lan",
     "http://lectern.lan?x=1", "http://", "http://lectern.lan:99999"],
)  # fmt: skip
def test_a_public_url_that_isnt_plain_scheme_host_port_is_refused(bad):
    from lectern.core.security import parse_public_urls

    with pytest.raises(ValueError, match="LECTERN_PUBLIC_URL"):
        parse_public_urls(bad)


def test_public_urls_are_read_comma_separated_and_a_trailing_slash_is_fine():
    from lectern.core.security import parse_public_urls

    urls = parse_public_urls(" http://lectern.lan/ , https://Lectern.Example:8443 ")
    assert [(u.host, u.origin) for u in urls] == [
        ("lectern.lan", "http://lectern.lan"),
        ("lectern.example:8443", "https://lectern.example:8443"),
    ]
    assert parse_public_urls("") == []


def test_an_ipv6_public_url_keeps_its_brackets_as_browsers_send_them():
    from lectern.core.security import parse_public_urls

    urls = parse_public_urls("http://[FD00::5]:8080, https://[fd00::6]")
    assert [(u.host, u.origin) for u in urls] == [
        ("[fd00::5]:8080", "http://[fd00::5]:8080"),
        ("[fd00::6]", "https://[fd00::6]"),
    ]


def test_on_every_address_the_guard_still_decides_by_host_and_token(paths, free_port):
    """`serve` with LECTERN_HOST=0.0.0.0: a real server, so the address it binds is real."""
    import threading

    import httpx2

    from lectern.cli import EVERY_ADDRESS, server_config

    lan = f"192.168.1.2:{free_port}"
    app = create_app(paths, port=free_port, public_urls=[f"http://{lan}"])
    server = uvicorn.Server(server_config(app, free_port, EVERY_ADDRESS))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            threading.Event().wait(0.02)
        assert server.started
        bound = {sock.getsockname()[0] for srv in server.servers for sock in srv.sockets}
        assert bound == {EVERY_ADDRESS}
        base = f"http://127.0.0.1:{free_port}"  # 0.0.0.0 includes the loopback
        assert httpx2.get(f"{base}/health", headers={"Host": lan}).status_code == 200
        assert httpx2.get(f"{base}/health").status_code == 200  # the local checks still work
        foreign = httpx2.get(f"{base}/health", headers={"Host": "evil.example"})
        assert foreign.status_code == 421
        assert httpx2.get(f"{base}{API_GET}", headers={"Host": lan}).status_code == 401
        token = {"X-Lectern-Token": app.state.token, "Host": lan}
        assert httpx2.get(f"{base}{API_GET}", headers=token).status_code == 200
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# --- accounts: once somebody can sign in, the page is a login's, not the link's -------------------

from datetime import UTC, datetime, timedelta  # noqa: E402

from lectern.core import accounts  # noqa: E402

WEB = "https://lectern.example"  # reached through a tunnel or a proxy with TLS
LAN = "http://lectern.lan"  # reached on the home network, without TLS
PASSWORD = "we walked to the lake in may"  # noqa: S105 (made up for the tests)
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)  # the app's clock stands here


@pytest.fixture
def family(paths):
    """An install with accounts on, and one browser signed in as the member `olena`.
    Returns (app, login_id, token)."""
    app = create_app(paths, port=PORT, public_urls=[WEB, LAN], clock=lambda: NOW)
    accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
    olena = accounts.add_user(app.state.sessions, "olena", PASSWORD)
    login_id, token = accounts.open_login(app.state.sessions, olena.id, NOW)
    return app, login_id, token


def _browser(app, base: str, cookie: dict | None = None, *, at: str = "203.0.113.7") -> TestClient:
    return TestClient(app, base_url=base, cookies=cookie, client=(at, 50000))


def _projects_of(app, login: str) -> list[str]:
    from lectern.core.tables import ProjectRow, UserRow

    with app.state.sessions() as session:
        owner = session.query(UserRow).filter_by(login=login).one()
        return [p.name for p in session.query(ProjectRow).filter_by(owner_id=owner.id)]


def test_with_accounts_on_a_public_address_needs_a_login(family):
    app, _, _ = family
    for base in (WEB, LAN):
        refused = _browser(app, base).get(API_GET)
        assert (refused.status_code, refused.json()) == (401, {"detail": "Not signed in"})
        # The per-install link opened everything before; from outside it opens nothing now.
        linked = _browser(app, base).get(API_GET, headers={"X-Lectern-Token": app.state.token})
        assert linked.status_code == 401


def test_a_login_is_its_cookie_and_its_token_together(family):
    app, login_id, token = family
    cookie = {"__Host-lectern": login_id}

    signed_in = _browser(app, WEB, cookie).get(API_GET, headers={"X-Lectern-Token": token})
    assert signed_in.status_code == 200
    # The cookie alone is what another site could make a browser send: not enough.
    assert _browser(app, WEB, cookie).get(API_GET).status_code == 401
    wrong = _browser(app, WEB, cookie).get(API_GET, headers={"X-Lectern-Token": "nope"})
    assert wrong.status_code == 401
    # Nor the token alone, nor with a cookie nobody was given.
    assert _browser(app, WEB).get(API_GET, headers={"X-Lectern-Token": token}).status_code == 401
    forged = _browser(app, WEB, {"__Host-lectern": "made-up"})
    assert forged.get(API_GET, headers={"X-Lectern-Token": token}).status_code == 401


def test_what_a_signed_in_person_does_is_done_as_them(family):
    app, login_id, token = family
    browser = _browser(app, WEB, {"__Host-lectern": login_id})
    made = browser.post(
        "/api/projects", json={"name": "Hers"}, headers={"X-Lectern-Token": token, "Origin": WEB}
    )
    assert made.status_code == 201
    assert _projects_of(app, "olena") == ["Hers"] and _projects_of(app, "yevhen") == []


def test_over_https_only_the_host_bound_cookie_counts(family):
    app, login_id, token = family
    headers = {"X-Lectern-Token": token}
    # A cookie a sibling site or plain http could have planted has the plain name.
    assert (
        _browser(app, WEB, {"lectern": login_id}).get(API_GET, headers=headers).status_code == 401
    )
    # Without TLS (the home network) there is no host-bound cookie: the plain one is the login.
    assert (
        _browser(app, LAN, {"lectern": login_id}).get(API_GET, headers=headers).status_code == 200
    )


def test_a_login_past_its_time_opens_nothing(family):
    app, _, _ = family
    with app.state.sessions() as session:
        from lectern.core.tables import UserRow

        olena = session.query(UserRow).filter_by(login="olena").one()
    old_id, old_token = accounts.open_login(app.state.sessions, olena.id, NOW - timedelta(days=31))
    browser = _browser(app, WEB, {"__Host-lectern": old_id})
    assert browser.get(API_GET, headers={"X-Lectern-Token": old_token}).status_code == 401


def test_the_link_still_opens_lectern_on_this_computer_as_the_admin(family):
    app, _, _ = family
    here = _browser(app, BASE, at="127.0.0.1")
    made = here.post(
        "/api/projects",
        json={"name": "Mine"},
        headers={"X-Lectern-Token": app.state.token, "Origin": ORIGIN},
    )
    assert made.status_code == 201
    assert _projects_of(app, "yevhen") == ["Mine"]


def test_the_link_is_refused_once_its_account_is_disabled(family):
    """The link signs in as the account `lectern user init` made (user 1). Another admin can
    disable it; the link then lets nobody in, as a password wouldn't. The way back is at the
    terminal: `lectern user enable`."""
    app, _, _ = family
    accounts.add_user(app.state.sessions, "anna", PASSWORD, admin=True)
    accounts.set_disabled(app.state.sessions, "yevhen", True)
    here = _browser(app, BASE, at="127.0.0.1")
    link = {"X-Lectern-Token": app.state.token}

    assert here.get(API_GET, headers=link).status_code == 401

    accounts.set_disabled(app.state.sessions, "yevhen", False)
    assert here.get(API_GET, headers=link).status_code == 200


def test_the_link_is_refused_to_another_computer_that_names_this_one(family):
    """Listening on the network, a request can carry `Host: 127.0.0.1:8765` from anywhere:
    the link is this computer's only when the connection is too."""
    app, _, _ = family
    elsewhere = _browser(app, BASE, at="192.168.1.50")
    assert elsewhere.get(API_GET, headers={"X-Lectern-Token": app.state.token}).status_code == 401


def test_signing_in_is_reachable_without_a_login(family):
    app, _, _ = family
    for method in ("GET", "POST"):
        response = _browser(app, WEB).request(method, "/api/login", headers={"Origin": WEB})
        assert response.status_code != 401  # past the guard (the route itself comes later)
    assert _browser(app, WEB).get("/api/login/extra").status_code == 401  # only that path


def test_a_socket_needs_the_login_too(family):
    app, login_id, token = family

    def follow(cookie: str | None, offered: str | None, *, name: str = "__Host-lectern"):  # noqa: ANN202
        headers = {"Host": "lectern.example", "Origin": WEB}
        if cookie:
            headers["Cookie"] = f"{name}={cookie}"
        protocols = ["lectern.v1"] + ([f"lectern.token.{offered}"] if offered else [])
        client = TestClient(app, base_url=WEB, client=("203.0.113.7", 50000))
        return client.websocket_connect(
            "/api/runtime/events", headers=headers, subprotocols=protocols
        )

    with follow(login_id, token) as socket:
        assert socket.receive_json()["type"] == "state"
    for cookie, offered, name in [
        (None, token, "__Host-lectern"),  # no cookie
        (login_id, None, "__Host-lectern"),  # no token
        (login_id, "nope", "__Host-lectern"),
        (None, app.state.token, "__Host-lectern"),  # the link, from outside
        (login_id, token, "lectern"),  # the plain cookie, over https
    ]:
        with pytest.raises(WebSocketDisconnect), follow(cookie, offered, name=name):
            pass


def _follow(app, cookie: str, token: str, *, host: str = "lectern.example", origin: str = WEB):  # noqa: ANN202
    """The runtime's events, followed as a signed-in browser does."""
    client = TestClient(app, base_url=origin, client=("203.0.113.7", 50000))
    return client.websocket_connect(
        "/api/runtime/events",
        headers={"Host": host, "Origin": origin, "Cookie": f"__Host-lectern={cookie}"},
        subprotocols=["lectern.v1", f"lectern.token.{token}"],
    )


@pytest.mark.parametrize(
    "end",
    [
        lambda sessions, login_id: accounts.end_login(sessions, login_id),  # signed out
        lambda sessions, login_id: accounts.set_password(sessions, "olena", PASSWORD[::-1]),
        lambda sessions, login_id: accounts.set_disabled(sessions, "olena", True),
    ],
    ids=["signed out", "a new password", "disabled"],
)
def test_a_socket_is_closed_once_its_login_has_ended(family, end):
    """Also when it ended in another process (`lectern user disable`): the socket asks."""
    app, login_id, token = family
    app.state.login_check_seconds = 0.01
    with _follow(app, login_id, token) as socket:
        assert socket.receive_json()["type"] == "state"
        end(app.state.sessions, login_id)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert (closed.value.code, closed.value.reason) == (4401, "Not signed in")


def test_an_http_event_stream_ends_once_its_login_has_ended(paths, free_port, live_server):
    """As a socket does, though no event comes to wake it: it asks on its own."""
    import httpx2

    app = create_app(paths, port=free_port, public_urls=[WEB], clock=lambda: NOW,
                     login_check_seconds=0.01)  # fmt: skip
    accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
    olena = accounts.add_user(app.state.sessions, "olena", PASSWORD)
    login_id, token = accounts.open_login(app.state.sessions, olena.id, NOW)
    headers = {"Host": "lectern.example", "Cookie": f"__Host-lectern={login_id}",
               "X-Lectern-Token": token}  # fmt: skip

    with (
        live_server(app, free_port) as base,
        httpx2.Client(base_url=base, timeout=5) as client,
        client.stream("GET", "/api/runtime/events", headers=headers) as stream,
    ):
        lines = stream.iter_lines()
        assert next(lines) == ": connected"
        accounts.end_login(app.state.sessions, login_id)
        rest = list(lines)  # a stream still open would time out here instead
    assert not any(line.startswith(": keepalive") for line in rest)


class _Clock:
    """The app's clock, set by the test; `read` says it has been asked the time."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.read = threading.Event()

    def __call__(self) -> datetime:
        self.read.set()
        return self.now


def test_an_open_socket_doesnt_count_as_using_the_login(paths):
    """A tab left open isn't someone at the computer: its socket asks whether the login still
    stands without marking it used, so a week unused still ends it."""
    from lectern.core.tables import LoginRow

    clock = _Clock(NOW)
    app = create_app(paths, port=PORT, public_urls=[WEB], clock=clock, login_check_seconds=0.01)
    accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
    olena = accounts.add_user(app.state.sessions, "olena", PASSWORD)
    login_id, token = accounts.open_login(app.state.sessions, olena.id, NOW)

    def last_seen() -> datetime:
        with app.state.sessions() as session:
            return session.query(LoginRow).one().last_seen_at.replace(tzinfo=UTC)

    clock.now = NOW + timedelta(days=6)
    with _follow(app, login_id, token) as socket:
        assert socket.receive_json()["type"] == "state"
        assert last_seen() == NOW + timedelta(days=6)  # opening it was a use
        clock.now = NOW + timedelta(days=12)
        for _ in range(2):  # asked twice at day 12: the first question has been answered
            clock.read.clear()
            assert clock.read.wait(5)
        assert last_seen() == NOW + timedelta(days=6)

        clock.now = NOW + timedelta(days=13, seconds=1)  # a week since it was last used
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 4401


def _follow_with_the_link(app, *, here: bool):  # noqa: ANN001, ANN202
    """The runtime's events, followed with the per-install token: from this computer, or
    from elsewhere through the public address."""
    origin = BASE if here else WEB
    host = f"127.0.0.1:{PORT}" if here else "lectern.example"
    address = "127.0.0.1" if here else "203.0.113.7"
    return TestClient(app, base_url=origin, client=(address, 50000)).websocket_connect(
        "/api/runtime/events",
        headers={"Host": host, "Origin": origin},
        subprotocols=["lectern.v1", f"lectern.token.{app.state.token}"],
    )


def test_a_socket_opened_with_the_link_elsewhere_is_closed_when_accounts_are_turned_on(paths):
    """Before accounts the per-install token opens Lectern at every address it is served at;
    once they are on, on this computer only. A socket already open from elsewhere goes the
    way a new one would: it is asked the guard's own question, not only about a login."""
    app = create_app(paths, port=PORT, public_urls=[WEB], login_check_seconds=0.01)
    with _follow_with_the_link(app, here=False) as socket:
        assert socket.receive_json()["type"] == "state"
        accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert (closed.value.code, closed.value.reason) == (4401, "Not signed in")


def test_a_socket_opened_with_the_link_on_this_computer_stays_when_accounts_are_turned_on(
    paths, monkeypatch
):
    """The link goes on working here, so its socket has no login to ask about and stays."""
    asked = threading.Event()
    accounts_on = accounts.accounts_on

    def counted(sessions):  # noqa: ANN001, ANN202
        answer = accounts_on(sessions)
        asked.set()
        return answer

    monkeypatch.setattr(accounts, "accounts_on", counted)
    app = create_app(paths, port=PORT, public_urls=[WEB], login_check_seconds=0.01)
    with _follow_with_the_link(app, here=True) as socket:
        assert socket.receive_json()["type"] == "state"
        accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
        for _ in range(2):  # asked twice since: the first of them has been answered
            asked.clear()
            assert asked.wait(5)
        socket.portal.call(app.state.runtime.events.publish, {"type": "still", "n": 1})
        assert socket.receive_json() == {"type": "still", "n": 1}


def test_a_socket_opened_with_the_link_on_this_computer_is_closed_once_its_account_is_disabled(
    paths,
):
    """The link's socket asks the guard again, as any socket does: once another admin has
    disabled the account the link signs in as, it is let in no more."""
    app = create_app(paths, port=PORT, public_urls=[WEB], login_check_seconds=0.01)
    accounts.init_admin(app.state.sessions, "yevhen", PASSWORD)
    accounts.add_user(app.state.sessions, "anna", PASSWORD, admin=True)
    with _follow_with_the_link(app, here=True) as socket:
        assert socket.receive_json()["type"] == "state"
        accounts.set_disabled(app.state.sessions, "yevhen", True)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert (closed.value.code, closed.value.reason) == (4401, "Not signed in")
