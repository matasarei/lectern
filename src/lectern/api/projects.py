"""/api/projects: the projects list, creating a project, and renaming it.

Every route looks the project up for the current user; an unknown id and someone else's
project both answer 404, so ids can't be probed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from lectern.core.auth import User, current_user
from lectern.projects import files, search
from lectern.projects.store import UNTITLED, Project, ProjectNotFound, ProjectStore
from lectern.projects.tables import ArtifactRow, MessageRow, SessionRow, UiStateRow, iso

router = APIRouter()

# The person asking (always the local user in the MVP; see lectern.core.auth).
CurrentUser = Annotated[User, Depends(current_user)]


def store(request: Request) -> ProjectStore:
    return request.app.state.projects


def open_project(request: Request, project_id: int, user: User) -> Project:
    try:
        return store(request).get(user, project_id)
    except ProjectNotFound:
        raise HTTPException(404, "No such project.") from None


def project_dict(project: Project) -> dict:
    created = project.created_at
    return {
        "id": project.id,
        "name": project.name,
        "folder": str(project.root),
        "created_at": iso(created) if isinstance(created, datetime) else None,
    }


class NewProject(BaseModel):
    name: str = Field(default=UNTITLED, max_length=400)


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=400)


@router.get("/projects")
def list_projects(request: Request, user: CurrentUser) -> list[dict]:
    return [project_dict(p) for p in store(request).list(user)]


@router.post("/projects", status_code=201)
def create_project(body: NewProject, request: Request, user: CurrentUser) -> dict:
    project = store(request).create(user, body.name)
    with store(request).db(project)() as session:
        chat = session.scalars(select(SessionRow).order_by(SessionRow.id)).first()
    return {**project_dict(project), "first_chat_id": chat.id if chat else None}


@router.get("/projects/{project_id}")
def get_project(project_id: int, request: Request, user: CurrentUser) -> dict:
    return project_dict(open_project(request, project_id, user))


@router.patch("/projects/{project_id}")
def rename_project(project_id: int, body: Rename, request: Request, user: CurrentUser) -> dict:
    try:
        return project_dict(store(request).rename(user, project_id, body.name))
    except ProjectNotFound:
        raise HTTPException(404, "No such project.") from None


# --- files ---------------------------------------------------------------------------------


def artifact_dict(row: ArtifactRow, from_title: str | None = None) -> dict:
    return {
        "id": row.id,
        "name": row.path,
        "kind": row.kind,
        "mime": row.mime,
        "size": row.size,
        "text": row.text_status,
        "text_note": row.text_note,
        "from": {
            "kind": row.from_kind,
            "id": row.from_id,
            "note": row.from_note,
            "title": from_title,
        },
        "created_at": iso(row.created_at),
        "modified_at": iso(row.modified_at),
    }


@router.put("/projects/{project_id}/files", status_code=201)
async def import_file(
    project_id: int,
    request: Request,
    user: CurrentUser,
    name: Annotated[str, Query(min_length=1, max_length=1024)],
) -> dict:
    """Import one file. The body is the file itself, streamed to disk."""
    project = open_project(request, project_id, user)
    try:
        row = await files.import_stream(
            project.root,
            store(request).db(project),
            owner_id=user.id,
            name=name,
            body=request.stream(),
            origin=files.Origin("import"),
        )
    except files.BadName as exc:
        raise HTTPException(400, str(exc)) from None
    except files.TooLarge as exc:
        raise HTTPException(413, str(exc)) from None
    return artifact_dict(row)


# Shown in the page: plain text and images. Everything else downloads, so a file someone
# imported (an .html, an .svg) never runs as a page under Lectern's address.
INLINE = ("text/plain", "text/markdown", "text/csv", "image/png", "image/jpeg", "image/gif",
          "image/webp")  # fmt: skip


@router.get("/projects/{project_id}/artifacts/{artifact_id}/content")
def artifact_content(
    project_id: int, artifact_id: int, request: Request, user: CurrentUser
) -> FileResponse:
    project = open_project(request, project_id, user)
    with store(request).db(project)() as session:
        row = session.get(ArtifactRow, artifact_id)
    if row is None or row.trashed_at is not None or row.owner_id != user.id:
        raise HTTPException(404, "No such file.")
    path = files.inside(project.root, row.path)
    if not path.is_file():
        raise HTTPException(404, "The file is no longer in the project folder.")
    inline = row.mime in INLINE
    return FileResponse(
        path,
        media_type=row.mime if inline else "application/octet-stream",
        filename=path.name,
        content_disposition_type="inline" if inline else "attachment",
        headers={"Cache-Control": "no-store"},  # LocalGuard adds nosniff and the CSP
    )


# --- Overview, search and tabs -------------------------------------------------------------


@router.get("/projects/{project_id}/items")
def project_items(project_id: int, request: Request, user: CurrentUser) -> dict:
    """Everything Overview lists: sessions, and files with where each came from."""
    project = open_project(request, project_id, user)
    with store(request).db(project)() as session:
        counts = dict(
            session.execute(
                select(MessageRow.session_id, func.count())
                # What the person sees as messages: theirs, and replies with text (not the
                # model's tool-call steps or the tools' results).
                .where(
                    (MessageRow.role == "user")
                    | ((MessageRow.role == "assistant") & (MessageRow.content != ""))
                )
                .group_by(MessageRow.session_id)
            ).all()
        )
        chats = session.scalars(
            select(SessionRow).where(SessionRow.owner_id == user.id).order_by(SessionRow.id)
        ).all()
        titles = {c.id: c.title for c in chats}
        rows = [r for r in files.live(session) if r.owner_id == user.id]
        names = {r.id: r.path for r in rows}
    return {
        "sessions": [
            {
                "id": c.id,
                "kind": c.kind,
                "title": c.title,
                "messages": counts.get(c.id, 0),
                "created_at": iso(c.created_at),
                "updated_at": iso(c.updated_at),
            }
            for c in chats
        ],
        "artifacts": [artifact_dict(r, _from_title(r, titles, names)) for r in rows],
    }


def _from_title(
    row: ArtifactRow, sessions: dict[int, str], artifacts: dict[int, str]
) -> str | None:
    if row.from_kind == "session" and row.from_id is not None:
        return sessions.get(row.from_id)
    if row.from_kind == "artifact" and row.from_id is not None:
        return artifacts.get(row.from_id)
    return None


@router.get("/projects/{project_id}/search")
def search_project(
    project_id: int,
    request: Request,
    user: CurrentUser,
    q: Annotated[str, Query(max_length=500)],
) -> list[dict]:
    project = open_project(request, project_id, user)
    with store(request).db(project)() as session:
        names = {r.id: r.path for r in files.live(session) if r.owner_id == user.id}
        hits = search.search(session, q, limit=20, artifact_ids=list(names))
    return [
        {
            "artifact_id": h.artifact_id,
            "name": names[h.artifact_id],
            "ref": h.ref,
            "heading": h.heading,
            "snippet": h.snippet,
        }
        for h in hits
    ]


TABS_KEY = "tabs"
MAX_TABS = 30


class Tab(BaseModel):
    kind: Literal["session", "artifact"]
    id: int


class Tabs(BaseModel):
    open: list[Tab] = Field(max_length=MAX_TABS)
    active: Tab | None = None  # None: Overview


@router.get("/projects/{project_id}/tabs")
def get_tabs(project_id: int, request: Request, user: CurrentUser) -> dict:
    """The open tabs, as last saved. Tabs for things that no longer exist are dropped. A
    project nobody has opened yet shows its first chat."""
    project = open_project(request, project_id, user)
    with store(request).db(project)() as session:
        chats = set(
            session.scalars(select(SessionRow.id).where(SessionRow.owner_id == user.id)).all()
        )
        live = {r.id for r in files.live(session) if r.owner_id == user.id}
        row = session.get(UiStateRow, TABS_KEY)
    if row is None:
        first = min(chats) if chats else None
        tabs = Tabs(open=[Tab(kind="session", id=first)] if first else [])
        tabs.active = tabs.open[0] if tabs.open else None
    else:
        tabs = Tabs.model_validate_json(row.value)

    def exists(tab: Tab) -> bool:
        return tab.id in (chats if tab.kind == "session" else live)

    tabs.open = [t for t in tabs.open if exists(t)]
    if tabs.active is not None and not exists(tabs.active):
        tabs.active = None
    return tabs.model_dump()


@router.put("/projects/{project_id}/tabs")
def put_tabs(project_id: int, body: Tabs, request: Request, user: CurrentUser) -> dict:
    project = open_project(request, project_id, user)
    with store(request).db(project).begin() as session:
        row = session.get(UiStateRow, TABS_KEY)
        if row is None:
            session.add(UiStateRow(key=TABS_KEY, value=body.model_dump_json()))
        else:
            row.value = body.model_dump_json()
    return body.model_dump()
