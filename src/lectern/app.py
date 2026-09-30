"""The FastAPI application."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from lectern import __version__
from lectern.agent.core_tools import CORE_TOOLS
from lectern.agent.turns import TurnManager
from lectern.api import api_router
from lectern.catalog import catalog
from lectern.config import DEFAULT_PORT, Paths, get_paths
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.core.security import LocalGuard, load_or_create_token
from lectern.downloads.manager import DownloadManager
from lectern.hardware.probe import probe
from lectern.modules import EnvProbe, load_modules, tool_registry
from lectern.modules.ocr.jobs import OcrJobs
from lectern.modules.sandbox.container import Sandbox
from lectern.projects.store import ProjectStore
from lectern.runtime.supervisor import ModelRuntime
from lectern.runtime.wiring import build_runtime
from lectern.web import WEB_DIST, mount_web


def create_app(
    paths: Paths | None = None,
    *,
    port: int = DEFAULT_PORT,
    downloads: DownloadManager | None = None,
    runtime: ModelRuntime | None = None,
    web_dist: Path = WEB_DIST,
) -> FastAPI:
    paths = paths or get_paths()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        entries = {m.id: m for m in catalog.load().models}
        app.state.downloads.resume_interrupted(entries)
        yield
        await app.state.turns.stop_all()  # a reply in progress keeps what it wrote
        await app.state.ocr_jobs.stop_all()  # an OCR batch shows as paused when read again
        await app.state.runtime.stop_all()  # every llama-server process ends with the app
        await app.state.downloads.stop_all()
        app.state.projects.dispose()

    app = FastAPI(
        title="Lectern", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.paths = paths
    app.state.shutting_down = asyncio.Event()  # set by `lectern serve` on Ctrl+C
    app.state.port = port
    app.state.token = load_or_create_token(paths.token_file)

    engine = make_engine(paths.app_db)
    migrate(engine)
    app.state.engine = engine
    app.state.sessions = make_sessionmaker(engine)
    app.state.projects = ProjectStore(paths, app.state.sessions)
    app.state.downloads = downloads or DownloadManager(paths.models, app.state.sessions)
    app.state.runtime = runtime or build_runtime(
        paths, app.state.downloads, probe(), app.state.sessions
    )

    app.state.turns = TurnManager(
        app.state.runtime,
        app.state.projects,
        tool_registry(CORE_TOOLS, load_modules(), EnvProbe()),
        downloads=app.state.downloads,
        sandbox=Sandbox(),
    )

    app.state.ocr_jobs = OcrJobs(app.state.runtime, app.state.projects)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(api_router())
    mount_web(app, web_dist)  # last: it answers every path the API didn't
    app.add_middleware(LocalGuard, port=port, token=app.state.token)
    return app
