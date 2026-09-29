"""Localhost security: Lectern listens on 127.0.0.1, but any website the user visits can
still send requests there. This middleware makes sure only Lectern's own page can use the API.

- Host check (HTTP and WebSocket): the Host header must be 127.0.0.1:<port> or
  localhost:<port>. This defeats DNS rebinding.
- Origin check: HTTP requests that change something, and every WebSocket upgrade, must come
  from Lectern's own origin. Browsers don't apply same-origin rules to WebSockets, so there
  the Origin header is required, not just checked when present.
- Token check: every /api request carries the per-install token, in the X-Lectern-Token
  header (HTTP) or as the `lectern.token.<token>` subprotocol (WebSocket, where browsers
  can't set headers).
- Every HTTP response gets a strict Content-Security-Policy.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from pathlib import Path
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

TOKEN_HEADER = "x-lectern-token"  # noqa: S105 (a header name, not a secret)
WS_TOKEN_PREFIX = "lectern.token."  # noqa: S105 (a subprotocol prefix, not a secret)
WS_SUBPROTOCOL = "lectern.v1"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

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


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def allowed_origins(port: int) -> frozenset[str]:
    return frozenset({f"http://{host}" for host in allowed_hosts(port)})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _ws_subprotocols(scope: Scope) -> Iterable[str]:
    return scope.get("subprotocols", ())


def _tokens_match(given: str | None, expected: str) -> bool:
    return given is not None and hmac.compare_digest(given.encode(), expected.encode())


class LocalGuard:
    """ASGI middleware applying the host, origin and token checks, and the CSP."""

    def __init__(self, app: ASGIApp, *, port: int, token: str) -> None:
        self.app = app
        self.hosts = allowed_hosts(port)
        self.origins = allowed_origins(port)
        self.token = token

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

        if _header(scope, b"host") not in self.hosts:
            await _plain_error(send_with_headers, 421, "Misdirected request")
            return
        origin = _header(scope, b"origin")
        foreign_origin = origin is not None and origin not in self.origins
        if scope["method"] in MUTATING_METHODS and foreign_origin:
            await _plain_error(send_with_headers, 403, "Origin not allowed")
            return
        if scope["path"].startswith("/api/") and not _tokens_match(
            _header(scope, TOKEN_HEADER.encode()), self.token
        ):
            await _plain_error(send_with_headers, 401, "Missing or wrong token")
            return
        await self.app(scope, receive, send_with_headers)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        token = next(
            (
                p.removeprefix(WS_TOKEN_PREFIX)
                for p in _ws_subprotocols(scope)
                if p.startswith(WS_TOKEN_PREFIX)
            ),
            None,
        )
        allowed = (
            _header(scope, b"host") in self.hosts
            and _header(scope, b"origin") in self.origins
            and _tokens_match(token, self.token)
        )
        if not allowed:
            # Closing before accepting makes the server answer the upgrade with 403.
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
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
