"""The /api routes. Every router is mounted under `api_router()`, which requires a user."""

from __future__ import annotations

from fastapi import APIRouter, Depends, WebSocket

from lectern.api import chat, firstrun, models, runtime, system
from lectern.core.auth import current_user
from lectern.core.security import WS_SUBPROTOCOL


def api_router() -> APIRouter:
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
    return router
