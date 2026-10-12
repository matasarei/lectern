"""A role's model fetched while a tool waits: the download starts, its progress shows in the
chat, and the tool goes on once it's done. Used for the models that download "the first time
they're needed" (the coding model, the OCR models); the card asking first is the caller's."""

from __future__ import annotations

from lectern.agent.tools import ToolContext
from lectern.catalog.catalog import CatalogEntry
from lectern.core.auth import ADMIN
from lectern.downloads.manager import DownloadState


def on_disk(ctx: ToolContext, entry: CatalogEntry) -> bool:
    """True when there's nothing to download, or nothing to download with (tests)."""
    return ctx.downloads is None or ctx.downloads.is_downloaded(entry)


async def download(ctx: ToolContext, entry: CatalogEntry) -> DownloadState:
    """Download `entry`, reporting progress to the chat, and return how it ended: "done",
    "failed" or "paused" (stopped before it finished, e.g. the app shutting down). Downloading
    a model is the admin's, here as in Models & memory: for anyone else it fails at once, and
    says who to ask."""
    if ctx.user.role != ADMIN:
        why = f"only an admin can download models. Ask them to download {entry.title}"
        return DownloadState(entry.id, "failed", 0, entry.size, error=why)
    downloads = ctx.downloads
    async with downloads.events.listen() as queue:
        state = downloads.start(entry)
        # "paused" too: a download that stopped won't finish by itself.
        while state.status not in ("done", "failed", "paused"):
            event = await queue.get()
            if event.get("type") != "download" or event.get("model_id") != entry.id:
                continue
            state = type(state)(**{k: v for k, v in event.items() if k != "type"})
            ctx.emit(
                {
                    "type": "progress",
                    "label": f"Downloading {entry.title}",
                    "done": state.bytes_done,
                    "total": state.bytes_total,
                }
            )
    return state
