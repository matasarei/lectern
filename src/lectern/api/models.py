"""The model catalog over HTTP: list models and what's installed, choose a role's model, start
a download, remove a model, follow progress."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from lectern.api.sse import hub_stream
from lectern.catalog import catalog
from lectern.core import settings
from lectern.downloads.manager import (
    DownloadManager,
    NoSuchFolder,
    NothingToUpdate,
    NotRemovable,
)
from lectern.hardware.probe import probe
from lectern.hardware.tiers import effective_tier
from lectern.projects.tables import iso
from lectern.runners import mlx
from lectern.runtime.supervisor import ModelInUse
from lectern.runtime.wiring import pick

router = APIRouter()


def _manager(request: Request) -> DownloadManager:
    return request.app.state.downloads


@router.get("/models")
def list_models(request: Request) -> dict:
    cat = catalog.load()
    tier = effective_tier(probe())
    downloads = _manager(request)
    recommended = cat.defaults_for(tier)
    chosen = request.app.state.runtime.chosen_models()
    uses = {role: pick(role, tier, chosen)[1].id for role in recommended}
    runtime_settings = settings.get(
        request.app.state.sessions, settings.RUNTIME_KEY, settings.RUNTIME_DEFAULTS
    )
    mlx_on = bool(runtime_settings["mlx_enabled"]) and mlx.mlx_available()

    def used_by(m: catalog.CatalogEntry) -> list[str]:
        """The roles that use the model now. An MLX build is used by the roles of the model
        it stands in for, while MLX is on."""
        stands_for = m.replaces if mlx_on else None
        return [role for role, model_id in uses.items() if model_id in (m.id, stands_for)]

    return {
        "tier": tier.value,
        # The model recommended for each role on this computer, and the one the role uses:
        # the same unless another was chosen (PUT /models/roles/{role}).
        "defaults": {role: entry.id for role, entry in recommended.items()},
        "chosen": uses,
        "models": [
            {
                "id": m.id,
                "title": m.title,
                "quant": m.quant,
                "roles": m.roles,
                "size": m.size,
                "licence": m.licence,
                "licence_notice": m.licence_notice,
                "notes": m.notes,
                "repo": m.repo,
                "format": m.format,
                "replaces": m.replaces,  # an MLX build: the model it stands in for
                "suits_tier": tier in m.ctx,  # listed for this computer's tier
                "download": downloads.state(m).to_dict(),
                "installed": _installed(downloads, m),  # None: nothing of it on disk
                "used_by": used_by(m),
                # The catalog pins a newer revision than the one on disk (POST …/update), and
                # that update while it runs or after it failed.
                "update_available": downloads.update_available(m),
                "update": _update(downloads, m),
            }
            for m in cat.models
        ],
        # Folders in the models directory that no catalog model owns (a model a later
        # Lectern dropped): they take room, and can be removed like a model.
        "unlisted": [
            {"name": name, "bytes": size}
            for name, size in downloads.unlisted({m.id for m in cat.models})
        ],
    }


def _installed(downloads: DownloadManager, entry: catalog.CatalogEntry) -> dict | None:
    installed = downloads.installed(entry)
    if installed is None:
        return None
    return {
        "revision": installed.revision,  # the model's version on disk; None: not recorded
        "at": iso(installed.at) if installed.at else None,
        "bytes": installed.bytes,
    }


def _update(downloads: DownloadManager, entry: catalog.CatalogEntry) -> dict | None:
    state = downloads.update_state(entry.id)
    return state.to_dict() if state is not None else None


class RoleModel(BaseModel):
    model_id: str | None = None  # None: the one recommended for this computer


@router.put("/models/roles/{role}")
async def set_role_model(role: str, body: RoleModel, request: Request) -> dict:
    """Choose the model a role uses. Any model with the role can be chosen, one this
    computer isn't meant for included: the runtime refuses what doesn't fit when it loads."""
    if role not in settings.MODEL_DEFAULTS:
        raise HTTPException(404, f"There's no {role!r} role.")
    cat = catalog.load()
    model_id = body.model_id
    if model_id is not None:
        entry = cat.get(model_id)
        if entry is None:
            raise HTTPException(404, f"No model {model_id!r} in the catalog.")
        if entry.replaces is not None:
            raise HTTPException(
                409,
                f"{entry.title} is an MLX build. Choose {entry.replaces} and switch MLX on: "
                "the build is used in its place.",
            )
        if entry not in cat.choices_for(role):
            raise HTTPException(409, f"{entry.title} can't be used for the {role!r} role.")
        tier = await run_in_threadpool(lambda: effective_tier(probe()))
        if cat.defaults_for(tier).get(role) is entry:
            model_id = None  # the recommended one: the role follows the recommendation again
    before = await run_in_threadpool(
        settings.update,
        request.app.state.sessions,
        settings.MODELS_KEY,
        settings.MODEL_DEFAULTS,
        {role: model_id},
    )
    if before[role] != model_id:
        # The role's idle model goes now and the chosen one loads when the role is next used;
        # a model a reply is being written with changes over with the next turn.
        await request.app.state.runtime.unload_between_replies(role)
    return await run_in_threadpool(list_models, request)


class DownloadRequest(BaseModel):
    accept_licence: bool = False


@router.post("/models/{model_id}/download", status_code=202)
async def start_download(
    model_id: str, request: Request, body: DownloadRequest | None = None
) -> dict:
    entry = catalog.load().get(model_id)
    if entry is None:
        raise HTTPException(404, f"No model {model_id!r} in the catalog.")
    if entry.format == "mlx" and not mlx.mlx_supported():
        # The page doesn't offer it here; a request made by hand shouldn't fill the disk
        # with a model nothing on this machine can load.
        raise HTTPException(
            409, f"{entry.title} is an MLX model, and MLX only runs on Apple Silicon Macs."
        )
    if entry.licence_notice and not (body and body.accept_licence):
        raise HTTPException(
            409,
            f"{entry.title} is under {entry.licence}. Read the licence, then download again "
            "with accept_licence set to true.",
        )
    return _manager(request).start(entry).to_dict()


@router.post("/models/{model_id}/update", status_code=202)
async def update_model(model_id: str, request: Request) -> dict:
    """Bring an installed model to the revision the catalog pins now. It runs in the
    background: the new revision is built beside the model, which stays in use, and takes its
    place when the model is free. The list (`update`) says how far it is."""
    entry = catalog.load().get(model_id)
    if entry is None:
        raise HTTPException(404, f"No model {model_id!r} in the catalog.")
    try:
        return _manager(request).start_update(entry).to_dict()
    except NothingToUpdate as exc:
        raise HTTPException(
            409, f"{exc} It isn't on disk, or it is the catalog's version."
        ) from exc


@router.delete("/models/{name}")
async def remove_model(name: str, request: Request) -> dict:
    """Delete a model's folder from the models directory: a catalog model's, or one no
    catalog model owns. A role that used it keeps asking for it and shows Download."""
    downloads = _manager(request)
    entry = catalog.load().get(name)
    title = entry.title if entry is not None else name
    try:
        downloads.removable(name)  # before the runtime is asked to unload anything
        # No runner may have the files open, and none may load them half-deleted.
        await request.app.state.runtime.without_model(name, lambda: downloads.remove(name))
    except NoSuchFolder as exc:
        raise HTTPException(404, str(exc)) from exc
    except NotRemovable as exc:
        raise HTTPException(409, str(exc)) from exc
    except ModelInUse as exc:
        raise HTTPException(
            409, f"{title} is in use. Remove it when the reply or the job using it is done."
        ) from exc
    except OSError as exc:
        raise HTTPException(500, f"{title} couldn't be removed: {exc.strerror or exc}.") from exc
    return await run_in_threadpool(list_models, request)


@router.get("/models/events")
async def model_events(request: Request) -> StreamingResponse:
    """Server-sent events with download progress. Read with fetch(); EventSource can't send
    the token header."""
    return hub_stream(request, _manager(request).events)
