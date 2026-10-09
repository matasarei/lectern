"""The FastAPI application."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from lectern import __version__
from lectern.agent.core_tools import CORE_TOOLS
from lectern.agent.export_tools import export_tools
from lectern.agent.turns import TurnManager
from lectern.api import api_router
from lectern.catalog import catalog
from lectern.config import DEFAULT_PORT, Paths, get_paths
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.core.security import LocalGuard, load_or_create_token, parse_public_urls
from lectern.downloads.manager import DownloadManager
from lectern.hardware.probe import probe
from lectern.modules import (
    EnvProbe,
    load_modules,
    module_exporters,
    module_routers,
    tool_registry,
)
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
    public_urls: list[str] | None = None,
    dev: bool | None = None,
) -> FastAPI:
    """`public_urls`: the addresses a reverse proxy serves Lectern at; LECTERN_PUBLIC_URL when
    not given. A malformed one raises ValueError here, before anything starts.

    `dev`: serve the developer tools (/api/dev); LECTERN_DEV=1 when not given."""
    paths = paths or get_paths()
    if dev is None:
        dev = os.environ.get("LECTERN_DEV") == "1"
    public = parse_public_urls(
        ",".join(public_urls) if public_urls is not None
        else os.environ.get("LECTERN_PUBLIC_URL", "")
    )  # fmt: skip

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        entries = {m.id: m for m in catalog.load().models}
        app.state.downloads.resume_interrupted(entries)
        app.state.tool_env.start()  # is a container engine running? (in the background)
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

    modules = load_modules()
    app.state.tool_env = EnvProbe()
    app.state.exporters = module_exporters(modules)  # export.run and the Export buttons
    app.state.ocr_jobs = OcrJobs(app.state.runtime, app.state.projects)
    app.state.turns = TurnManager(
        app.state.runtime,
        app.state.projects,
        # The export tools offer whatever export modules are installed.
        tool_registry(
            [*CORE_TOOLS, *export_tools(app.state.exporters)], modules, app.state.tool_env
        ),
        downloads=app.state.downloads,
        sandbox=Sandbox(),
        ocr_jobs=app.state.ocr_jobs,
    )
    # When an OCR session finishes, the chats waiting on it run their follow-ups.
    app.state.ocr_jobs.on_status = app.state.turns.after_session

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.include_router(api_router(module_routers(modules), dev=dev))
    mount_web(app, web_dist)  # last: it answers every path the API didn't
    app.add_middleware(LocalGuard, port=port, token=app.state.token, public=public)
    app.state.public_urls = [u.origin for u in public]
    return app
