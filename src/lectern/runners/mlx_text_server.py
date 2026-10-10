"""MLX's text server, started with a key.

`mlx_lm.server` has no API key and answers every website (`Access-Control-Allow-Origin: *`), so
a page open in a browser could use the model. Lectern starts it through this module instead:

    python -m lectern.runners.mlx_text_server --model <folder> --host 127.0.0.1 --port <n> …

Every request then has to carry `Authorization: Bearer <key>`. The key comes in the process's
environment (LECTERN_MLX_KEY), never on its command line, where `ps` would show it to every
user. The arguments go to `mlx_lm.server` unchanged.

The guard changes the server's request handler class in place: `mlx_lm.server` builds its
handlers itself and takes no class from outside. That leans on the server's internals, so the
`mlx` extra pins mlx-lm to the versions this was read against, and
tests/runners/test_mlx_text_server.py fails where MLX is installed and the handler has moved.
"""

from __future__ import annotations

import hmac
import os
import sys
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler

KEY_ENV = "LECTERN_MLX_KEY"  # noqa: S105 (the variable's name, not a secret)


def guard(handler: type[BaseHTTPRequestHandler], key: str) -> None:
    """Make every request to `handler` need the bearer key: without it the answer is 401, with
    none of the handler's own headers. Every `do_*` method the class has is covered, so a
    method a later server adds isn't left open."""
    expected = f"Bearer {key}".encode()

    def needing_the_key(
        answer: Callable[[BaseHTTPRequestHandler], None],
    ) -> Callable[[BaseHTTPRequestHandler], None]:
        def checked(self: BaseHTTPRequestHandler) -> None:
            given = (self.headers.get("Authorization") or "").encode()
            if not hmac.compare_digest(given, expected):
                self.send_error(401)
                return
            answer(self)

        return checked

    for name in dir(handler):
        if name.startswith("do_"):
            setattr(handler, name, needing_the_key(getattr(handler, name)))


def main() -> None:
    # Taken out of the environment: nothing the server starts or logs should carry it.
    key = os.environ.pop(KEY_ENV, "")
    if not key:
        sys.exit(f"{KEY_ENV} isn't set. Lectern starts this server itself, with a key.")
    from mlx_lm import server  # only on a Mac with lectern-ai[mlx]

    guard(server.APIHandler, key)
    server.main()


if __name__ == "__main__":
    main()
