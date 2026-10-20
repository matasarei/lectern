"""The web module's switch: whether the chats get the web tools. Seen by everyone, changed
only by an admin, since it decides for the whole install what may leave it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from lectern.core import settings
from lectern.core.auth import require_admin
from lectern.modules.web import tools

router = APIRouter()


class WebSettings(BaseModel):
    enabled: bool


@router.get("/web/settings")
def get_settings(request: Request) -> dict:
    values = settings.get(request.app.state.sessions, settings.WEB_KEY, settings.WEB_DEFAULTS)
    # Where what the chat sends goes: the search engines in the order they're asked, then
    # Wikipedia. Pages opened go to their own sites.
    return {"values": values, "hosts": [*(s.host for s in tools.SOURCES), "wikipedia.org"]}


@router.put("/web/settings", dependencies=[Depends(require_admin)])
async def put_settings(body: WebSettings, request: Request) -> dict:
    sessions = request.app.state.sessions
    await run_in_threadpool(settings.put, sessions, settings.WEB_KEY, body.model_dump())
    return await run_in_threadpool(get_settings, request)
