"""Serving the built web UI. Any path that isn't an API route or a real file gets index.html,
so the browser-side router can handle it."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response

WEB_DIST = Path(__file__).parent / "web_dist"


def mount_web(app: FastAPI, dist: Path = WEB_DIST) -> None:
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not found")
        index = root / "index.html"
        if not index.is_file():
            return PlainTextResponse(
                "The web UI isn't built. Run: npm --prefix web ci && npm --prefix web run build",
                status_code=503,
            )
        if path:
            candidate = (root / path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-store"})
