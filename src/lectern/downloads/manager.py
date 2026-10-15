"""Model downloads from Hugging Face: resumable, checked against the catalog's sha256, and run
in the background so the request that starts one returns at once.

Each file is written to `<models>/<model id>/<file>.part`. An interrupted download resumes
from the `.part` with an HTTP Range request. Only a file whose sha256 matches the catalog is
renamed into place, so a file without `.part` is always a verified one.

An update, for a model the catalog now pins at a newer revision, is built beside the model in
`<models>/<model id>.update/` while the old copy goes on being used, and takes its place in
one step when nothing has the model loaded (`_update`, `_swap`, and `_recover` for a start
that finds the step cut short).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx2
from sqlalchemy.orm import Session, sessionmaker

from lectern.catalog.catalog import CatalogEntry, ModelFile
from lectern.core.tables import DownloadRow
from lectern.downloads.fetch import DownloadError, _hash_file, fetch_verified

HF_BASE_URL = "https://huggingface.co"
DISK_HEADROOM = 1024**3  # keep 1 GiB free after the download
PROGRESS_EVERY = 0.25  # seconds between progress events
SAVE_EVERY = 1.0  # seconds between database writes
ACTIVE = ("queued", "downloading", "verifying")
UPDATE_SUFFIX = ".update"  # <id>.update: a newer revision being built beside the model
OLD_SUFFIX = ".old"  # <id>.old: the revision being replaced, for the moment of the swap
UPDATE_MARKER = ".for-revision"  # in an update's folder: the revision it's being built for


@dataclass(frozen=True)
class DownloadState:
    model_id: str
    status: str
    bytes_done: int
    bytes_total: int
    rate: float = 0.0  # bytes per second
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Installed:
    """What of a model is on disk."""

    revision: str | None  # the catalog revision it was downloaded at; None: not recorded
    at: datetime | None  # when its download finished
    bytes: int  # the room its folder takes, a half-fetched file included


@dataclass(frozen=True)
class UpdateState:
    """How far a model's update is, or how it failed."""

    model_id: str
    # "checking" (what's on disk against the new pin), "updating" (fetching what differs),
    # "waiting" (all fetched: for the model to be free), or "failed"
    status: str
    bytes_done: int
    bytes_total: int
    rate: float = 0.0  # bytes per second
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class NothingToUpdate(Exception):
    """The model isn't on disk at a recorded revision other than the catalog's."""


class NoSuchFolder(Exception):
    """Remove was given a name that isn't in the models directory."""


class NotRemovable(Exception):
    """The name is in the models directory, and isn't Lectern's to delete."""


class EventHub:
    """Fan-out of download events to any number of listeners (SSE connections)."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[dict]] = set()

    @property
    def listening(self) -> int:
        """How many listen now."""
        return len(self._queues)

    def publish(self, event: dict) -> None:
        for queue in list(self._queues):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @contextlib.asynccontextmanager
    async def listen(self) -> AsyncIterator[asyncio.Queue[dict]]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._queues.add(queue)
        try:
            yield queue
        finally:
            self._queues.discard(queue)


async def _at_once(model_id: str, action: Callable[[], Awaitable[None]]) -> None:
    await action()


def _default_client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(follow_redirects=True, timeout=httpx2.Timeout(30.0, read=120.0))


class DownloadManager:
    def __init__(
        self,
        models_dir: Path,
        sessions: sessionmaker[Session],
        *,
        client_factory: Callable[[], httpx2.AsyncClient] = _default_client,
        base_url: str = HF_BASE_URL,
        disk_free: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free,
    ) -> None:
        self.models_dir = models_dir
        self.sessions = sessions
        self.client_factory = client_factory
        self.base_url = base_url.rstrip("/")
        self.disk_free = disk_free
        self.events = EventHub()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._update_tasks: dict[str, asyncio.Task[None]] = {}
        self._updates: dict[str, UpdateState] = {}  # running, or how the last one failed
        # Runs the swap that ends an update, once nothing has the model loaded. The app sets
        # it to the runtime's `without_model`; on its own the manager has nothing to wait for.
        self.exclusive: Callable[[str, Callable[[], Awaitable[None]]], Awaitable[None]] = _at_once

    # --- paths and state -------------------------------------------------------------

    def model_dir(self, entry: CatalogEntry) -> Path:
        return self.models_dir / entry.id

    def file_path(self, entry: CatalogEntry, file: ModelFile) -> Path:
        return self.model_dir(entry) / file.path

    def _bytes_on_disk(self, entry: CatalogEntry) -> int:
        total = 0
        for file in entry.files.all():
            final = self.file_path(entry, file)
            part = final.with_name(final.name + ".part")
            if final.exists():
                total += file.size
            elif part.exists():
                total += min(part.stat().st_size, file.size)
        return total

    def is_downloaded(self, entry: CatalogEntry) -> bool:
        return all(self.file_path(entry, f).exists() for f in entry.files.all())

    def installed(self, entry: CatalogEntry) -> Installed | None:
        """What of the model is on disk, or None when nothing is. A download that stopped
        halfway, and files nobody downloaded through Lectern, take room and have no version."""
        size = _folder_bytes(self.model_dir(entry))
        if not size:
            return None
        with self.sessions() as session:
            row = session.get(DownloadRow, entry.id)
        if row is None or row.status != "done":
            return Installed(revision=None, at=None, bytes=size)
        # The revision that was downloaded, also when the catalog now pins one with a file
        # this one lacks: it's that older revision that is on disk.
        return Installed(revision=row.revision, at=row.updated_at, bytes=size)

    def unlisted(self, catalog_ids: Collection[str]) -> list[tuple[str, int]]:
        """Folders in the models directory that no catalog model owns, with their size: a
        model a later Lectern no longer offers still takes its room."""
        if not self.models_dir.is_dir():
            return []
        # A model's own folders while it's updated aren't strays.
        ours = {f"{i}{suffix}" for i in catalog_ids for suffix in ("", UPDATE_SUFFIX, OLD_SUFFIX)}
        return sorted(
            (path.name, _folder_bytes(path))
            for path in self.models_dir.iterdir()
            if path.is_dir() and not path.is_symlink() and path.name not in ours
        )

    def state(self, entry: CatalogEntry) -> DownloadState:
        with self.sessions() as session:
            row = session.get(DownloadRow, entry.id)
        if row is None or (row.status == "done" and not self.is_downloaded(entry)):
            # No record, or the files were removed after a finished download: trust the disk.
            status = "done" if self.is_downloaded(entry) else "none"
            return DownloadState(entry.id, status, self._bytes_on_disk(entry), entry.size)
        return DownloadState(entry.id, row.status, row.bytes_done, row.bytes_total, error=row.error)

    def _save(self, entry_id: str, **fields: object) -> None:
        with self.sessions.begin() as session:
            row = session.get(DownloadRow, entry_id)
            if row is None:
                row = DownloadRow(model_id=entry_id, status="queued", bytes_done=0, bytes_total=0)
                session.add(row)
            for key, value in fields.items():
                setattr(row, key, value)

    def _publish(self, state: DownloadState) -> None:
        self.events.publish({"type": "download", **state.to_dict()})

    # --- starting and running --------------------------------------------------------

    def is_running(self, model_id: str) -> bool:
        task = self._tasks.get(model_id)
        return task is not None and not task.done()

    def start(self, entry: CatalogEntry) -> DownloadState:
        """Start (or resume) a download in the background and return at once."""
        if self.is_running(entry.id):
            return self.state(entry)
        if self.is_downloaded(entry):
            self._save(
                entry.id, status="done", bytes_done=entry.size, bytes_total=entry.size, error=None
            )
            return self.state(entry)
        if self.update_available(entry):
            # An older revision is on disk and the pin has a file it lacks. Fetching just
            # that file would leave the older ones in place as the pinned revision's.
            self.start_update(entry)
            return self.state(entry)

        done = self._bytes_on_disk(entry)
        remaining = entry.size - done
        self.models_dir.mkdir(parents=True, exist_ok=True)
        free = self.disk_free(self.models_dir)
        if free < remaining + DISK_HEADROOM:
            error = (
                f"Not enough disk space: {remaining / 1e9:.1f} GB needed plus 1 GB to spare, "
                f"{free / 1e9:.1f} GB free."
            )
            self._save(
                entry.id, status="failed", bytes_done=done, bytes_total=entry.size, error=error
            )
            state = self.state(entry)
            self._publish(state)
            return state

        self._save(entry.id, status="queued", bytes_done=done, bytes_total=entry.size, error=None)
        self._tasks[entry.id] = asyncio.get_running_loop().create_task(self._run(entry))
        state = self.state(entry)
        self._publish(state)
        return state

    def resume_interrupted(self, entries: dict[str, CatalogEntry]) -> list[str]:
        """After a restart: start again every download that was still running, and go on
        with every update that was."""
        updating = self._recover(entries)
        with self.sessions() as session:
            ids = [
                r.model_id
                for r in session.query(DownloadRow).filter(DownloadRow.status.in_(ACTIVE))
            ]
        resumed = []
        for model_id in ids:
            entry = entries.get(model_id)
            if entry is not None:
                self.start(entry)
                resumed.append(model_id)
        for entry in updating:
            self.start_update(entry)
            resumed.append(entry.id)
        return resumed

    async def stop(self, model_id: str) -> None:
        """Stop a model's download, if one is running, and wait until it has."""
        task = self._tasks.get(model_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def removable(self, name: str) -> Path:
        """The folder Remove would delete: a real folder directly in the models directory,
        with exactly this name. It is looked up in the directory's own listing, so the name
        is only ever compared, never joined to a path: "..", a path or anything else that
        isn't there finds nothing. A link is refused: what's behind it isn't ours."""
        listing = self.models_dir.iterdir() if self.models_dir.is_dir() else ()
        for path in listing:
            if path.name != name:
                continue
            if path.is_symlink():
                raise NotRemovable(
                    f"{name} is a link to a folder somewhere else. Lectern doesn't delete "
                    "through a link: remove the link yourself."
                )
            if not path.is_dir():
                raise NotRemovable(f"{name} isn't a folder.")
            return path
        raise NoSuchFolder(f"There's no model folder {name!r}.")

    async def remove(self, name: str) -> None:
        """Delete a model's folder and its record, stopping its download first. The caller
        makes sure no runner has the model loaded (ModelRuntime.without_model)."""
        folder = self.removable(name)
        await self.stop(name)
        await self.stop_update(name)
        await asyncio.to_thread(self._delete, folder, name)
        self._publish(DownloadState(name, "none", 0, 0))  # open pages show it gone

    def _delete(self, folder: Path, name: str) -> None:
        shutil.rmtree(folder)
        for suffix in (UPDATE_SUFFIX, OLD_SUFFIX):  # what an update of it had built so far
            beside = folder.with_name(folder.name + suffix)
            if beside.is_dir() and not beside.is_symlink():
                shutil.rmtree(beside)
        with self.sessions.begin() as session:
            row = session.get(DownloadRow, name)
            if row is not None:
                session.delete(row)

    # --- updating a model to the revision the catalog pins now -------------------------------

    def _staging(self, entry: CatalogEntry) -> Path:
        return self.model_dir(entry).with_name(entry.id + UPDATE_SUFFIX)

    def update_available(self, entry: CatalogEntry) -> bool:
        """The catalog pins another revision than the one on disk. Only for a finished
        download whose revision is recorded: files of an unknown revision may already be
        the pinned one. Not for a folder that is a link: it isn't ours to replace.

        Whether every file of the pinned revision is there doesn't come into it. A pin that
        adds a file leaves the model "not downloaded" by the new list, and it is still an
        update that brings it up to date: a plain download would fetch the missing file,
        keep the older ones for the pinned revision's, and record what it never checked."""
        with self.sessions() as session:
            row = session.get(DownloadRow, entry.id)
        folder = self.model_dir(entry)
        return (
            row is not None
            and row.status == "done"
            and row.revision is not None
            and row.revision != entry.revision
            and folder.is_dir()
            and not folder.is_symlink()
        )

    def update_state(self, model_id: str) -> UpdateState | None:
        """The model's update: running, failed, or None (none, or it finished)."""
        return self._updates.get(model_id)

    def start_update(self, entry: CatalogEntry) -> UpdateState:
        """Start (or go on with) the model's update in the background and return at once."""
        task = self._update_tasks.get(entry.id)
        if task is not None and not task.done():
            return self._updates[entry.id]
        if not self.update_available(entry):
            raise NothingToUpdate(f"{entry.title} has no update.")
        so_far = self._staged_bytes(entry) if self._staged_for(entry) else 0
        state = UpdateState(entry.id, "checking", so_far, entry.size)
        self._updates[entry.id] = state
        self._update_tasks[entry.id] = asyncio.get_running_loop().create_task(self._update(entry))
        return state

    async def stop_update(self, model_id: str) -> None:
        """Stop a model's update, if one is running, and wait until it has. What it fetched
        stays in its folder."""
        task = self._update_tasks.get(model_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._updates.pop(model_id, None)

    async def _update(self, entry: CatalogEntry) -> None:
        """Build the pinned revision in the model's `.update` folder, then put it in the
        model's place. The copy in use isn't touched until that last step."""
        await asyncio.to_thread(self._claim_staging, entry)
        progress = _Progress(entry, self._staged_bytes(entry))
        try:
            async with self.client_factory() as client:
                # First what the new revision shares with the old: nothing to fetch for it.
                changed = [
                    file
                    for file in entry.files.all()
                    if not await self._carry_over(entry, file, progress)
                ]
                self._needs_room(entry, changed)
                self._note(progress, "updating", at_once=True)
                for file in changed:
                    await self._fetch_update(client, entry, file, progress)
            self._note(progress, "waiting", at_once=True)
            await self.exclusive(entry.id, lambda: asyncio.to_thread(self._swap, entry))
        except asyncio.CancelledError:
            self._updates.pop(entry.id, None)  # the folder stays: the next start goes on
            raise
        except (DownloadError, httpx2.HTTPError, OSError) as exc:
            self._updates[entry.id] = UpdateState(
                entry.id, "failed", progress.done, entry.size, error=_describe(exc)
            )
        else:
            self._updates.pop(entry.id, None)
        state = self.state(entry)  # for open pages, which read the list again
        failed = self._updates.get(entry.id)
        if failed is not None and state.status != "done":
            # The model couldn't be used as it was (the pin has a file it lacked), so someone
            # may be waiting for its download: they're told it failed, in a download's words.
            state = DownloadState(
                entry.id, "failed", failed.bytes_done, failed.bytes_total, error=failed.error
            )
        self._publish(state)

    def _staged_for(self, entry: CatalogEntry) -> bool:
        marker = self._staging(entry) / UPDATE_MARKER
        return marker.is_file() and marker.read_text().strip() == entry.revision

    def _claim_staging(self, entry: CatalogEntry) -> None:
        """The update's folder, noted as being built for this revision. One begun for
        another pin (an update interrupted, then a Lectern that pins something newer) is
        thrown away: its files were checked against that pin's checksums, not this one's."""
        staging = self._staging(entry)
        if staging.is_dir() and not self._staged_for(entry):
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        (staging / UPDATE_MARKER).write_text(entry.revision)

    async def _carry_over(self, entry: CatalogEntry, file: ModelFile, progress: _Progress) -> bool:
        """True when the file is in the update's folder without fetching it: it's there from
        an earlier try, or the copy in use already is this revision's file and is linked in
        (copied where the disk can't link)."""
        target = self._staging(entry) / file.path
        if target.exists():
            return True
        current = self.file_path(entry, file)
        if not current.exists() or current.stat().st_size != file.size:
            return False
        if (await asyncio.to_thread(_hash_file, current)).hexdigest() != file.sha256:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_link_or_copy, current, target)
        progress.done += file.size
        self._note(progress, "checking", at_once=True)
        return True

    def _needs_room(self, entry: CatalogEntry, changed: list[ModelFile]) -> None:
        missing = sum(file.size for file in changed) - self._staged_bytes(entry, changed)
        free = self.disk_free(self.models_dir)
        if free < missing + DISK_HEADROOM:
            raise DownloadError(
                f"Not enough disk space: {missing / 1e9:.1f} GB needed plus 1 GB to spare, "
                f"{free / 1e9:.1f} GB free."
            )

    async def _fetch_update(
        self, client: httpx2.AsyncClient, entry: CatalogEntry, file: ModelFile, progress: _Progress
    ) -> None:
        def advance(delta: int) -> None:
            progress.done += delta
            if delta > 0:
                self._note(progress, "updating")

        await fetch_verified(
            client,
            f"{self.base_url}/{entry.repo}/resolve/{entry.revision}/{file.path}",
            self._staging(entry) / file.path,
            size=file.size,
            sha256=file.sha256,
            on_bytes=advance,
            label=file.path,
        )

    def _note(self, progress: _Progress, status: str, *, at_once: bool = False) -> None:
        """Say how far the update is: at a change of phase at once, while fetching no more
        often than a download does."""
        now = time.monotonic()
        if at_once or now - progress.last_event >= PROGRESS_EVERY:
            progress.update_rate(now)
            self._updates[progress.entry.id] = UpdateState(
                progress.entry.id,
                status,
                progress.done,
                progress.entry.size,
                rate=progress.rate if status == "updating" else 0.0,
            )

    def _staged_bytes(self, entry: CatalogEntry, files: list[ModelFile] | None = None) -> int:
        """How much of the update is in its folder already (of `files`, or of all)."""
        total = 0
        for file in entry.files.all() if files is None else files:
            final = self._staging(entry) / file.path
            part = final.with_name(final.name + ".part")
            if final.exists():
                total += file.size
            elif part.exists():
                total += min(part.stat().st_size, file.size)
        return total

    def _swap(self, entry: CatalogEntry) -> None:
        """The update's folder takes the model's place. Two renames and a record: a start
        that finds this cut short between any two of them puts it right (`_recover`). The
        folder's note of its revision goes last but one, so that until the record is written
        the folder itself says what it holds."""
        current, staging = self.model_dir(entry), self._staging(entry)
        old = current.with_name(entry.id + OLD_SUFFIX)
        current.rename(old)
        try:
            staging.rename(current)
        except OSError:
            old.rename(current)  # not left renamed away until the next start
            raise
        self._record_swapped(entry, entry.revision)
        _discard(old)

    def _record_swapped(self, entry: CatalogEntry, revision: str) -> None:
        self._save(
            entry.id,
            status="done",
            bytes_done=entry.size,
            bytes_total=entry.size,
            error=None,
            revision=revision,
        )
        (self.model_dir(entry) / UPDATE_MARKER).unlink(missing_ok=True)

    def _recover(self, entries: dict[str, CatalogEntry]) -> list[CatalogEntry]:
        """At start: put right a swap that was cut short, and return the models whose update
        should go on. With `.old` beside the model, the new revision had taken its place:
        the swap is finished, and if the folder still carries its note, the revision in it
        is recorded (the record hadn't been written). With `.old` and no model, it had only
        begun: it is undone, and the update's folder, still whole, is swapped in again by
        the update going on."""
        updating = []
        for entry in entries.values():
            current, staging = self.model_dir(entry), self._staging(entry)
            old = current.with_name(entry.id + OLD_SUFFIX)
            if old.is_dir() and not old.is_symlink():
                if current.exists():
                    marker = current / UPDATE_MARKER
                    if marker.is_file():
                        self._record_swapped(entry, marker.read_text().strip())
                    _discard(old)
                else:
                    old.rename(current)
            if staging.is_dir() and not staging.is_symlink():
                if self.update_available(entry):
                    updating.append(entry)
                else:
                    shutil.rmtree(staging)  # left by an update of a model that's current
        return updating

    async def stop_all(self) -> None:
        tasks = [t for t in (*self._tasks.values(), *self._update_tasks.values()) if not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self, entry: CatalogEntry) -> None:
        progress = _Progress(entry, self._bytes_on_disk(entry))
        self._save(entry.id, status="downloading")
        try:
            async with self.client_factory() as client:
                for file in entry.files.all():
                    await self._fetch(client, entry, file, progress)
        except asyncio.CancelledError:
            self._save(entry.id, status="paused", bytes_done=progress.done)
            self._publish(self.state(entry))
            raise
        except (DownloadError, httpx2.HTTPError, OSError) as exc:
            self._save(
                entry.id,
                status="failed",
                bytes_done=self._bytes_on_disk(entry),
                error=_describe(exc),
            )
            self._publish(self.state(entry))
            return
        self._save(
            entry.id, status="done", bytes_done=entry.size, error=None, revision=entry.revision
        )
        self._publish(self.state(entry))

    async def _fetch(
        self, client: httpx2.AsyncClient, entry: CatalogEntry, file: ModelFile, progress: _Progress
    ) -> None:
        def advance(delta: int) -> None:
            progress.done += delta
            if delta > 0:
                self._report(progress)

        await fetch_verified(
            client,
            f"{self.base_url}/{entry.repo}/resolve/{entry.revision}/{file.path}",
            self.file_path(entry, file),
            size=file.size,
            sha256=file.sha256,
            on_bytes=advance,
            on_verifying=lambda: self._save(entry.id, status="verifying", bytes_done=progress.done),
            label=file.path,
        )
        self._save(entry.id, status="downloading")

    def _report(self, progress: _Progress) -> None:
        now = time.monotonic()
        if now - progress.last_event >= PROGRESS_EVERY:
            progress.update_rate(now)
            self._publish(
                DownloadState(
                    progress.entry.id,
                    "downloading",
                    progress.done,
                    progress.entry.size,
                    rate=progress.rate,
                )
            )
        if now - progress.last_save >= SAVE_EVERY:
            progress.last_save = now
            self._save(progress.entry.id, bytes_done=progress.done)


def _discard(old: Path) -> None:
    """Delete the folder of the revision that was replaced. The swap is done by then, so a
    folder that can't be deleted now (a file in it still open, on Windows) doesn't undo it or
    stop Lectern starting: it stays for the next start to try again."""
    with contextlib.suppress(OSError):
        shutil.rmtree(old)


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:  # across disks, or a file system without hard links
        shutil.copy2(source, target)


def _folder_bytes(folder: Path) -> int:
    """The size of the files in a folder, at any depth; 0 for one that isn't there."""
    return sum(
        path.stat().st_size
        for path in folder.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


class _Progress:
    def __init__(self, entry: CatalogEntry, done: int) -> None:
        self.entry = entry
        self.done = done
        self.rate = 0.0
        self.last_event = 0.0
        self.last_save = time.monotonic()
        self._rate_mark = (time.monotonic(), done)

    def update_rate(self, now: float) -> None:
        then, done_then = self._rate_mark
        if now > then:
            instant = (self.done - done_then) / (now - then)
            self.rate = instant if self.rate == 0 else 0.7 * self.rate + 0.3 * instant
        self._rate_mark = (now, self.done)
        self.last_event = now


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx2.HTTPError):
        return f"Connection problem ({type(exc).__name__}). Try again to resume."
    return str(exc)
