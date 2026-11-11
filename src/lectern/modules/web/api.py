"""The web module's settings: whether the chats get the web tools, and the Brave Search API
key searches go to first. Seen by everyone, changed only by an admin, since they decide for the
whole install what may leave it. The key itself is never sent back: only whether there is one."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from lectern.core import settings
from lectern.core.auth import require_admin
from lectern.core.security import read_secret, write_secret
from lectern.modules.web import tools

router = APIRouter()


class WebSettings(BaseModel):
    enabled: bool


class WebKey(BaseModel):
    key: str


def _checked(key: str) -> str:
    """The key as it's stored, or a 422 that doesn't repeat it: a validation error from the
    model would send the refused value back in its `input` field."""
    key = key.strip()
    if not 8 <= len(key) <= 200 or not key.isprintable() or any(c.isspace() for c in key):
        raise HTTPException(422, "A key is one word of 8 to 200 characters, with no spaces.")
    return key


def _key(request: Request) -> str | None:
    return read_secret(request.app.state.paths.web_search_key_file)


@router.get("/web/settings")
def get_settings(request: Request) -> dict:
    values = settings.get(request.app.state.sessions, settings.WEB_KEY, settings.WEB_DEFAULTS)
    key = _key(request)
    # Where what the chat sends goes: the search sources in the order they're asked, then
    # Wikipedia. Pages opened go to their own sites.
    hosts = [s.host for s in tools.sources_for(key)]
    return {"values": values, "brave_key": key is not None, "hosts": [*hosts, "wikipedia.org"]}


@router.put("/web/settings", dependencies=[Depends(require_admin)])
async def put_settings(body: WebSettings, request: Request) -> dict:
    sessions = request.app.state.sessions
    await run_in_threadpool(settings.put, sessions, settings.WEB_KEY, body.model_dump())
    return await run_in_threadpool(get_settings, request)


@router.put("/web/key", dependencies=[Depends(require_admin)])
async def put_key(body: WebKey, request: Request) -> dict:
    path = request.app.state.paths.web_search_key_file
    await run_in_threadpool(write_secret, path, _checked(body.key))
    return await run_in_threadpool(get_settings, request)


@router.delete("/web/key", dependencies=[Depends(require_admin)])
async def delete_key(request: Request) -> dict:
    path = request.app.state.paths.web_search_key_file
    await run_in_threadpool(path.unlink, True)  # missing_ok: removing no key is no error
    return await run_in_threadpool(get_settings, request)
