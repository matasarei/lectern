"""The FastAPI application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from lectern import __version__
from lectern.api import api_router
from lectern.catalog import catalog
from lectern.config import DEFAULT_PORT, Paths, get_paths
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.core.security import LocalGuard, load_or_create_token
from lectern.downloads.manager import DownloadManager
from lectern.web import WEB_DIST, mount_web


def create_app(
    paths: Paths | None = None,
    *,
    port: int = DEFAULT_PORT,
    downloads: DownloadManager | None = None,
    web_dist: Path = WEB_DIST,
) -> FastAPI:
    paths = paths or get_paths()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        entries = {m.id: m for m in catalog.load().models}
        app.state.downloads.resume_interrupted(entries)
        yield
        await app.state.downloads.stop_all()

    app = FastAPI(
        title="Lectern", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.paths = paths
    app.state.port = port
    app.state.token = load_or_create_token(paths.token_file)

    engine = make_engine(paths.app_db)
    migrate(engine)
    app.state.engine = engine
    app.state.sessions = make_sessionmaker(engine)
    app.state.downloads = downloads or DownloadManager(paths.models, app.state.sessions)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(api_router())
    mount_web(app, web_dist)  # last: it answers every path the API didn't
    app.add_middleware(LocalGuard, port=port, token=app.state.token)
    return app
