"""The FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI

from lectern import __version__
from lectern.config import Paths, get_paths


def create_app(paths: Paths | None = None) -> FastAPI:
    paths = paths or get_paths()
    app = FastAPI(title="Lectern", version=__version__, docs_url=None, redoc_url=None)
    app.state.paths = paths

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
