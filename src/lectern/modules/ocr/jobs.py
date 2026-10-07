"""The OCR job: an OCR session's pages, read in the background.

Starting a session returns at once; the job runs as a server task and the tab follows it on
the session's events, like a chat turn. It runs in phases (blueprint: "OCR runs in phases"):

1. Rasterise the source into page images, once.
2. Read each page through its model: `ocr`, `ocr.hand` for a page marked handwritten, or the
   `role@model` chosen for it. The model is leased per page, so a chat gets the GPU after the
   current page. When loading the OCR model back would unload the chat's, the runtime holds
   the lease until the chat has been idle for CHAT_IDLE_RESUME seconds (see
   runtime/supervisor.py): a conversation shouldn't cost two model swaps per reply.

One job runs at a time; other sessions wait as queued. Pause stops after the current page.
A job interrupted by a restart isn't started again on its own: the session shows as paused
("Lectern restarted") the next time it's read, and continues from the first page not done
when Resume is pressed.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx2
from sqlalchemy import select

from lectern.agent.turns import Channel
from lectern.modules.ocr import store as ocr_store
from lectern.modules.ocr.clean import CleanFailed, clean_page
from lectern.modules.ocr.doubts import mark_unsure, unsure_words
from lectern.modules.ocr.pages import NotAPageSource, rasterise
from lectern.modules.ocr.recognise import RecogniseFailed, recognise
from lectern.projects.store import Project, ProjectStore
from lectern.projects.tables import OcrPageRow, OcrSessionRow, SessionRow, now
from lectern.runners.base import RunnerError
from lectern.runtime.supervisor import ModelRuntime, NotAvailable, Priority
from lectern.runtime.wiring import pick

RESTARTED = "Lectern restarted"
PAUSED = "Paused"

log = logging.getLogger(__name__)
T = TypeVar("T")
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
        "clean_status": page.clean_status,
        "clean_error": page.clean_error,
    }


@dataclass
class _Queued:
    key: Key
    project: Project


class OcrJobs:
    def __init__(self, runtime: ModelRuntime, projects: ProjectStore) -> None:
        self.runtime = runtime
        self.projects = projects
        self._channels: dict[Key, Channel] = {}
        self._queue: collections.deque[_Queued] = collections.deque()
        self._current: Key | None = None
        self._pausing: set[Key] = set()
        self._starting: set[Key] = set()
        self._pause_signals: dict[Key, asyncio.Event] = {}
        # A paused session asked to read some pages again reads only those, then is paused
        # again with its reason: re-reading one page mustn't restart the whole batch.
        self._only: dict[Key, tuple[set[int], str | None]] = {}
        self._worker: asyncio.Task[None] | None = None
        # Told when a session ends done or failed: (project, session id, status, notice).
        # The app points it at the chat's follow-ups (TurnManager.after_session).
        self.on_status: Callable[[Project, int, str, str], Awaitable[None]] | None = None

    def channel(self, key: Key) -> Channel:
        return self._channels.setdefault(key, Channel())

    def active(self, key: Key) -> bool:
        """This process is running or holding the session's job."""
        return (
            key == self._current or key in self._starting or any(q.key == key for q in self._queue)
        )

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

    def summary(self, project: Project, session_id: int) -> dict:
        """What Overview shows of an OCR session: its pages, where its job stands, its output
        folder, and its Document's version, open questions and image regions."""
        ocr = self.settle(project, session_id)
        open_doubts, figures = ocr_store.document_counts(project.root, ocr)
        return {
            "pages": ocr.pages_total,
            "status": ocr.status,
            "folder": ocr.folder,
            "document_version": ocr.document_version,
            "open_doubts": open_doubts,
            "figures": figures,
        }

    # --- starting, pausing, resuming -----------------------------------------------------

    async def start(self, project: Project, session_id: int) -> None:
        """Queue a session's job (a new session, a Resume, or a page to read again)."""
        key = (project.id, session_id)
        self._pausing.discard(key)
        self._pause_signal(key).clear()
        if not self.active(key):
            # "queued" is written before the job can be taken up, so it can't land after the
            # worker's "running". Marked first, so a second start can't queue it twice.
            self._starting.add(key)
            try:
                await asyncio.to_thread(self._set_status, project, session_id, "queued")
                self._queue.append(_Queued(key, project))
            finally:
                self._starting.discard(key)
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
            self._pause_signal(key).set()
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

        key = (project.id, session_id)
        ocr = await asyncio.to_thread(self.settle, project, session_id)
        page = await asyncio.to_thread(reset)
        self.channel(key).publish({"type": "page", **page_dict(page)})
        if ocr.status == "paused" or key in self._only:
            numbers, reason = self._only.get(key, (set(), ocr.paused_reason))
            self._only[key] = (numbers | {number}, reason)
        await self.start(project, session_id)
        return page

    async def reclean(self, project: Project, session_id: int, number: int) -> OcrPageRow:
        """Clean one page again (its note changed), without reading it, then assemble."""

        def reset() -> OcrPageRow:
            with self.projects.db(project).begin() as session:
                page = session.scalars(
                    select(OcrPageRow).where(
                        OcrPageRow.session_id == session_id, OcrPageRow.number == number
                    )
                ).one()
                page.clean_status, page.clean_error = "pending", None
                page.updated_at = now()
                return page

        key = (project.id, session_id)
        ocr = await asyncio.to_thread(self.settle, project, session_id)
        page = await asyncio.to_thread(reset)
        self.channel(key).publish({"type": "page", **page_dict(page)})
        if ocr.status == "paused" or key in self._only:
            numbers, reason = self._only.get(key, (set(), ocr.paused_reason))
            self._only[key] = (numbers | {number}, reason)
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
                self._pause_signal(queued.key).clear()
                self._only.pop(queued.key, None)
            await self._tell(queued.project, queued.key[1])

    async def _tell(self, project: Project, session_id: int) -> None:
        """A job ended done or failed: tell `on_status` (the chat's follow-ups), with a line
        saying what happened. A paused one isn't over, so it says nothing."""
        if self.on_status is None:
            return

        def notice() -> tuple[str, str]:
            summary = self.summary(project, session_id)
            with self.projects.db(project)() as session:
                title = session.get(SessionRow, session_id).title
                error = session.get(OcrSessionRow, session_id).error
            if summary["status"] != "done":
                return summary["status"], f"OCR · {title} stopped: {error or 'it failed'}."
            count = summary["open_doubts"]
            questions = f"{count} open question{'s' if count != 1 else ''}"
            pages = f"{summary['pages']} page{'s' if summary['pages'] != 1 else ''}"
            return "done", f"OCR · {title} finished · {pages} · {questions}."

        status, text = await asyncio.to_thread(notice)
        if status in ("done", "failed"):
            try:
                await self.on_status(project, session_id, status, text)
            except Exception:  # noqa: BLE001 — a follow-up's problem mustn't stop the queue
                log.exception("Telling about OCR session %s failed", session_id)

    async def _run(self, project: Project, session_id: int) -> None:
        """The phases in order; each returns False when the session stopped (paused or
        failed) and the rest must wait."""
        key = (project.id, session_id)
        db = await asyncio.to_thread(self.projects.db, project)
        await asyncio.to_thread(self._set_status, project, session_id, "running")
        ocr = await asyncio.to_thread(self._ocr_row, db, session_id)
        for phase in (self._rasterise, self._recognise, self._clean, self._assemble):
            if not await phase(project, db, ocr):
                return
        if key in self._only:  # it was paused: it stays paused, the rest of its pages to read
            reason = self._only[key][1]
            await asyncio.to_thread(self._set_status, project, session_id, "paused", reason=reason)
            return
        await asyncio.to_thread(self._set_status, project, session_id, "done")

    async def _stop(self, project: Project, ocr: OcrSessionRow, status: str, **kw: str) -> bool:
        await asyncio.to_thread(self._set_status, project, ocr.session_id, status, **kw)
        return False

    async def _rasterise(self, project: Project, db, ocr: OcrSessionRow) -> bool:  # noqa: ANN001
        if ocr.pages_total:
            return True
        self._publish((project.id, ocr.session_id), {"type": "phase", "phase": "rasterise"})
        try:
            source = await asyncio.to_thread(ocr_store.source_path, project.root, db, ocr)
            folder = ocr_store.pages_dir(project.root, ocr)
            images = await asyncio.to_thread(rasterise, source, folder)
        except (NotAPageSource, ocr_store.CannotOpen) as exc:
            return await self._stop(project, ocr, "failed", error=str(exc))
        await asyncio.to_thread(ocr_store.add_pages, db, ocr.session_id, images)
        return True

    async def _recognise(self, project: Project, db, ocr: OcrSessionRow) -> bool:  # noqa: ANN001
        key = (project.id, ocr.session_id)
        self._publish(key, {"type": "phase", "phase": "recognise"})
        while (
            page := await asyncio.to_thread(
                self._next_page, db, ocr.session_id, self._only_pages(key)
            )
        ) is not None:
            if key in self._pausing:
                return await self._stop(project, ocr, "paused", reason=PAUSED)
            lease_key = page.lease_key or ("ocr.hand" if page.handwritten else "ocr")
            image = ocr_store.page_image(project.root, ocr, page)
            try:
                _role, entry = pick(lease_key, self.runtime.tier)
                if self._held_for_the_chat(lease_key):
                    self._publish(key, {"type": "waiting", "reason": "chat"})
                await asyncio.to_thread(self._mark_reading, db, page.id)
                self._publish(key, {"type": "page", **page_dict(page), "status": "recognising"})
                settings = entry.ocr
                read = await self._leased_unless_paused(
                    key,
                    lease_key,
                    functools.partial(
                        recognise, image=image, prompt=settings.prompt, max_side=settings.max_side
                    ),
                )
                if read is None:  # paused while waiting for the model: this page wasn't read
                    await asyncio.to_thread(self._unmark_reading, db, page.id)
                    self._publish(key, {"type": "page", **page_dict(page)})
                    continue  # the check at the top pauses
                model_id, reading = read
            except (NotAvailable, RunnerError) as exc:
                # The model can't be loaded: every page would fail the same way. The session
                # stops, its pages stay to be read, and Resume tries again.
                await asyncio.to_thread(self._unmark_reading, db, page.id)
                return await self._stop(project, ocr, "failed", error=str(exc).splitlines()[0])
            except RecogniseFailed as exc:
                page = await asyncio.to_thread(ocr_store.page_failed, db, page.id, str(exc))
                self._publish(key, {"type": "page", **page_dict(page)})
                continue
            page = await asyncio.to_thread(
                ocr_store.save_reading, project.root, db, ocr, page.id,
                model_id=model_id, reading=reading,
            )  # fmt: skip
            self._publish(key, {"type": "page", **page_dict(page)})
        return True

    async def _clean(self, project: Project, db, ocr: OcrSessionRow) -> bool:  # noqa: ANN001
        """Each page read since it was last cleaned goes through the main model."""
        key = (project.id, ocr.session_id)
        self._publish(key, {"type": "phase", "phase": "clean"})
        while (
            page := await asyncio.to_thread(
                self._next_to_clean, db, ocr.session_id, self._only_pages(key)
            )
        ) is not None:
            if key in self._pausing:
                return await self._stop(project, ocr, "paused", reason=PAUSED)
            raw, before, after = await asyncio.to_thread(self._texts_around, project, db, ocr, page)
            tokens = await asyncio.to_thread(ocr_store.page_tokens, project.root, ocr, page)
            decided = await asyncio.to_thread(ocr_store.decisions, db, ocr.session_id)
            marked = mark_unsure(raw, unsure_words(tokens))
            try:
                if self._held_for_the_chat("main"):
                    self._publish(key, {"type": "waiting", "reason": "chat"})
                page = await asyncio.to_thread(ocr_store.mark_cleaning, db, page.id, "cleaning")
                self._publish(key, {"type": "page", **page_dict(page)})
                cleaned = await self._leased_unless_paused(
                    key,
                    "main",
                    functools.partial(
                        clean_page,
                        number=page.number,
                        raw=marked,
                        note=page.note,
                        before=before,
                        after=after,
                        confirmed=[(d.raw, d.value) for d in decided],
                    ),
                )
                if cleaned is None:  # paused while waiting for the model
                    page = await asyncio.to_thread(ocr_store.mark_cleaning, db, page.id, "pending")
                    self._publish(key, {"type": "page", **page_dict(page)})
                    continue
                _model, result = cleaned
                page = await asyncio.to_thread(
                    ocr_store.save_cleanup, project.root, db, ocr, page.id, result
                )
            except (NotAvailable, RunnerError) as exc:
                await asyncio.to_thread(ocr_store.mark_cleaning, db, page.id, "pending")
                return await self._stop(project, ocr, "failed", error=str(exc).splitlines()[0])
            except CleanFailed as exc:
                page = await asyncio.to_thread(ocr_store.cleanup_failed, db, page.id, str(exc))
            self._publish(key, {"type": "page", **page_dict(page)})
        return True

    async def _assemble(self, project: Project, db, ocr: OcrSessionRow) -> bool:  # noqa: ANN001
        key = (project.id, ocr.session_id)
        if key in self._only:  # one page of a paused session: Resume assembles them all
            return True
        self._publish(key, {"type": "phase", "phase": "assemble"})

        def build() -> OcrSessionRow:
            with db() as session:
                owner = session.get(SessionRow, ocr.session_id).owner_id
            published, _unplaced = ocr_store.redo_document(
                project.root, db, ocr.session_id, owner_id=owner
            )
            return published

        published = await asyncio.to_thread(build)
        self._publish(
            key,
            {
                "type": "document",
                "version": published.document_version,
                "artifact_id": published.document_artifact_id,
            },
        )
        return True

    async def _leased_unless_paused(
        self, key: Key, lease_key: str, work: Callable[[httpx2.AsyncClient], Awaitable[T]]
    ) -> tuple[str, T] | None:
        """Lease the model and do the work with it, or None when a pause comes first. Waiting
        for the model can take minutes (the runtime holds it for the chat), and Pause
        shouldn't wait that out; once the model is ours the page is done to the end."""
        leased = asyncio.Event()

        async def run() -> tuple[str, T]:
            async with self.runtime.lease(lease_key, Priority.JOB) as lease:
                leased.set()
                return lease.model_id, await work(lease.client)

        worker = asyncio.create_task(run())
        pause = asyncio.create_task(self._pause_signal(key).wait())
        got = asyncio.create_task(leased.wait())
        await asyncio.wait({worker, pause, got}, return_when=asyncio.FIRST_COMPLETED)
        got.cancel()
        pause.cancel()
        if not leased.is_set() and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            return None
        return await worker

    def _texts_around(
        self,
        project: Project,
        db,
        ocr: OcrSessionRow,
        page: OcrPageRow,  # noqa: ANN001
    ) -> tuple[str, str, str]:
        """The page's raw text, and its neighbours' (for words and tables crossing a break)."""
        by_number = {p.number: p for p in ocr_store.pages(db, ocr.session_id)}

        def text_of(number: int) -> str:
            other = by_number.get(number)
            return (ocr_store.page_text(project.root, ocr, other) or "") if other else ""

        return text_of(page.number), text_of(page.number - 1), text_of(page.number + 1)

    def _pause_signal(self, key: Key) -> asyncio.Event:
        return self._pause_signals.setdefault(key, asyncio.Event())

    def _only_pages(self, key: Key) -> set[int] | None:
        return self._only[key][0] if key in self._only else None

    def _held_for_the_chat(self, lease_key: str) -> bool:
        """The runtime will hold this page's lease for the chat: tell the tab why it waits."""
        runtime = self.runtime
        return (
            runtime.chat_idle_resume > 0
            and runtime.chat_idle_seconds() < runtime.chat_idle_resume
            and runtime.needs_room(lease_key)
        )

    @staticmethod
    def _next_page(  # noqa: ANN001 — db is a sessionmaker
        db, session_id: int, numbers: set[int] | None = None
    ) -> OcrPageRow | None:
        """The first page still to read (of `numbers`, when given). Asked again after every
        page, so a page set to be read again while the job runs is picked up."""
        query = select(OcrPageRow).where(
            OcrPageRow.session_id == session_id,
            OcrPageRow.status.in_(("pending", "recognising")),
        )
        if numbers is not None:
            query = query.where(OcrPageRow.number.in_(numbers))
        with db() as session:
            return session.scalars(query.order_by(OcrPageRow.number).limit(1)).first()

    @staticmethod
    def _next_to_clean(  # noqa: ANN001 — db is a sessionmaker
        db, session_id: int, numbers: set[int] | None = None
    ) -> OcrPageRow | None:
        """The first page whose current reading hasn't been cleaned (a reading done since, or
        a cleanup cut short)."""
        query = select(OcrPageRow).where(
            OcrPageRow.session_id == session_id,
            OcrPageRow.status == "done",
            (OcrPageRow.clean_status.in_(("pending", "cleaning")))
            | (OcrPageRow.cleaned_reading != OcrPageRow.reading),
        )
        if numbers is not None:
            query = query.where(OcrPageRow.number.in_(numbers))
        with db() as session:
            return session.scalars(query.order_by(OcrPageRow.number).limit(1)).first()

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
