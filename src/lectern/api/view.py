"""An HTML file of a project, opened as a page in a tab of its own.

The file tab can't show an HTML file under Lectern's address: anything that runs there can
read the token the page keeps, and Lectern's CSP blocks the file's own styles anyway. So the
page asks for a link (`POST …/artifacts/{id}/view`, which the token and the Origin check
guard like every change), and opens it in a new tab: `/view/<ticket>`.

- The ticket opens that one file of that one person, once, within VIEW_TICKET_SECONDS of
  being made. A new tab can't send the token header, so the ticket stands in for it, and only
  here: this path is outside /api, and nothing else is served under it.
- The file is served with its own policy instead of Lectern's (`core.security.VIEW_CSP`):
  `sandbox` without allow-scripts or allow-same-origin, so no script runs and the page has an
  origin of its own that can't read Lectern's storage or cookies; its inline styles and data:
  images apply, and nothing is loaded from anywhere else.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from lectern.api.projects import owned_file
from lectern.core.auth import User

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
        _row, path = owned_file(request, held.project_id, held.artifact_id, held.user)
    except HTTPException:  # trashed, removed or gone from the folder since the link was made
        return PlainTextResponse(GONE, status_code=404)
    response = FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})
    # Starlette adds "; charset=utf-8" to a text type, which would override the file's own
    # <meta charset> (a header's charset wins): the browser reads it as it would a local file.
    response.headers["content-type"] = "text/html"
    return response
