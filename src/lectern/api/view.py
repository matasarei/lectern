"""An HTML file of a project, opened as a page in a tab of its own.

The file tab can't show an HTML file under Lectern's address: anything that runs there can
read the token the page keeps, and Lectern's CSP blocks the file's own styles anyway. So the
page asks for a link (`POST …/artifacts/{id}/view`, which the token and the Origin check
guard like every change), and opens it in a new tab: `/view/<ticket>`.

- The ticket opens that one file of that one person, once, within VIEW_TICKET_SECONDS of
  being made. A new tab can't send the token header, so the ticket stands in for it, and only
  here: this path is outside /api, and nothing else is served under it.
- The file is served with its own policy instead of Lectern's (`core.security.VIEW_CSP`):
  `sandbox allow-scripts` without allow-same-origin, so its scripts run as they would from
  disk, in an origin of their own that can't read Lectern's storage or cookies; nothing is
  loaded from any address, Lectern's included. It can still send out what is in it, by
  navigating itself away (see VIEW_CSP's comment).
- The files the page links to beside it in the project (its stylesheets, scripts, images,
  fonts) are put inside it before it is sent (`projects.html_page`): only the owner's, not in
  the trash, of those kinds, so nothing else is ever served under this path.
- The tab is opened from a rel="noopener" link (the file tab), so it starts with none of
  Lectern's session storage, where the token is kept.
"""

from __future__ import annotations

import posixpath
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from lectern.api.projects import open_project, owned_file, store
from lectern.core.auth import User
from lectern.projects import files, html_page
from lectern.projects.html_page import Read, inline_page
from lectern.projects.tables import ArtifactRow

VIEW_TICKET_SECONDS = 60
GONE = "This link has been used or has expired. Open the file again from Lectern."


@dataclass(frozen=True)
class Ticket:
    user: User
    project_id: int
    artifact_id: int
    expires: datetime


class ViewTickets:
    """The links handed out and not yet used, in memory: one process serves Lectern, and a
    restart ends them all, which is what a minute-long link should do."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()

    def issue(self, user: User, project_id: int, artifact_id: int) -> str:
        now = self._clock()
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            # Those never opened go when the next one is made, so the store stays small.
            self._tickets = {k: t for k, t in self._tickets.items() if t.expires > now}
            expires = now + timedelta(seconds=VIEW_TICKET_SECONDS)
            self._tickets[ticket] = Ticket(user, project_id, artifact_id, expires)
        return ticket

    def take(self, ticket: str) -> Ticket | None:
        """The ticket's file, once: asking again, or after it expired, gives None."""
        with self._lock:
            held = self._tickets.pop(ticket, None)
        if held is None or held.expires <= self._clock():
            return None
        return held


router = APIRouter()


@router.get("/view/{ticket}", include_in_schema=False)
def view(ticket: str, request: Request) -> Response:
    held = request.app.state.view_tickets.take(ticket)
    if held is None:
        return PlainTextResponse(GONE, status_code=404)
    try:
        row, path = owned_file(request, held.project_id, held.artifact_id, held.user)
    except HTTPException:  # trashed, removed or gone from the folder since the link was made
        return PlainTextResponse(GONE, status_code=404)
    inlined = None
    if path.stat().st_size <= html_page.PAGE_LIMIT:
        project = open_project(request, held.project_id, held.user)
        with store(request).db(project)() as session:
            folder = posixpath.dirname(row.path)
            inlined = inline_page(
                path.read_bytes(), _side_files(project.root, session, held.user, folder)
            )
    if inlined is not None:
        return Response(
            inlined, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"}
        )
    response = FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})
    # Starlette adds "; charset=utf-8" to a text type, which would override the file's own
    # <meta charset> (a header's charset wins): the browser reads it as it would a local file.
    response.headers["content-type"] = "text/html"
    return response


def _side_files(root: Path, session: Session, user: User, folder: str) -> Read:
    """`read` for `inline_page`: a file the page names from its folder, when it is one of the
    page owner's files in the project, not in the trash, still on disk and within the limit."""

    def read(relative: str) -> bytes | None:
        stored = posixpath.normpath(posixpath.join(folder, relative))
        if stored.startswith(("../", "/")) or stored in {"..", "."}:
            return None
        row = session.scalar(
            select(ArtifactRow).where(
                ArtifactRow.path == stored,
                ArtifactRow.owner_id == user.id,
                ArtifactRow.trashed_at.is_(None),
            )
        )
        if row is None:
            return None
        try:
            path = files.inside(root, stored)
            if not path.is_file() or path.stat().st_size > html_page.FILE_LIMIT:
                return None
            return path.read_bytes()
        except (files.BadName, OSError):
            return None

    return read
