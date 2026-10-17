"""/api/projects/{id}/sessions: chats — the transcript, sending a message, following the
reply, answering confirmation cards, and stopping.

Sending returns at once (202); the turn runs on the server and the page follows it on the
events stream. A page that reloads reads the transcript, then the events stream, which
starts with the current turn's events so far.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection

from lectern.agent import followups
from lectern.agent import state as pins
from lectern.agent.state import load as chat_state_of
from lectern.agent.turns import Busy, TurnManager
from lectern.api.projects import CurrentUser, open_project, store
from lectern.api.sse import event_response, pump
from lectern.api.ws import refuse_as, socket_events
from lectern.core.auth import User
from lectern.projects.store import NEW_CHAT, Project, clean_name
from lectern.projects.tables import ArtifactRow, ConfirmationRow, MessageRow, SessionRow, iso

router = APIRouter()
MESSAGE_LIMIT = 100_000
ATTACH_LIMIT = 20


def turns(request: HTTPConnection) -> TurnManager:
    return request.app.state.turns


def open_session(
    request: HTTPConnection, project: Project, session_id: int, user: User
) -> SessionRow:
    with store(request).db(project)() as session:
        row = session.get(SessionRow, session_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(404, "No such session.")
    return row


async def open_chat(
    request: HTTPConnection, project_id: int, session_id: int, user: User
) -> Project:
    """The project and chat lookups for async routes, in a worker thread: they read SQLite,
    and a project's first open also migrates its database and empties old trash."""

    def lookup() -> Project:
        project = open_project(request, project_id, user)
        open_session(request, project, session_id, user)
        store(request).db(project)
        return project

    return await run_in_threadpool(lookup)


def session_dict(row: SessionRow, state: str) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "state": state,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


class NewSession(BaseModel):
    title: str = Field(default=NEW_CHAT, max_length=200)


class Retitle(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class Send(BaseModel):
    text: str = Field(min_length=1, max_length=MESSAGE_LIMIT)
    attachments: list[int] = Field(default_factory=list, max_length=ATTACH_LIMIT)


class Decision(BaseModel):
    approve: bool


@router.post("/projects/{project_id}/sessions", status_code=201)
def new_chat(project_id: int, body: NewSession, request: Request, user: CurrentUser) -> dict:
    project = open_project(request, project_id, user)
    title = clean_name(body.title) if body.title.strip() else NEW_CHAT
    with store(request).db(project).begin() as session:
        row = SessionRow(owner_id=user.id, kind="chat", title=title, titled=title != NEW_CHAT)
        session.add(row)
        session.flush()
    return session_dict(row, "idle")


@router.get("/projects/{project_id}/sessions/{session_id}")
def get_chat(project_id: int, session_id: int, request: Request, user: CurrentUser) -> dict:
    """The session and its whole transcript, with any card still waiting for an answer."""
    project = open_project(request, project_id, user)
    row = open_session(request, project, session_id, user)
    # Before the transcript: a turn that ends between the two reads then shows as still
    # running, never as idle without its last reply.
    state = turns(request).state((project.id, session_id))
    with store(request).db(project)() as session:
        messages = session.scalars(
            select(MessageRow).where(MessageRow.session_id == session_id).order_by(MessageRow.id)
        ).all()
        attached = {
            i for m in messages for i in json.loads(m.attachments or "[]")
        }  # fmt: skip
        names = {
            a.id: a.path
            for a in session.scalars(select(ArtifactRow).where(ArtifactRow.id.in_(attached)))
        }
        pending = session.scalars(
            select(ConfirmationRow).where(
                ConfirmationRow.session_id == session_id, ConfirmationRow.status == "pending"
            )
        ).all()
    chat_state = chat_state_of(store(request).db(project), session_id)
    return {
        **session_dict(row, state),
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "tool_calls": json.loads(m.tool_calls) if m.tool_calls else None,
                "tool_call_id": m.tool_call_id,
                "tool_name": m.tool_name,
                "attachments": [
                    {"id": i, "name": names.get(i)} for i in json.loads(m.attachments or "[]")
                ],
                "refs": json.loads(m.refs) if m.refs else [],  # what a tool result pointed at
                "created_at": iso(m.created_at),
            }
            for m in messages
        ],
        "pinned": [{"id": p.id, "kind": p.kind, "text": p.text} for p in chat_state.pinned],
        # What runs when an OCR session finishes (task.after, ocr.start's then_export).
        "followups": [
            # tool "": a notice (a plain message when the session ends), not a step to show
            {"id": f.id, "label": f.label, "ocr_session_id": f.ocr_session_id,
             "tool": f.tool, "what": f.what}
            for f in chat_state.followups
        ],
        # Where the summary ends: messages up to this id reach the model only as the summary.
        "summary": {
            "upto": chat_state.summary_upto,
            "version": chat_state.summary_version,
            "messages": sum(1 for m in messages if m.id <= chat_state.summary_upto),
        },
        # A card only counts while its turn is still waiting on it.
        "pending": [
            {"confirmation_id": p.id, "call_id": p.tool_call_id, "name": p.tool}
            for p in pending
            if state == "waiting"
        ],
    }  # fmt: skip


@router.patch("/projects/{project_id}/sessions/{session_id}")
def retitle(
    project_id: int, session_id: int, body: Retitle, request: Request, user: CurrentUser
) -> dict:
    project = open_project(request, project_id, user)
    open_session(request, project, session_id, user)
    with store(request).db(project).begin() as session:
        row = session.get(SessionRow, session_id)
        row.title, row.titled = clean_name(body.title), True  # type: ignore[union-attr]
        session.flush()
        return session_dict(row, turns(request).state((project.id, session_id)))  # type: ignore[arg-type]


@router.post("/projects/{project_id}/sessions/{session_id}/messages", status_code=202)
async def send(
    project_id: int, session_id: int, body: Send, request: Request, user: CurrentUser
) -> dict:
    project = await open_chat(request, project_id, session_id, user)
    try:
        message_id = await turns(request).send(
            project, user, session_id, body.text, body.attachments
        )
    except Busy:
        raise HTTPException(409, "This chat is still answering. Wait, or press Stop.") from None
    return {"message_id": message_id}


@router.get("/projects/{project_id}/sessions/{session_id}/events")
async def events(project_id: int, session_id: int, request: Request, user: CurrentUser):  # noqa: ANN201
    project = await open_chat(request, project_id, session_id, user)
    channel = turns(request).channel((project.id, session_id))
    # The chat is open: a follow-up whose session ended while the app was down runs now.
    await turns(request).resume_follow_ups(project, session_id)

    async def stream() -> AsyncIterator[bytes]:
        async with channel.listen() as (so_far, queue):
            # The marker tells the page where the replay ends: an action in a replayed event
            # (such as opening a tab) already happened and must not happen again.
            replayed = [*so_far, {"type": "replayed", "seq": 0}]
            async for chunk in pump(request, queue, replayed):
                yield chunk

    return event_response(stream())


@router.websocket("/projects/{project_id}/sessions/{session_id}/events")
async def events_socket(
    project_id: int, session_id: int, websocket: WebSocket, user: CurrentUser
) -> None:
    """The same events for the page, which follows them on a socket (see `lectern.api.ws`)."""
    try:
        project = await open_chat(websocket, project_id, session_id, user)
    except HTTPException as gone:
        await refuse_as(websocket, gone)
        return
    channel = turns(websocket).channel((project.id, session_id))
    await turns(websocket).resume_follow_ups(project, session_id)  # as the HTTP stream does
    async with channel.listen() as (so_far, queue):
        await socket_events(websocket, queue, [*so_far, {"type": "replayed", "seq": 0}])


@router.get("/projects/{project_id}/events")
async def project_events(project_id: int, request: Request, user: CurrentUser):  # noqa: ANN201
    """The tabs this project's chats open, for the project page: it shows one chat at a time,
    and a chat that isn't showing can still add a tab (a DOCX exported after the last
    answer in the OCR tab)."""
    project = await run_in_threadpool(open_project, request, project_id, user)
    channel = turns(request).project_channel(project.id)

    async def stream() -> AsyncIterator[bytes]:
        async with channel.listen() as (_so_far, queue):
            async for chunk in pump(request, queue, [{"type": "replayed", "seq": 0}]):
                yield chunk

    return event_response(stream())


@router.websocket("/projects/{project_id}/events")
async def project_events_socket(project_id: int, websocket: WebSocket, user: CurrentUser) -> None:
    """The same events for the page, which follows them on a socket (see `lectern.api.ws`)."""
    try:
        project = await run_in_threadpool(open_project, websocket, project_id, user)
    except HTTPException as gone:
        await refuse_as(websocket, gone)
        return
    channel = turns(websocket).project_channel(project.id)
    async with channel.listen() as (_so_far, queue):
        await socket_events(websocket, queue, [{"type": "replayed", "seq": 0}])


@router.post("/projects/{project_id}/sessions/{session_id}/confirmations/{confirmation_id}")
def decide(
    project_id: int,
    session_id: int,
    confirmation_id: int,
    body: Decision,
    request: Request,
    user: CurrentUser,
) -> dict:
    project = open_project(request, project_id, user)
    open_session(request, project, session_id, user)
    if not turns(request).decide((project.id, session_id), confirmation_id, body.approve):
        raise HTTPException(404, "Nothing is waiting for that answer.")
    return {"approved": body.approve}


@router.post("/projects/{project_id}/sessions/{session_id}/stop")
async def stop(project_id: int, session_id: int, request: Request, user: CurrentUser) -> dict:
    project = await open_chat(request, project_id, session_id, user)
    return {"stopped": await turns(request).stop((project.id, session_id))}


@router.post("/projects/{project_id}/sessions/{session_id}/followups/{followup_id}/run",
             status_code=202)  # fmt: skip
async def run_follow_up(
    project_id: int, session_id: int, followup_id: str, request: Request, user: CurrentUser
) -> dict:
    """Export now: a follow-up waiting for its OCR session's Document to be clear runs as it
    is (open questions are marked). It runs in the chat like any follow-up; the page hears
    it on the chat's events."""
    project = await run_in_threadpool(open_project, request, project_id, user)
    await run_in_threadpool(open_session, request, project, session_id, user)
    if not await turns(request).run_follow_up_now(project, session_id, followup_id):
        raise HTTPException(404, "Nothing is waiting with that id.")
    return {"id": followup_id}


@router.delete("/projects/{project_id}/sessions/{session_id}/followups/{followup_id}")
def cancel_follow_up(
    project_id: int, session_id: int, followup_id: str, request: Request, user: CurrentUser
) -> dict:
    """The person drops what the chat queued for when an OCR session finishes. One that has
    already started (or run) is no longer waiting: 404."""
    project = open_project(request, project_id, user)
    open_session(request, project, session_id, user)
    taken = followups.take(store(request).db(project), session_id, followup_id)
    if taken is None:
        raise HTTPException(404, "Nothing is waiting with that id.")
    return {"id": taken.id, "label": taken.label}


@router.delete("/projects/{project_id}/sessions/{session_id}/pins/{pin_id}")
def unpin(
    project_id: int, session_id: int, pin_id: str, request: Request, user: CurrentUser
) -> dict:
    """The person removes a pinned item; the model stops getting it from the next call."""
    project = open_project(request, project_id, user)
    open_session(request, project, session_id, user)
    try:
        removed = pins.unpin(store(request).db(project), session_id, pin_id)
    except pins.PinError:
        raise HTTPException(404, "Nothing pinned has that id.") from None
    return {"id": removed.id, "text": removed.text}
