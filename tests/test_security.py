from __future__ import annotations

import stat
import sys

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lectern.app import create_app
from lectern.core.security import CONTENT_SECURITY_POLICY, load_or_create_token

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = BASE


@pytest.fixture
def app(paths):
    app = create_app(paths, port=PORT)

    @app.get("/api/echo")
    def echo_get() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/echo")
    def echo_post() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
def client(app):
    return TestClient(app, base_url=BASE)


@pytest.fixture
def token(app):
    return app.state.token


def test_api_without_token_is_401(client):
    assert client.get("/api/echo").status_code == 401


def test_api_with_wrong_token_is_401(client):
    assert client.get("/api/echo", headers={"X-Lectern-Token": "nope"}).status_code == 401


def test_api_with_token_passes(client, token):
    response = client.get("/api/echo", headers={"X-Lectern-Token": token})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/", "/health", "/api/echo"])
def test_foreign_host_is_421(client, token, path):
    response = client.get(path, headers={"Host": "evil.example", "X-Lectern-Token": token})
    assert response.status_code == 421


def test_localhost_name_is_allowed(app):
    client = TestClient(app, base_url=f"http://localhost:{PORT}")
    assert client.get("/health").status_code == 200


def test_post_with_foreign_origin_is_403(client, token):
    response = client.post(
        "/api/echo", headers={"X-Lectern-Token": token, "Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_post_with_own_origin_passes(client, token):
    response = client.post("/api/echo", headers={"X-Lectern-Token": token, "Origin": ORIGIN})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/health", "/api/echo", "/nowhere"])
def test_every_response_has_csp(client, path):
    response = client.get(path)
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"


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
