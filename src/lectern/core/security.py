"""Localhost security: Lectern listens on 127.0.0.1, but any website the user visits can
still send requests there. This middleware makes sure only Lectern's own page can use the API.

- Host check (HTTP and WebSocket): the Host header must be 127.0.0.1:<port> or
  localhost:<port>. This defeats DNS rebinding. Behind a reverse proxy or tunnel on the same
  machine, or listening on the network itself (LECTERN_HOST=0.0.0.0), the public URL it's
  reached by is set explicitly (LECTERN_PUBLIC_URL), and its host and origin are accepted
  too: exactly those, nothing near them.
- Origin check: HTTP requests that change something, and every WebSocket upgrade, must come
  from Lectern's own origin. Browsers don't apply same-origin rules to WebSockets, so there
  the Origin header is required, not just checked when present.
- Token check: every /api request carries a token, in the X-Lectern-Token header (HTTP) or
  as the `lectern.token.<token>` subprotocol (WebSocket, where browsers can't set headers).
- Every HTTP response gets a strict Content-Security-Policy.

Whose token: until anyone can sign in (`lectern user init`), the per-install one, which the
link `lectern url` prints carries, and whoever has it is the local user. Once there are
accounts, a browser is signed in by a cookie that names a login, and the token is that
login's: the cookie alone is what another site could make a browser send, so it is never
enough (this is the request-forgery defence). The per-install token then opens Lectern from
this computer only, as the admin.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import sys
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

TOKEN_HEADER = "x-lectern-token"  # noqa: S105 (a header name, not a secret)
WS_TOKEN_PREFIX = "lectern.token."  # noqa: S105 (a subprotocol prefix, not a secret)
WS_SUBPROTOCOL = "lectern.v1"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# The cookie that names a login. Over https it is bound to the host (the `__Host-` prefix: a
# browser accepts it only with Secure, no Domain and Path=/, so a sibling site or a plain-http
# page can't plant one).
LOGIN_COOKIE = "lectern"
HOST_BOUND = "__Host-"
# Reachable by a browser that isn't signed in: signing in, and asking whether it is.
SIGNING_IN = "/api/login"
THIS_COMPUTER = frozenset({"127.0.0.1", "::1"})

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
    "style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


def load_or_create_token(token_file: Path) -> str:
    """The per-install token: 32 random bytes, stored readable by the owner only."""
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(token_file, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    if sys.platform != "win32":
        token_file.chmod(0o600)
    return token


@dataclass(frozen=True)
class PublicUrl:
    host: str  # as the Host header carries it: "lectern.lan", "192.168.1.20:8443"
    origin: str  # as the Origin header carries it: "http://lectern.lan"


def parse_public_urls(value: str) -> list[PublicUrl]:
    """LECTERN_PUBLIC_URL: one or more `http(s)://host[:port]`, comma-separated. Anything
    else (a path, a user, a query, another scheme) is refused, so the guard can't be widened
    by accident."""
    urls = []
    for raw in (part.strip() for part in value.split(",")):
        if not raw:
            continue
        try:
            parts = urlsplit(raw.removesuffix("/"))
            port = parts.port  # raises for a port out of range
        except ValueError:
            parts, port = None, None
        if (
            parts is None
            or parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.path or parts.query or parts.fragment
            or parts.username or parts.password
        ):  # fmt: skip
            raise ValueError(
                f"LECTERN_PUBLIC_URL must be like http://lectern.lan or "
                f"https://192.168.1.20:8443, not {raw!r}."
            )
        name = parts.hostname.lower()
        if ":" in name:  # IPv6: browsers send it bracketed, Host: [fd00::5]:8080
            name = f"[{name}]"
        host = name + (f":{port}" if port is not None else "")
        urls.append(PublicUrl(host=host, origin=f"{parts.scheme}://{host}"))
    return urls


def allowed_hosts(port: int, public: Iterable[PublicUrl] = ()) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}", *(u.host for u in public)})


def allowed_origins(port: int, public: Iterable[PublicUrl] = ()) -> frozenset[str]:
    local = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    return frozenset({*local, *(u.origin for u in public)})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _host(scope: Scope) -> str | None:
    """The Host header; host names aren't case-sensitive."""
    host = _header(scope, b"host")
    return host.lower() if host is not None else None


def _ws_subprotocols(scope: Scope) -> Iterable[str]:
    return scope.get("subprotocols", ())


def _tokens_match(given: str | None, expected: str) -> bool:
    return given is not None and hmac.compare_digest(given.encode(), expected.encode())


def _cookie(scope: Scope, name: str) -> str | None:
    """The value of the cookie `name`, if the request carries it."""
    for part in (_header(scope, b"cookie") or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name and value:
            return value
    return None


def login_cookie(host: str | None, public: Iterable[PublicUrl]) -> tuple[str, bool]:
    """The name of the login cookie for a request to `host`, and whether it is Secure:
    host-bound where Lectern is reached over https, plain otherwise."""
    secure = any(u.host == host and u.origin.startswith("https://") for u in public)
    return (HOST_BOUND + LOGIN_COOKIE if secure else LOGIN_COOKIE), secure


class Logins(Protocol):
    """What the guard asks about accounts (`lectern.core.accounts.Logins`). Both read the
    database, so the guard calls them in a worker thread."""

    def accounts_on(self) -> bool: ...

    def find(self, login_id: str, *, seen: bool = True) -> tuple[Any, str] | None:
        """The user that login is, and its token. `seen`: whether asking counts as the
        browser using its login."""


@dataclass(frozen=True)
class Asker:
    """Who a request turned out to be from."""

    user: Any  # None: nobody
    token: str | None  # their login's token; None for the per-install link's holder
    accounts: bool  # whether people sign in here at all


class LocalGuard:
    """ASGI middleware applying the host, origin and token checks, and the CSP. Whoever a
    request to /api turns out to be is left in the connection's state (`state.user`)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        port: int,
        token: str,
        public: Iterable[PublicUrl] = (),
        logins: Logins | None = None,
        local_user: Any = None,
    ) -> None:
        self.app = app
        self.public = list(public)
        self.local_hosts = allowed_hosts(port)
        self.hosts = allowed_hosts(port, self.public)
        self.origins = allowed_origins(port, self.public)
        self.token = token
        self.logins = logins
        self.local_user = local_user  # who the per-install token's holder is

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "http":
            await self._http(scope, receive, send)
        elif kind == "websocket":
            await self._websocket(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *SECURITY_HEADERS]
            await send(message)

        if _host(scope) not in self.hosts:
            await _plain_error(send_with_headers, 421, "Misdirected request")
            return
        origin = _header(scope, b"origin")
        foreign_origin = origin is not None and origin not in self.origins
        if scope["method"] in MUTATING_METHODS and foreign_origin:
            await _plain_error(send_with_headers, 403, "Origin not allowed")
            return
        if scope["path"].startswith("/api/"):
            signing_in = scope["path"] == SIGNING_IN
            offered = _header(scope, TOKEN_HEADER.encode())
            who = await self._who(scope, offered, cookie_is_enough=signing_in)
            if who.user is None and not (who.accounts and signing_in):
                detail = "Not signed in" if who.accounts else "Missing or wrong token"
                await _plain_error(send_with_headers, 401, detail)
                return
            scope.setdefault("state", {}).update(user=who.user, login_token=who.token)
        await self.app(scope, receive, send_with_headers)

    async def _who(
        self,
        scope: Scope,
        offered: str | None,
        *,
        cookie_is_enough: bool = False,
        seen: bool = True,
    ) -> Asker:
        """Who the request is from. `cookie_is_enough`: for the one request that hands a
        signed-in browser its token. `seen`: whether asking counts as using the login."""
        if self.logins is None or not await asyncio.to_thread(self.logins.accounts_on):
            user = self.local_user if _tokens_match(offered, self.token) else None
            return Asker(user, None, accounts=False)
        name, _secure = login_cookie(_host(scope), self.public)
        login_id = _cookie(scope, name)
        if login_id:
            found = await asyncio.to_thread(self.logins.find, login_id, seen=seen)
            if found is not None and (cookie_is_enough or _tokens_match(offered, found[1])):
                return Asker(found[0], found[1], accounts=True)
        # The link: from this computer only. The Host header alone doesn't say so (anyone
        # reaching the port can send `Host: 127.0.0.1`), so the connection must be local too.
        client = scope.get("client") or ("", 0)
        here = _host(scope) in self.local_hosts and client[0] in THIS_COMPUTER
        if here and _tokens_match(offered, self.token):
            return Asker(self.local_user, None, accounts=True)
        return Asker(None, None, accounts=True)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        token = next(
            (
                p.removeprefix(WS_TOKEN_PREFIX)
                for p in _ws_subprotocols(scope)
                if p.startswith(WS_TOKEN_PREFIX)
            ),
            None,
        )
        user = None
        if _host(scope) in self.hosts and _header(scope, b"origin") in self.origins:
            user = (await self._who(scope, token)).user
        if user is None:
            # Closing before accepting makes the server answer the upgrade with 403.
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return

        # A socket stays open for hours: what it is let in on can end meanwhile (signed
        # out, a new password, the account disabled, accounts turned on). The route asks
        # again while it follows (`lectern.api.ws`); a socket left open isn't a use.
        async def still_signed_in() -> bool:
            return (await self._who(scope, token, seen=False)).user is not None

        scope.setdefault("state", {}).update(user=user, signed_in=still_signed_in)
        await self.app(scope, receive, send)


async def _plain_error(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
