"""The /api routes. Every router is mounted under `api_router()`, which requires a user."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends, WebSocket

from lectern.api import chat, dev, firstrun, models, projects, runtime, sessions, system
from lectern.core.auth import current_user
from lectern.core.security import WS_SUBPROTOCOL


def api_router(module_routers: Sequence[APIRouter] = ()) -> APIRouter:
    """Every /api route, the modules' included, behind the user check."""
    router = APIRouter(prefix="/api", dependencies=[Depends(current_user)])

    @router.websocket("/ws/ping")
    async def ws_ping(websocket: WebSocket) -> None:
        await websocket.accept(subprotocol=WS_SUBPROTOCOL)
        await websocket.send_text("pong")
        await websocket.close()

    router.include_router(system.router)
    router.include_router(models.router)
    router.include_router(firstrun.router)
    router.include_router(runtime.router)
    router.include_router(chat.router)
    router.include_router(projects.router)
    router.include_router(sessions.router)
    router.include_router(dev.router)
    for extra in module_routers:
        router.include_router(extra)
    return router
