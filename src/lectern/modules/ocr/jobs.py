"""The OCR job: an OCR session's pages, read in the background.

Starting a session returns at once; the job runs as a server task and the tab follows it on
the session's events, like a chat turn. It runs in phases (blueprint: "OCR runs in phases"):

1. Rasterise the source into page images, once.
2. Read each page through its model: `ocr`, `ocr.hand` for a page marked handwritten, or the
   `role@model` chosen for it. The model is leased per page, so a chat gets the GPU after the
   current page. When the chat has taken it, the job waits until the chat has been idle for
   CHAT_IDLE_RESUME seconds before loading its model back: a conversation shouldn't cost two
   model swaps per reply.

One job runs at a time; other sessions wait as queued. Pause stops after the current page.
A job interrupted by a restart isn't started again on its own: the session shows as paused
("Lectern restarted") the next time it's read, and continues from the first page not done
when Resume is pressed.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select

from lectern.agent.turns import Channel
from lectern.modules.ocr import store as ocr_store
from lectern.modules.ocr.pages import NotAPageSource, rasterise
from lectern.modules.ocr.recognise import RecogniseFailed, recognise
from lectern.projects.store import Project, ProjectStore
from lectern.projects.tables import OcrPageRow, OcrSessionRow, now
from lectern.runners.base import RunnerError
from lectern.runtime.supervisor import ModelRuntime, NotAvailable, Priority
from lectern.runtime.wiring import pick

CHAT_IDLE_RESUME = 120.0  # seconds of chat idleness before OCR loads its model back
IDLE_POLL = 1.0
RESTARTED = "Lectern restarted"
PAUSED = "Paused"

log = logging.getLogger(__name__)
Key = tuple[int, int]  # (project id, session id)


class NotNow(Exception):
    """The session isn't in a state for that (resuming a running job, say)."""


def page_dict(page: OcrPageRow) -> dict:
    return {
        "number": page.number,
        "key": page.key,
        "status": page.status,
        "model_id": page.model_id,
        "lease_key": page.lease_key,
        "handwritten": page.handwritten,
        "note": page.note,
        "seconds": page.seconds,
        "cut_off": page.cut_off,
        "looped": page.looped,
        "error": page.error,
        "width": page.width,
        "height": page.height,
        "has_hint": page.has_hint,
    }


@dataclass
class _Queued:
    key: Key
    project: Project


class OcrJobs:
    def __init__(
        self,
        runtime: ModelRuntime,
        projects: ProjectStore,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.runtime = runtime
        self.projects = projects
        self._sleep = sleep
        self._channels: dict[Key, Channel] = {}
        self._queue: collections.deque[_Queued] = collections.deque()
        self._current: Key | None = None
        self._pausing: set[Key] = set()
        self._worker: asyncio.Task[None] | None = None

    def channel(self, key: Key) -> Channel:
        return self._channels.setdefault(key, Channel())

    def active(self, key: Key) -> bool:
        """This process is running or holding the session's job."""
        return key == self._current or any(q.key == key for q in self._queue)

    # --- reading a session ---------------------------------------------------------------

    def settle(self, project: Project, session_id: int) -> OcrSessionRow:
        """The session's row, with a job that no longer exists (the app was restarted
        mid-batch) shown as paused. Nothing is started: that waits for Resume."""
        key = (project.id, session_id)
        with self.projects.db(project).begin() as session:
            ocr = session.get(OcrSessionRow, session_id)
            if ocr.status in ("queued", "running") and not self.active(key):
                ocr.status, ocr.paused_reason = "paused", RESTARTED
                ocr.updated_at = now()
            return ocr

    # --- starting, pausing, resuming -----------------------------------------------------

    async def start(self, project: Project, session_id: int) -> None:
        """Queue a session's job (a new session, a Resume, or a page to read again)."""
        key = (project.id, session_id)
        self._pausing.discard(key)
        if not self.active(key):
            self._queue.append(_Queued(key, project))  # before the await: nothing slips in
            await asyncio.to_thread(self._set_status, project, session_id, "queued")
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="ocr-jobs")

    async def resume(self, project: Project, session_id: int) -> None:
        ocr = await asyncio.to_thread(self.settle, project, session_id)
        if ocr.status not in ("paused", "failed", "done"):
            raise NotNow(f"The session is {ocr.status}.")
        await self.start(project, session_id)

    async def pause(self, project: Project, session_id: int) -> None:
        """A queued job leaves the queue; a running one stops after its current page."""
        key = (project.id, session_id)
        if key == self._current:
            self._pausing.add(key)
            return
        waiting = [q for q in self._queue if q.key == key]
        if not waiting:
            raise NotNow("The session isn't running.")
        self._queue.remove(waiting[0])
        await asyncio.to_thread(self._set_status, project, session_id, "paused", reason=PAUSED)

    async def rerun(
        self, project: Project, session_id: int, number: int, *, lease_key: str | None
    ) -> OcrPageRow:
        """Read one page again, with the model named (`role@model`) or the page's default."""

        def reset() -> OcrPageRow:
            with self.projects.db(project).begin() as session:
                page = session.scalars(
                    select(OcrPageRow).where(
                        OcrPageRow.session_id == session_id, OcrPageRow.number == number
                    )
                ).one()
                page.status, page.lease_key, page.error = "pending", lease_key, None
                page.updated_at = now()
                return page

        page = await asyncio.to_thread(reset)
        self.channel((project.id, session_id)).publish({"type": "page", **page_dict(page)})
        await self.start(project, session_id)
        return page

    async def stop_all(self) -> None:
        """At shutdown. Sessions keep their status: the next read shows them as paused."""
        self._queue.clear()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    async def idle(self) -> None:
        """For tests: until the queue is empty and nothing is running."""
        while self._worker is not None and not self._worker.done():
            await asyncio.sleep(0)

    # --- the job -------------------------------------------------------------------------

    async def _work(self) -> None:
        while self._queue:
            queued = self._queue.popleft()
            self._current = queued.key
            try:
                await self._run(queued.project, queued.key[1])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a job's crash mustn't stop the queue
                log.exception("OCR job %s failed", queued.key)
                await asyncio.to_thread(
                    self._set_status, queued.project, queued.key[1], "failed", error=str(exc)
                )
            finally:
                self._current = None
                self._pausing.discard(queued.key)

    async def _run(self, project: Project, session_id: int) -> None:
        key = (project.id, session_id)
        db = await asyncio.to_thread(self.projects.db, project)
        await asyncio.to_thread(self._set_status, project, session_id, "running")
        ocr = await asyncio.to_thread(self._ocr_row, db, session_id)
        if ocr.pages_total == 0:
            self._publish(key, {"type": "phase", "phase": "rasterise"})
            try:
                source = await asyncio.to_thread(ocr_store.source_path, project.root, db, ocr)
                folder = ocr_store.pages_dir(project.root, ocr)
                images = await asyncio.to_thread(rasterise, source, folder)
            except (NotAPageSource, ocr_store.CannotOpen) as exc:
                await asyncio.to_thread(
                    self._set_status, project, session_id, "failed", error=str(exc)
                )
                return
            await asyncio.to_thread(ocr_store.add_pages, db, session_id, images)
        self._publish(key, {"type": "phase", "phase": "recognise"})
        while (page := await asyncio.to_thread(self._next_page, db, session_id)) is not None:
            if key in self._pausing:
                await asyncio.to_thread(
                    self._set_status, project, session_id, "paused", reason=PAUSED
                )
                return
            lease_key = page.lease_key or ("ocr.hand" if page.handwritten else "ocr")
            try:
                _role, entry = pick(lease_key, self.runtime.tier)
                await self._wait_for_the_chat(key, lease_key)
                if key in self._pausing:
                    continue  # the check above pauses
                await asyncio.to_thread(self._mark_reading, db, page.id)
                self._publish(key, {"type": "page", **page_dict(page), "status": "recognising"})
                async with self.runtime.lease(lease_key, Priority.JOB) as lease:
                    reading = await recognise(
                        lease.client,
                        ocr_store.page_image(project.root, ocr, page),
                        prompt=entry.ocr.prompt,
                        max_side=entry.ocr.max_side,
                    )
            except (NotAvailable, RunnerError) as exc:
                # The model can't be loaded: every page would fail the same way. The session
                # stops, its pages stay to be read, and Resume tries again.
                await asyncio.to_thread(self._unmark_reading, db, page.id)
                error = str(exc).splitlines()[0]
                await asyncio.to_thread(
                    self._set_status, project, session_id, "failed", error=error
                )
                return
            except RecogniseFailed as exc:
                page = await asyncio.to_thread(ocr_store.page_failed, db, page.id, str(exc))
                self._publish(key, {"type": "page", **page_dict(page)})
                continue
            page = await asyncio.to_thread(
                ocr_store.save_reading, project.root, db, ocr, page.id,
                model_id=lease.model_id, reading=reading,
            )  # fmt: skip
            self._publish(key, {"type": "page", **page_dict(page)})
        await asyncio.to_thread(self._set_status, project, session_id, "done")

    async def _wait_for_the_chat(self, key: Key, lease_key: str) -> None:
        """Before loading the OCR model back after a chat, wait until the chat has been idle
        for CHAT_IDLE_RESUME seconds. Only when loading it would unload the chat's model: a
        model that's loaded, or fits beside it, is used at once."""
        told = False
        while (
            self.runtime.needs_room(lease_key)
            and self.runtime.chat_idle_seconds() < CHAT_IDLE_RESUME
            and key not in self._pausing
        ):
            if not told:
                self._publish(key, {"type": "waiting", "reason": "chat"})
                told = True
            await self._sleep(IDLE_POLL)

    @staticmethod
    def _next_page(db, session_id: int) -> OcrPageRow | None:  # noqa: ANN001
        """The first page still to read. Asked again after every page, so a page set to be
        read again while the job runs is picked up."""
        with db() as session:
            return session.scalars(
                select(OcrPageRow)
                .where(
                    OcrPageRow.session_id == session_id,
                    OcrPageRow.status.in_(("pending", "recognising")),
                )
                .order_by(OcrPageRow.number)
                .limit(1)
            ).first()

    @staticmethod
    def _ocr_row(db, session_id: int) -> OcrSessionRow:  # noqa: ANN001
        with db() as session:
            return session.get(OcrSessionRow, session_id)

    @staticmethod
    def _mark_reading(db, page_id: int) -> None:  # noqa: ANN001
        with db.begin() as session:
            session.get(OcrPageRow, page_id).status = "recognising"

    @staticmethod
    def _unmark_reading(db, page_id: int) -> None:  # noqa: ANN001
        with db.begin() as session:
            session.get(OcrPageRow, page_id).status = "pending"

    def _set_status(
        self,
        project: Project,
        session_id: int,
        status: str,
        *,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.projects.db(project).begin() as session:
            ocr = session.get(OcrSessionRow, session_id)
            ocr.status, ocr.paused_reason, ocr.error = status, reason, error
            ocr.updated_at = now()
        event = {"type": "session", "status": status, "reason": reason, "error": error}
        self._publish((project.id, session_id), event)

    def _publish(self, key: Key, event: dict) -> None:
        self.channel(key).publish(event)
