"""The FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI

from lectern import __version__
from lectern.api import api_router
from lectern.config import DEFAULT_PORT, Paths, get_paths
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.core.security import LocalGuard, load_or_create_token


def create_app(paths: Paths | None = None, *, port: int = DEFAULT_PORT) -> FastAPI:
    paths = paths or get_paths()
    app = FastAPI(title="Lectern", version=__version__, docs_url=None, redoc_url=None)
    app.state.paths = paths
    app.state.port = port
    app.state.token = load_or_create_token(paths.token_file)

    engine = make_engine(paths.app_db)
    migrate(engine)
    app.state.engine = engine
    app.state.sessions = make_sessionmaker(engine)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(api_router())
    app.add_middleware(LocalGuard, port=port, token=app.state.token)
    return app
