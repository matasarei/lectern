"""Model downloads from Hugging Face: resumable, checked against the catalog's sha256, and run
in the background so the request that starts one returns at once.

Each file is written to `<models>/<model id>/<file>.part`. An interrupted download resumes
from the `.part` with an HTTP Range request. Only a file whose sha256 matches the catalog is
renamed into place, so a file without `.part` is always a verified one.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx2
from sqlalchemy.orm import Session, sessionmaker

from lectern.catalog.catalog import CatalogEntry, ModelFile
from lectern.core.tables import DownloadRow
from lectern.downloads.fetch import DownloadError, fetch_verified

HF_BASE_URL = "https://huggingface.co"
DISK_HEADROOM = 1024**3  # keep 1 GiB free after the download
PROGRESS_EVERY = 0.25  # seconds between progress events
SAVE_EVERY = 1.0  # seconds between database writes
ACTIVE = ("queued", "downloading", "verifying")


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


class EventHub:
    """Fan-out of download events to any number of listeners (SSE connections)."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[dict]] = set()

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
        """After a restart: start again every download that was still running."""
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
        return resumed

    async def stop_all(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
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
