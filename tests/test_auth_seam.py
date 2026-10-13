from __future__ import annotations

import re

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


# The one path that can't ask for a user first: it is how someone becomes one.
OPEN_TO_ALL = {"/api/login"}


def test_every_api_http_route_requires_current_user(guarded):
    app, client, headers = guarded
    operations = [
        (path, method)
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/") and path not in OPEN_TO_ALL
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


# --- accounts: what a member may do, and whose things are whose -----------------------------

from datetime import UTC, datetime  # noqa: E402

from lectern.core import accounts  # noqa: E402

WEB = "https://lectern.example"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)  # the app's clock stands here
PASSWORD = "we walked to the lake in may"  # noqa: S105 (made up for the tests)

# Install-wide routes that change something: a member is refused each of them. Reads of the
# same things (which models exist, what is loaded) are everybody's.
ADMIN_ONLY = [
    ("put", "/api/models/roles/main"),
    ("post", "/api/models/qwen3.5-4b/download"),
    ("post", "/api/models/qwen3.5-4b/update"),
    ("delete", "/api/models/qwen3.5-4b"),
    ("post", "/api/runtime/unload"),
    ("put", "/api/runtime/settings"),
    ("post", "/api/first-run/engine"),
    ("post", "/api/first-run"),
    ("post", "/api/dev/swap-test"),
    ("get", "/api/dev/swap-test"),
]
EVERYBODYS = [
    "/api/models",
    "/api/runtime",
    "/api/runtime/settings",
    "/api/system",
    "/api/first-run",
]
# Install-wide routes that change something and are a member's too.
A_MEMBERS_TOO = {
    ("post", "/api/chat"),
    ("post", "/api/login"),
    ("post", "/api/logout"),
    ("post", "/api/password"),
}


@pytest.fixture
def family(paths):
    """Accounts on: the admin `yevhen`, the members `olena` and `taras`, each signed in from
    one browser. Returns (app, as_): as_(login) is (client, headers) for that person."""
    app = create_app(paths, public_urls=[WEB], dev=True, clock=lambda: NOW)
    sessions = app.state.sessions
    people = {
        "yevhen": accounts.init_admin(sessions, "yevhen", PASSWORD),
        "olena": accounts.add_user(sessions, "olena", PASSWORD),
        "taras": accounts.add_user(sessions, "taras", PASSWORD),
    }

    def as_(login: str):  # noqa: ANN202
        login_id, token = accounts.open_login(sessions, people[login].id, NOW)
        client = TestClient(app, base_url=WEB, cookies={"__Host-lectern": login_id})
        return client, {"X-Lectern-Token": token, "Origin": WEB}

    return app, as_


@pytest.mark.parametrize(("method", "path"), ADMIN_ONLY)
def test_a_member_is_refused_what_only_an_admin_may_change(family, method: str, path: str):
    _, as_ = family
    client, headers = as_("olena")
    refused = client.request(method.upper(), path, headers=headers, json={})
    assert refused.status_code == 403, f"{method.upper()} {path} is open to a member"
    assert refused.json() == {"detail": "Only an admin can do that."}

    admin, admin_headers = as_("yevhen")
    allowed = admin.request(method.upper(), path, headers=admin_headers, json={})
    assert allowed.status_code != 403  # past the check; what the route then says is its own


@pytest.mark.parametrize("path", EVERYBODYS)
def test_a_member_can_look_at_what_they_cant_change(family, path: str):
    _, as_ = family
    client, headers = as_("olena")
    assert client.get(path, headers=headers).status_code == 200


def test_no_install_wide_change_is_open_to_a_member_by_omission(family):
    """Every route that changes something and isn't inside a project is admin-only, unless
    it is listed above as a member's. A new one must be put in one list or the other."""
    app, as_ = family
    client, headers = as_("olena")
    changing = {"post", "put", "patch", "delete"}
    unlisted = []
    for path, item in app.openapi()["paths"].items():
        if not path.startswith("/api/") or "{project_id}" in path or path == "/api/projects":
            continue
        for method in set(item) & changing:
            if (method, path) in A_MEMBERS_TOO:
                continue
            url = re.sub(r"\{\w+\}", "x", path)
            status = client.request(method.upper(), url, headers=headers, json={}).status_code
            if status != 403:
                unlisted.append(f"{method.upper()} {path} -> {status}")
    assert unlisted == []


def test_everything_inside_a_project_is_its_owners_alone(family):
    """Every route under a project, asked for by someone else: the answer never comes from
    the project. (404 from the lookup; 422 where the made-up body is refused first.)"""
    app, as_ = family
    hers, her_headers = as_("olena")
    his, his_headers = as_("taras")
    pid = hers.post("/api/projects", json={"name": "Hers"}, headers=her_headers).json()["id"]
    assert his.get("/api/projects", headers=his_headers).json() == []  # not even listed

    leaks = []
    for path, item in app.openapi()["paths"].items():
        if "{project_id}" not in path:
            continue
        # The project is hers; every other value in the path is a made-up 1.
        url = re.sub(r"\{\w+\}", "1", path.replace("{project_id}", str(pid)))
        for method in item:
            status = his.request(method.upper(), url, headers=his_headers, json={}).status_code
            if status not in (404, 422):
                leaks.append(f"{method.upper()} {path} -> {status}")
    assert leaks == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/projects/{pid}", None),
        ("patch", "/api/projects/{pid}", {"name": "Mine now"}),
        ("get", "/api/projects/{pid}/items", None),
        ("get", "/api/projects/{pid}/sessions/1", None),
        ("post", "/api/projects/{pid}/sessions/1/messages", {"text": "Hello"}),
        ("post", "/api/projects/{pid}/sessions", {}),
    ],
)
def test_a_well_formed_request_for_someone_elses_project_finds_nothing(family, method, path, body):
    _, as_ = family
    hers, her_headers = as_("olena")
    his, his_headers = as_("taras")
    pid = hers.post("/api/projects", json={"name": "Hers"}, headers=her_headers).json()["id"]
    url = path.format(pid=pid)

    refused = his.request(method.upper(), url, headers=his_headers, json=body)
    assert (refused.status_code, refused.json()) == (404, {"detail": "No such project."})
    assert hers.request(method.upper(), url, headers=her_headers, json=body).status_code < 300


def test_someone_elses_sockets_close_as_not_found(family):
    app, as_ = family
    hers, her_headers = as_("olena")
    pid = hers.post("/api/projects", json={"name": "Hers"}, headers=her_headers).json()["id"]
    his, his_headers = as_("taras")
    cookie = his.cookies["__Host-lectern"]
    protocols = ["lectern.v1", f"lectern.token.{his_headers['X-Lectern-Token']}"]
    for path in (f"/api/projects/{pid}/events", f"/api/projects/{pid}/sessions/1/events",
                 f"/api/projects/{pid}/ocr/1/events"):  # fmt: skip
        with (
            TestClient(app, base_url=WEB).websocket_connect(
                path,
                headers={
                    "Host": "lectern.example",
                    "Origin": WEB,
                    "Cookie": f"__Host-lectern={cookie}",
                },
                subprotocols=protocols,
            ) as socket,
            pytest.raises(WebSocketDisconnect) as closed,
        ):
            socket.receive_json()
        assert (closed.value.code, closed.value.reason) == (4404, "No such project.")


def test_a_project_lands_in_its_owners_folder(family, paths):
    _, as_ = family
    for login, folder in (("olena", "olena"), ("yevhen", "local")):  # the admin was `local`
        client, headers = as_(login)
        made = client.post("/api/projects", json={"name": "Scans"}, headers=headers).json()
        assert made["folder"] == str(paths.home / "users" / folder / "projects" / "Scans")


def test_a_follow_up_runs_as_the_chats_owner(family):
    from lectern.agent.turns import _owner

    app, as_ = family
    client, headers = as_("olena")
    pid = client.post("/api/projects", json={"name": "Hers"}, headers=headers).json()["id"]
    with app.state.sessions() as session:
        from lectern.core.tables import UserRow

        olena = session.query(UserRow).filter_by(login="olena").one()
    project = app.state.projects.get(
        accounts.sign_in(app.state.sessions, "olena", PASSWORD, NOW), pid
    )

    owner = _owner(app.state.projects, app.state.projects.db(project), 1)
    assert (owner.id, owner.name, owner.role) == (olena.id, "olena", "member")
