"""/api/projects: the projects list, creating a project, and renaming it.

Every route looks the project up for the current user; an unknown id and someone else's
project both answer 404, so ids can't be probed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from lectern.core.auth import User, current_user
from lectern.projects import files
from lectern.projects.store import UNTITLED, Project, ProjectNotFound, ProjectStore
from lectern.projects.tables import ArtifactRow, SessionRow

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
        "created_at": created.isoformat() if isinstance(created, datetime) else None,
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


def artifact_dict(row: ArtifactRow) -> dict:
    return {
        "id": row.id,
        "name": row.path,
        "kind": row.kind,
        "mime": row.mime,
        "size": row.size,
        "text": row.text_status,
        "text_note": row.text_note,
        "from": {"kind": row.from_kind, "id": row.from_id, "note": row.from_note},
        "created_at": row.created_at.isoformat(),
        "modified_at": row.modified_at.isoformat(),
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
