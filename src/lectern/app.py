"""The FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI, WebSocket

from lectern import __version__
from lectern.config import DEFAULT_PORT, Paths, get_paths
from lectern.core.security import WS_SUBPROTOCOL, LocalGuard, load_or_create_token


def create_app(paths: Paths | None = None, *, port: int = DEFAULT_PORT) -> FastAPI:
    paths = paths or get_paths()
    app = FastAPI(title="Lectern", version=__version__, docs_url=None, redoc_url=None)
    app.state.paths = paths
    app.state.port = port
    app.state.token = load_or_create_token(paths.token_file)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.websocket("/api/ws/ping")
    async def ws_ping(websocket: WebSocket) -> None:
        await websocket.accept(subprotocol=WS_SUBPROTOCOL)
        await websocket.send_text("pong")
        await websocket.close()

    app.add_middleware(LocalGuard, port=port, token=app.state.token)
    return app
