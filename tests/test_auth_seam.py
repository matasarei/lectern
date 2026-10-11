from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from fastapi.routing import APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lectern.app import create_app
from lectern.core.auth import LOCAL_USER, current_user

BASE = "http://127.0.0.1:8765"
SENTINEL = 418


def _refuse() -> None:
    raise HTTPException(status_code=SENTINEL)


@pytest.fixture
def guarded(paths):
    """The app with current_user replaced by something that always refuses."""
    app = create_app(paths)
    app.dependency_overrides[current_user] = _refuse
    return app, TestClient(app, base_url=BASE), {"X-Lectern-Token": app.state.token}


def test_every_api_http_route_requires_current_user(guarded):
    app, client, headers = guarded
    operations = [
        (path, method)
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/")
        for method in item
    ]
    for path, method in operations:
        url = path.replace("{", "").replace("}", "")  # placeholder values are enough here
        response = client.request(method.upper(), url, headers={**headers, "Origin": BASE})
        assert response.status_code == SENTINEL, f"{method.upper()} {path} skips current_user"


# Every WebSocket route: the ping, and the five event streams the page follows. A socket
# isn't in the OpenAPI document, so they are listed; the count below says when one is missing.
SOCKETS = [
    "/api/ws/ping",
    "/api/runtime/events",
    "/api/models/events",
    "/api/projects/1/events",
    "/api/projects/1/sessions/1/events",
    "/api/projects/1/ocr/1/events",
]


def _socket_routes(routes) -> list[APIWebSocketRoute]:  # noqa: ANN001
    """The WebSocket routes under `routes`, through the routers included in each other."""
    found: list[APIWebSocketRoute] = []
    for route in routes:
        if isinstance(route, APIWebSocketRoute):
            found.append(route)
        included = getattr(route, "original_router", None)
        if included is not None:
            found.extend(_socket_routes(included.routes))
    return found


def test_every_api_websocket_requires_current_user(guarded):
    app, client, _ = guarded
    assert len(_socket_routes(app.routes)) == len(SOCKETS), "a socket route isn't in SOCKETS"
    for path in SOCKETS:
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                path,
                headers={"Host": "127.0.0.1:8765", "Origin": BASE},
                subprotocols=["lectern.v1", f"lectern.token.{app.state.token}"],
            ),
        ):
            pytest.fail(f"{path} skips current_user")


def test_local_user_exists(paths):
    app = create_app(paths)
    with app.state.engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id, name FROM users")).all()
    assert [tuple(r) for r in rows] == [(LOCAL_USER.id, LOCAL_USER.name)]


def test_projects_table_has_owner_and_root_columns(paths):
    app = create_app(paths)
    columns = {c["name"]: c for c in sa.inspect(app.state.engine).get_columns("projects")}
    assert {"owner_id", "root_kind", "root_path"} <= columns.keys()
    fks = sa.inspect(app.state.engine).get_foreign_keys("projects")
    assert any(
        fk["referred_table"] == "users" and fk["constrained_columns"] == ["owner_id"] for fk in fks
    )


def test_root_kind_only_accepts_managed_or_linked(paths):
    app = create_app(paths)
    insert = sa.text(
        "INSERT INTO projects (owner_id, name, root_kind, root_path, created_at) "
        "VALUES (1, 'x', :kind, '/tmp/x', CURRENT_TIMESTAMP)"
    )
    with app.state.engine.begin() as conn:
        conn.execute(insert, {"kind": "linked"})
    try:
        with app.state.engine.begin() as conn:
            conn.execute(insert, {"kind": "elsewhere"})
    except sa.exc.IntegrityError:
        pass
    else:
        raise AssertionError("root_kind accepted an unknown value")


def test_migrations_are_idempotent_across_restarts(paths):
    create_app(paths)
    app = create_app(paths)
    with app.state.engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users")).scalar() == 1


def test_sqlite_runs_in_wal_mode_with_foreign_keys(paths):
    app = create_app(paths)
    with app.state.engine.connect() as conn:
        assert conn.execute(sa.text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(sa.text("PRAGMA foreign_keys")).scalar() == 1
