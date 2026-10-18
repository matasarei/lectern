from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.catalog.catalog import CatalogEntry
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.core.tables import DownloadRow
from lectern.downloads.manager import (
    DISK_HEADROOM,
    UPDATE_MARKER,
    DownloadManager,
    NoSuchFolder,
    NothingToUpdate,
    NotRemovable,
)

MODEL = b"GGUF" + bytes(range(256)) * 400  # ~100 KB
MMPROJ = b"MMPROJ" + bytes(range(200)) * 50


def _entry(model: bytes = MODEL, *, sha_override: str | None = None) -> CatalogEntry:
    return CatalogEntry.model_validate(
        {
            "id": "tiny",
            "title": "Tiny",
            "quant": "Q4_K_M",
            "roles": ["main"],
            "format": "gguf",
            "repo": "acme/tiny-GGUF",
            "revision": "a" * 40,
            "licence": "Apache-2.0",
            "permissive": True,
            "files": {
                "model": [
                    {
                        "path": "tiny.gguf",
                        "size": len(model),
                        "sha256": sha_override or hashlib.sha256(model).hexdigest(),
                    }
                ],
                "mmproj": {
                    "path": "mmproj.gguf",
                    "size": len(MMPROJ),
                    "sha256": hashlib.sha256(MMPROJ).hexdigest(),
                },
            },
        }
    )


class _CutStream(httpx2.AsyncByteStream):
    """Sends `data`, then fails like a dropped connection if `cut` is set."""

    def __init__(self, data: bytes, cut: bool, gate: threading.Event | None) -> None:
        self.data, self.cut, self.gate = data, cut, gate

    async def __aiter__(self):
        step = 8192
        for i in range(0, len(self.data), step):
            while self.gate is not None and not self.gate.is_set():
                await asyncio.sleep(0.01)
            yield self.data[i : i + step]
        if self.cut:
            raise httpx2.ReadError("connection dropped")


class FakeHub:
    """A tiny stand-in for huggingface.co/<repo>/resolve/<rev>/<file> with Range support."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requests: list[httpx2.Request] = []
        self.cut_after: dict[str, int] = {}  # path -> bytes to send before dropping (once)
        self.ignore_range = False
        self.gate: threading.Event | None = None

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path.rsplit("/", 1)[-1]
        data = self.files[path]
        start = 0
        header = request.headers.get("range")
        status = 200
        if header and not self.ignore_range:
            start = int(header.removeprefix("bytes=").rstrip("-"))
            status = 206
        body = data[start:]
        cut = path in self.cut_after
        if cut:
            body = body[: self.cut_after.pop(path)]
        return httpx2.Response(status, stream=_CutStream(body, cut, self.gate))

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))


@pytest.fixture
def sessions(paths):
    engine = make_engine(paths.app_db)
    migrate(engine)
    return make_sessionmaker(engine)


def _manager(paths, sessions, hub, *, disk_free=10**12) -> DownloadManager:
    return DownloadManager(
        paths.models, sessions, client_factory=hub.client, disk_free=lambda _: disk_free
    )


async def _finish(manager: DownloadManager, entry: CatalogEntry) -> None:
    manager.start(entry)
    await manager._tasks[entry.id]


def test_downloads_all_files_and_verifies_them(paths, sessions):
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub)
    entry = _entry()

    asyncio.run(_finish(manager, entry))

    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL
    assert (paths.models / "tiny" / "mmproj.gguf").read_bytes() == MMPROJ
    assert not list((paths.models / "tiny").glob("*.part"))
    state = manager.state(entry)
    assert (state.status, state.bytes_done, state.bytes_total) == ("done", entry.size, entry.size)
    assert "/acme/tiny-GGUF/resolve/" + "a" * 40 + "/tiny.gguf" in str(hub.requests[0].url)


def _row(sessions, model_id: str) -> DownloadRow:  # noqa: ANN001
    with sessions() as session:
        return session.get(DownloadRow, model_id)


def test_a_finished_download_records_the_revision_it_fetched(paths, sessions):
    entry = _entry()
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub)

    asyncio.run(_finish(manager, entry))

    assert _row(sessions, "tiny").revision == entry.revision


def test_a_failed_download_records_no_revision(paths, sessions):
    entry = _entry(sha_override="0" * 64)
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub)

    asyncio.run(_finish(manager, entry))

    row = _row(sessions, "tiny")
    assert (row.status, row.revision) == ("failed", None)


def test_files_put_there_by_hand_have_no_recorded_revision(paths, sessions):
    entry = _entry()
    hub = FakeHub({})
    manager = _manager(paths, sessions, hub)
    for file in entry.files.all():
        path = manager.file_path(entry, file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"copied in")

    async def go():
        return manager.start(entry)

    # Nothing was fetched or checked, so which revision this is isn't known.
    assert asyncio.run(go()).status == "done"
    assert _row(sessions, "tiny").revision is None
    assert hub.requests == []


def test_resumes_after_a_dropped_connection(paths, sessions):
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    hub.cut_after["tiny.gguf"] = 40_000
    manager = _manager(paths, sessions, hub)
    entry = _entry()

    asyncio.run(_finish(manager, entry))
    failed = manager.state(entry)
    assert failed.status == "failed"
    assert "resume" in (failed.error or "")
    assert (paths.models / "tiny" / "tiny.gguf.part").stat().st_size == 40_000

    asyncio.run(_finish(manager, entry))

    assert hub.requests[-2].headers["range"] == "bytes=40000-"
    assert {r.headers["accept-encoding"] for r in hub.requests} == {"identity"}
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL
    assert manager.state(entry).status == "done"


def test_server_ignoring_range_restarts_the_file(paths, sessions):
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    hub.cut_after["tiny.gguf"] = 30_000
    manager = _manager(paths, sessions, hub)
    entry = _entry()
    asyncio.run(_finish(manager, entry))

    hub.ignore_range = True
    asyncio.run(_finish(manager, entry))

    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL
    assert manager.state(entry).status == "done"


def test_checksum_mismatch_removes_the_partial_file(paths, sessions):
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub)
    entry = _entry(sha_override="0" * 64)

    asyncio.run(_finish(manager, entry))

    state = manager.state(entry)
    assert state.status == "failed"
    assert "checksum" in (state.error or "")
    assert not (paths.models / "tiny" / "tiny.gguf").exists()
    assert not (paths.models / "tiny" / "tiny.gguf.part").exists()


def test_refuses_to_start_without_disk_space(paths, sessions):
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    entry = _entry()
    manager = _manager(paths, sessions, hub, disk_free=entry.size + DISK_HEADROOM - 1)

    async def go():
        return manager.start(entry)

    state = asyncio.run(go())

    assert state.status == "failed"
    assert "disk space" in (state.error or "")
    assert hub.requests == []


def test_already_downloaded_files_are_not_fetched_again(paths, sessions):
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub)
    entry = _entry()
    asyncio.run(_finish(manager, entry))
    count = len(hub.requests)

    async def go():
        return manager.start(entry)

    assert asyncio.run(go()).status == "done"
    assert len(hub.requests) == count


def test_progress_events_are_published(paths, sessions):
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub)
    entry = _entry()

    async def go():
        async with manager.events.listen() as queue:
            await _finish(manager, entry)
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            return events

    events = asyncio.run(go())

    assert events[0]["status"] == "queued"
    assert events[-1]["status"] == "done"
    assert all(e["type"] == "download" and e["model_id"] == "tiny" for e in events)


def test_restart_resumes_an_interrupted_download(paths, sessions):
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub)
    entry = _entry()
    manager._save(entry.id, status="downloading", bytes_done=0, bytes_total=entry.size)

    async def go():
        resumed = manager.resume_interrupted({entry.id: entry})
        await manager._tasks[entry.id]
        return resumed

    assert asyncio.run(go()) == ["tiny"]
    assert manager.state(entry).status == "done"


# --- over HTTP ---------------------------------------------------------------------------


# --- what's installed, and removing it ---------------------------------------------------------


def _installed_tiny(paths, sessions):  # noqa: ANN001, ANN202
    entry = _entry()
    manager = _manager(paths, sessions, FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ}))
    asyncio.run(_finish(manager, entry))
    return manager, entry


def test_an_installed_model_says_its_revision_its_size_and_when(paths, sessions):
    manager, entry = _installed_tiny(paths, sessions)

    installed = manager.installed(entry)

    assert installed.revision == entry.revision
    assert installed.bytes == len(MODEL) + len(MMPROJ)
    assert installed.at == _row(sessions, "tiny").updated_at


def test_a_file_that_goes_while_the_folder_is_sized_is_not_an_error(paths, sessions, monkeypatch):
    """An update's swap removes the folder's note of its revision while the model list may
    be sizing that folder: the note is seen, then gone before its size is read. Seen once
    in CI as a 500 from GET /api/models."""
    manager, entry = _installed_tiny(paths, sessions)
    note = manager.model_dir(entry) / UPDATE_MARKER
    note.write_text(entry.revision)
    real = {"stat": os.stat, "lstat": os.lstat}

    def seen_then_gone(name: str):  # noqa: ANN202
        def look(path, *args, **kwargs):  # noqa: ANN001, ANN202
            found = real[name](path, *args, **kwargs)
            if not isinstance(path, int) and os.fspath(path) == str(note):
                note.unlink(missing_ok=True)
            return found

        return look

    for name in real:
        monkeypatch.setattr(os, name, seen_then_gone(name))

    model = len(MODEL) + len(MMPROJ)
    # The note counts or not, depending on whether its size was read before it went.
    assert manager.installed(entry).bytes in (model, model + len(entry.revision))


def test_a_model_with_nothing_on_disk_is_not_installed(paths, sessions):
    manager = _manager(paths, sessions, FakeHub({}))
    assert manager.installed(_entry()) is None
    (paths.models / "tiny").mkdir(parents=True)  # a folder and no file in it
    assert manager.installed(_entry()) is None


def test_a_download_that_stopped_halfway_is_on_disk_without_a_version(paths, sessions):
    entry = _entry()
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    hub.cut_after["tiny.gguf"] = 20_000
    manager = _manager(paths, sessions, hub)
    asyncio.run(_finish(manager, entry))

    installed = manager.installed(entry)

    # It takes room and can be removed, but it isn't a version of the model yet.
    assert (installed.revision, installed.at, installed.bytes) == (None, None, 20_000)


def test_folders_no_catalog_model_owns_are_listed_apart(paths, sessions):
    manager, _ = _installed_tiny(paths, sessions)
    left = paths.models / "model-of-an-older-lectern"
    left.mkdir()
    (left / "weights.gguf").write_bytes(b"x" * 300)
    (left / "sub").mkdir()
    (left / "sub" / "more.bin").write_bytes(b"y" * 50)
    (paths.models / "a-file.txt").write_bytes(b"not a folder")
    (paths.models / "a-link").symlink_to(left, target_is_directory=True)

    assert manager.unlisted({"tiny"}) == [("model-of-an-older-lectern", 350)]


def test_removing_a_model_deletes_its_folder_and_its_record_and_says_so(paths, sessions):
    manager, entry = _installed_tiny(paths, sessions)

    async def go():
        async with manager.events.listen() as queue:
            await manager.remove("tiny")
            return [queue.get_nowait() for _ in range(queue.qsize())]

    events = asyncio.run(go())

    assert not (paths.models / "tiny").exists()
    assert _row(sessions, "tiny") is None
    assert manager.installed(entry) is None and manager.state(entry).status == "none"
    assert [(e["model_id"], e["status"]) for e in events] == [("tiny", "none")]  # pages update


def test_removing_a_model_stops_its_download_first(paths, sessions):
    entry = _entry()
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    hub.gate = threading.Event()  # the transfer hangs mid-file
    manager = _manager(paths, sessions, hub)

    async def go():
        manager.start(entry)
        await _until(lambda: list((paths.models / "tiny").glob("*.part")))
        await manager.remove("tiny")
        return manager.is_running("tiny")

    assert asyncio.run(go()) is False
    assert not (paths.models / "tiny").exists()  # nothing written back after the delete
    assert _row(sessions, "tiny") is None


def test_a_folder_no_catalog_model_owns_can_be_removed(paths, sessions):
    manager = _manager(paths, sessions, FakeHub({}))
    left = paths.models / "model-of-an-older-lectern"
    left.mkdir(parents=True)
    (left / "weights.gguf").write_bytes(b"x")

    asyncio.run(manager.remove("model-of-an-older-lectern"))

    assert not left.exists()


@pytest.mark.parametrize("name", ["..", ".", "", "tiny/..", "../models", "nope", "tiny/tiny.gguf"])
def test_remove_takes_only_the_name_of_a_folder_in_the_models_directory(paths, sessions, name):
    manager, _ = _installed_tiny(paths, sessions)
    beside = paths.models.parent / "beside.txt"
    beside.write_text("not Lectern's to delete")

    with pytest.raises(NoSuchFolder):
        asyncio.run(manager.remove(name))

    assert (paths.models / "tiny" / "tiny.gguf").exists() and beside.exists()
    assert _row(sessions, "tiny") is not None


def test_remove_never_deletes_through_a_link_or_takes_a_file_for_a_model(paths, sessions):
    manager, _ = _installed_tiny(paths, sessions)
    elsewhere = paths.models.parent / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.gguf").write_bytes(b"someone else's")
    (paths.models / "linked").symlink_to(elsewhere, target_is_directory=True)
    (paths.models / "a-file.txt").write_bytes(b"x")

    with pytest.raises(NotRemovable, match="link"):
        asyncio.run(manager.remove("linked"))
    with pytest.raises(NotRemovable, match="isn't a folder"):
        asyncio.run(manager.remove("a-file.txt"))

    assert (elsewhere / "keep.gguf").exists() and (paths.models / "linked").is_symlink()
    assert (paths.models / "a-file.txt").exists()


async def _until(condition, *, steps: int = 2000) -> None:  # noqa: ANN001
    """Let other tasks run until `condition()` holds. Counts loop turns, not seconds."""
    for _ in range(steps):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


# --- a newer revision in the catalog: Update --------------------------------------------------

NEWER = b"GGUF" + bytes(range(255, -1, -1)) * 400  # the model file of a newer revision


def _newer() -> CatalogEntry:
    """The same model as the catalog pins it after a Lectern update: the model file changed,
    its projector didn't."""
    return _entry(NEWER).model_copy(update={"revision": "b" * 40})


def _updating(paths, sessions, **hub_files):  # noqa: ANN001, ANN202
    """Tiny installed at its first revision, and a manager whose hub serves the newer one."""
    _installed_tiny(paths, sessions)
    hub = FakeHub({"tiny.gguf": NEWER, "mmproj.gguf": MMPROJ, **hub_files})
    return _manager(paths, sessions, hub), hub


async def _updated(manager: DownloadManager, entry: CatalogEntry) -> None:
    manager.start_update(entry)
    await manager._update_tasks[entry.id]


def test_an_update_is_there_when_the_catalog_pins_another_revision_than_the_one_on_disk(
    paths, sessions
):
    manager, entry = _installed_tiny(paths, sessions)
    assert manager.update_available(entry) is False  # the revision on disk is the pinned one
    assert manager.update_available(_newer()) is True

    never_installed = _newer().model_copy(update={"id": "another-model"})
    assert manager.update_available(never_installed) is False


def test_no_update_is_offered_for_a_model_folder_that_is_a_link(paths, sessions):
    manager, _ = _installed_tiny(paths, sessions)
    elsewhere = paths.models.parent / "elsewhere"
    (paths.models / "tiny").rename(elsewhere)
    (paths.models / "tiny").symlink_to(elsewhere, target_is_directory=True)
    # Usable through the link, and not Lectern's to replace: as with Remove.
    assert manager.is_downloaded(_newer()) is True
    assert manager.update_available(_newer()) is False


def test_no_update_is_offered_for_a_model_whose_revision_is_not_known(paths, sessions):
    manager, entry = _installed_tiny(paths, sessions)
    manager._save("tiny", revision=None)  # as for files that were copied in by hand
    # It may be the newer one already: Lectern can't tell, and doesn't guess.
    assert manager.update_available(_newer()) is False


def test_an_update_fetches_what_changed_and_keeps_what_did_not(paths, sessions):
    manager, hub = _updating(paths, sessions)
    folder = paths.models / "tiny"
    projector_before = (folder / "mmproj.gguf").stat().st_ino

    asyncio.run(_updated(manager, _newer()))

    assert (folder / "tiny.gguf").read_bytes() == NEWER
    assert (folder / "mmproj.gguf").read_bytes() == MMPROJ
    # Only the changed file was downloaded; the other is the very file that was there.
    assert [r.url.path.rsplit("/", 1)[-1] for r in hub.requests] == ["tiny.gguf"]
    assert "/resolve/" + "b" * 40 + "/" in str(hub.requests[0].url)
    assert (folder / "mmproj.gguf").stat().st_ino == projector_before
    assert _row(sessions, "tiny").revision == "b" * 40
    assert manager.update_available(_newer()) is False and manager.update_state("tiny") is None
    assert sorted(p.name for p in paths.models.iterdir()) == ["tiny"]  # nothing left beside it


def test_the_old_copy_is_whole_until_the_model_is_free_and_then_swapped(paths, sessions):
    manager, _ = _updating(paths, sessions)
    folder = paths.models / "tiny"
    seen: list[tuple[str, bytes, bool, str]] = []

    async def when_free(model_id, swap):  # noqa: ANN001, ANN202
        # As the runtime: called when the new revision is ready; runs `swap` once no copy of
        # the model is loaded.
        staged = (paths.models / "tiny.update" / "tiny.gguf").read_bytes() == NEWER
        seen.append((model_id, (folder / "tiny.gguf").read_bytes(), staged, "before"))
        waiting = manager.update_state("tiny")  # all fetched: only the model is waited for
        assert (waiting.status, waiting.bytes_done) == ("waiting", waiting.bytes_total)
        await swap()
        seen.append((model_id, (folder / "tiny.gguf").read_bytes(), False, "after"))

    manager.exclusive = when_free

    asyncio.run(_updated(manager, _newer()))

    assert seen == [("tiny", MODEL, True, "before"), ("tiny", NEWER, False, "after")]


def test_while_an_update_runs_the_list_can_say_how_far_it_is(paths, sessions):
    manager, hub = _updating(paths, sessions)
    hub.gate = threading.Event()  # the transfer hangs mid-file
    entry = _newer()

    async def go():
        started = manager.start_update(entry)
        again = manager.start_update(entry)  # pressed twice: still one update
        await _until(lambda: list((paths.models / "tiny.update").glob("*.part")))
        during = manager.update_state("tiny")
        usable = manager.is_downloaded(entry) and manager.state(entry).status == "done"
        hub.gate.set()
        await manager._update_tasks["tiny"]
        return started, again, during, usable

    started, again, during, usable = asyncio.run(go())
    # First what's on disk is compared with the new pin, then what differs is fetched.
    assert (started.status, again.status, during.status) == ("checking", "checking", "updating")
    assert during.bytes_total == entry.size and during.error is None
    assert usable  # the model on disk went on being a finished download throughout
    assert len(hub.requests) == 1


def test_an_update_that_fails_leaves_the_model_as_it_was_and_can_be_tried_again(paths, sessions):
    manager, hub = _updating(paths, sessions)
    hub.cut_after["tiny.gguf"] = 30_000
    entry = _newer()

    asyncio.run(_updated(manager, entry))

    failed = manager.update_state("tiny")
    assert failed.status == "failed" and "Connection problem" in failed.error
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL
    assert _row(sessions, "tiny").revision == "a" * 40
    assert manager.update_available(entry) is True

    asyncio.run(_updated(manager, entry))  # Update again: it goes on from where it stopped

    assert hub.requests[1].headers["range"] == "bytes=30000-"
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == NEWER
    assert manager.update_state("tiny") is None


def test_an_update_is_refused_without_room_for_what_it_has_to_fetch(paths, sessions):
    _installed_tiny(paths, sessions)
    hub = FakeHub({"tiny.gguf": NEWER, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub, disk_free=DISK_HEADROOM + len(NEWER) - 1)

    asyncio.run(_updated(manager, _newer()))

    failed = manager.update_state("tiny")
    assert failed.status == "failed" and "Not enough disk space" in failed.error
    assert hub.requests == []  # the unchanged file doesn't count: it isn't fetched
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL


def test_a_swap_whose_second_step_fails_puts_the_model_back_at_once(paths, sessions, monkeypatch):
    manager, _ = _updating(paths, sessions)
    rename = Path.rename

    def failing(self, target):  # noqa: ANN001, ANN202
        if self.name == "tiny.update":
            raise OSError("Operation not permitted")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", failing)

    asyncio.run(_updated(manager, _newer()))

    # Not left renamed away until the next start: the model is where it was, usable.
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL
    assert not (paths.models / "tiny.old").exists()
    failed = manager.update_state("tiny")
    assert failed.status == "failed" and "Operation not permitted" in failed.error
    assert _row(sessions, "tiny").revision == "a" * 40


def test_an_update_is_done_even_if_the_old_folder_cannot_be_deleted_yet(
    paths, sessions, monkeypatch
):
    manager, _ = _updating(paths, sessions)
    rmtree = shutil.rmtree

    def busy(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if Path(path).name == "tiny.old":
            raise OSError("Resource busy")
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", busy)

    asyncio.run(_updated(manager, _newer()))

    # The new revision is in place and recorded: that's an update that worked.
    assert manager.update_state("tiny") is None
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == NEWER
    assert _row(sessions, "tiny").revision == "b" * 40
    assert (paths.models / "tiny.old").is_dir()  # left for the next start

    async def next_start():
        return manager.resume_interrupted({"tiny": _newer()})

    # And a start that can't delete it either still starts.
    assert asyncio.run(next_start()) == []
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    assert asyncio.run(next_start()) == []
    assert not (paths.models / "tiny.old").exists()


def test_there_is_nothing_to_update_for_a_model_at_the_pinned_revision(paths, sessions):
    manager, entry = _installed_tiny(paths, sessions)

    async def go():
        manager.start_update(entry)

    with pytest.raises(NothingToUpdate):
        asyncio.run(go())


def _without_projector(entry: CatalogEntry) -> CatalogEntry:
    return entry.model_copy(update={"files": entry.files.model_copy(update={"mmproj": None})})


def _older_install(paths, sessions, **hub_files):  # noqa: ANN001, ANN202
    """Tiny installed at a first revision that had no projector file. The pin has moved to
    one whose model file changed and which has a projector: of that one, a file is missing."""
    first = _without_projector(_entry())
    manager = _manager(paths, sessions, FakeHub({"tiny.gguf": MODEL}))
    asyncio.run(_finish(manager, first))
    hub = FakeHub({"tiny.gguf": NEWER, "mmproj.gguf": MMPROJ, **hub_files})
    return _manager(paths, sessions, hub), hub


def test_a_new_pin_with_a_file_the_old_one_lacked_is_an_update_not_a_download(paths, sessions):
    manager, hub = _older_install(paths, sessions)
    entry = _newer()
    # By the new pin's file list the model isn't all there, and can't be used...
    assert manager.is_downloaded(entry) is False and manager.state(entry).status == "none"
    # ...but what's on disk is a known older revision: it's an update that is offered.
    assert manager.update_available(entry) is True
    assert manager.installed(entry).revision == "a" * 40

    async def download():
        # Download, as the page's button or a tool asks for it, is that update.
        state = manager.start(entry)
        await manager._update_tasks["tiny"]
        return state

    asyncio.run(download())

    # Every file is the new pin's: the changed one was fetched again, not kept for existing.
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == NEWER
    assert (paths.models / "tiny" / "mmproj.gguf").read_bytes() == MMPROJ
    assert _row(sessions, "tiny").revision == "b" * 40
    assert manager.state(entry).status == "done" and manager.update_available(entry) is False
    assert sorted(r.url.path.rsplit("/", 1)[-1] for r in hub.requests) == [
        "mmproj.gguf",
        "tiny.gguf",
    ]


def test_whoever_waits_for_such_a_download_hears_how_it_ended(paths, sessions):
    manager, hub = _older_install(paths, sessions)
    entry = _newer()

    async def waited() -> list[str]:
        async with manager.events.listen() as queue:
            manager.start(entry)
            await manager._update_tasks["tiny"]
            return [e["status"] for e in (queue.get_nowait() for _ in range(queue.qsize()))]

    hub.cut_after["tiny.gguf"] = 30_000
    # A tool waiting for the model reads download events until one says done, failed or
    # paused. The model isn't usable, so "it failed" has to be said in those words.
    assert asyncio.run(waited())[-1] == "failed"
    assert asyncio.run(waited())[-1] == "done"  # tried again: it goes on, and ends well


def _put(folder, *, built_for: str | None = None, **files: bytes) -> None:  # noqa: ANN001
    """A folder as an update leaves it: its files and, with `built_for`, the note of the
    revision it's being built for."""
    folder.mkdir(parents=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    if built_for is not None:
        (folder / UPDATE_MARKER).write_text(built_for)


def test_a_swap_cut_short_after_the_new_revision_was_in_place_is_finished_at_start(paths, sessions):
    manager, _ = _installed_tiny(paths, sessions)
    (paths.models / "tiny").rename(paths.models / "tiny.old")
    # Cut after the second rename: the folder still carries the note of what it was built for.
    _put(paths.models / "tiny", built_for="b" * 40, **{"tiny.gguf": NEWER, "mmproj.gguf": MMPROJ})

    async def go():
        # A Lectern that pins yet another revision by now: what's recorded is what's there.
        later = _newer().model_copy(update={"revision": "c" * 40})
        return manager.resume_interrupted({"tiny": later})

    assert asyncio.run(go()) == []
    assert not (paths.models / "tiny.old").exists()
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == NEWER
    assert _row(sessions, "tiny").revision == "b" * 40
    assert sorted(p.name for p in (paths.models / "tiny").iterdir()) == ["mmproj.gguf", "tiny.gguf"]


def test_a_swap_cut_short_with_only_the_old_folder_left_to_delete_is_finished_at_start(
    paths, sessions
):
    manager, _ = _installed_tiny(paths, sessions)
    (paths.models / "tiny").rename(paths.models / "tiny.old")
    _put(paths.models / "tiny", **{"tiny.gguf": NEWER, "mmproj.gguf": MMPROJ})
    manager._save("tiny", revision="b" * 40)  # the swap had recorded it already

    async def go():
        return manager.resume_interrupted({"tiny": _newer()})

    assert asyncio.run(go()) == []
    assert not (paths.models / "tiny.old").exists()
    assert _row(sessions, "tiny").revision == "b" * 40


def test_a_swap_cut_short_before_the_new_revision_was_in_place_is_undone_and_run_again(
    paths, sessions
):
    manager, _ = _updating(paths, sessions)
    (paths.models / "tiny").rename(paths.models / "tiny.old")
    _put(
        paths.models / "tiny.update",
        built_for="b" * 40,
        **{"tiny.gguf": NEWER, "mmproj.gguf": MMPROJ},
    )

    async def go():
        resumed = manager.resume_interrupted({"tiny": _newer()})
        # The old revision is back in its place at once, usable while the update finishes.
        back = (paths.models / "tiny" / "tiny.gguf").read_bytes()
        await manager._update_tasks["tiny"]
        return resumed, back

    assert asyncio.run(go()) == (["tiny"], MODEL)
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == NEWER
    assert _row(sessions, "tiny").revision == "b" * 40
    assert sorted(p.name for p in paths.models.iterdir()) == ["tiny"]


def test_an_update_interrupted_mid_download_goes_on_at_the_next_start(paths, sessions):
    manager, hub = _updating(paths, sessions)
    _put(paths.models / "tiny.update", built_for="b" * 40, **{"tiny.gguf.part": NEWER[:40_000]})

    async def go():
        resumed = manager.resume_interrupted({"tiny": _newer()})
        await manager._update_tasks["tiny"]
        return resumed

    assert asyncio.run(go()) == ["tiny"]
    assert hub.requests[0].headers["range"] == "bytes=40000-"
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == NEWER


@pytest.mark.parametrize("built_for", ["c" * 40, None])
def test_an_update_begun_for_another_pin_is_started_over(paths, sessions, built_for):
    """An update had fetched and verified its file, for the pin of that time, and was
    interrupted before the swap. The pin has moved since (or the folder doesn't say what it
    was for). Its files are another revision's: none of them may be taken for this one's."""
    manager, hub = _updating(paths, sessions)
    other = b"GGUF" + b"the weights of some other revision" * 3000
    _put(paths.models / "tiny.update", built_for=built_for, **{"tiny.gguf": other})

    async def go():
        resumed = manager.resume_interrupted({"tiny": _newer()})
        await manager._update_tasks["tiny"]
        return resumed

    assert asyncio.run(go()) == ["tiny"]
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == NEWER
    assert _row(sessions, "tiny").revision == "b" * 40
    assert "range" not in hub.requests[0].headers  # fetched from its first byte
    assert sorted(p.name for p in (paths.models / "tiny").iterdir()) == ["mmproj.gguf", "tiny.gguf"]


def test_what_an_update_left_behind_for_a_model_already_current_is_cleared(paths, sessions):
    manager, entry = _installed_tiny(paths, sessions)
    _put(paths.models / "tiny.update", **{"tiny.gguf.part": b"x" * 10})

    async def go():
        return manager.resume_interrupted({"tiny": entry})

    assert asyncio.run(go()) == []
    assert not (paths.models / "tiny.update").exists()
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL


def test_a_models_update_folders_are_not_listed_as_strays_and_go_with_the_model(paths, sessions):
    manager, hub = _updating(paths, sessions)
    hub.gate = threading.Event()
    entry = _newer()

    async def go():
        manager.start_update(entry)
        await _until(lambda: list((paths.models / "tiny.update").glob("*.part")))
        strays = manager.unlisted({"tiny"})
        await manager.remove("tiny")  # the update is stopped, and its folder goes too
        return strays, manager.update_state("tiny")

    assert asyncio.run(go()) == ([], None)
    assert list(paths.models.iterdir()) == []


def test_shutting_down_stops_an_update_and_keeps_what_it_fetched(paths, sessions):
    manager, hub = _updating(paths, sessions)
    hub.gate = threading.Event()

    async def go():
        manager.start_update(_newer())
        await _until(lambda: list((paths.models / "tiny.update").glob("*.part")))
        await manager.stop_all()
        return manager._update_tasks["tiny"].done()

    assert asyncio.run(go()) is True
    assert (paths.models / "tiny.update").is_dir()  # the next start goes on from it
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL


@pytest.fixture
def api(paths, sessions, monkeypatch):
    from lectern.catalog import catalog as catalog_module

    entry = _entry()
    real = catalog_module.load()
    restricted = real.get("chandra-ocr-2")
    mlx_build = entry.model_copy(update={"id": "tiny-mlx", "format": "mlx", "replaces": "tiny"})
    fake = real.model_copy(update={"models": [entry, restricted, mlx_build]})
    monkeypatch.setattr(catalog_module, "load", lambda: fake)
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    hub.gate = threading.Event()  # hold the transfer until the test lets it go
    manager = _manager(paths, sessions, hub)
    app = create_app(paths, downloads=manager)
    headers = {"X-Lectern-Token": app.state.token, "Origin": "http://127.0.0.1:8765"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield client, headers, hub, manager


def test_post_returns_before_the_download_finishes(api):
    client, headers, hub, manager = api

    response = client.post("/api/models/tiny/download", headers=headers)

    assert response.status_code == 202
    assert response.json()["status"] in ("queued", "downloading")
    assert not (manager.models_dir / "tiny" / "tiny.gguf").exists()
    hub.gate.set()
    for _ in range(200):
        if (
            client.get("/api/models", headers=headers).json()["models"][0]["download"]["status"]
            == "done"
        ):
            break
        threading.Event().wait(0.02)
    assert (manager.models_dir / "tiny" / "tiny.gguf").read_bytes() == MODEL


def test_update_over_http_brings_a_model_to_the_pinned_revision(api):
    client, headers, hub, manager = api
    client.post("/api/models/tiny/download", headers=headers)
    hub.gate.set()
    _until_downloaded(client, headers, "tiny")
    manager._save("tiny", revision="0" * 40)  # as if an older Lectern's pin had fetched it

    def tiny() -> dict:
        models = client.get("/api/models", headers=headers).json()["models"]
        return next(m for m in models if m["id"] == "tiny")

    assert tiny()["update_available"] is True

    started = client.post("/api/models/tiny/update", headers=headers)

    assert started.status_code == 202
    for _ in range(200):
        if not tiny()["update_available"] and tiny()["update"] is None:
            break
        threading.Event().wait(0.02)
    # Through the app: the downloads' swap went by way of the runtime's gate.
    assert tiny()["installed"]["revision"] == "a" * 40
    assert (manager.models_dir / "tiny" / "tiny.gguf").read_bytes() == MODEL
    assert sorted(p.name for p in manager.models_dir.iterdir()) == ["tiny"]


def test_unknown_model_is_404(api):
    client, headers, _, _ = api
    assert client.post("/api/models/nope/download", headers=headers).status_code == 404


def test_restricted_licence_needs_explicit_acceptance(api):
    client, headers, hub, _ = api

    refused = client.post("/api/models/chandra-ocr-2/download", headers=headers)

    assert refused.status_code == 409
    assert "licence" in refused.json()["detail"]
    assert hub.requests == []


def _until_downloaded(client, headers, model_id: str) -> None:
    """Let a started download finish, so no test ends with one still running."""
    for _ in range(200):
        models = client.get("/api/models", headers=headers).json()["models"]
        if next(m for m in models if m["id"] == model_id)["download"]["status"] == "done":
            return
        threading.Event().wait(0.02)
    raise AssertionError(f"{model_id} never finished downloading")


def test_an_mlx_build_is_not_downloaded_where_mlx_cannot_run(api, monkeypatch):
    client, headers, hub, manager = api
    monkeypatch.setattr("lectern.runners.mlx.mlx_supported", lambda: False)

    refused = client.post("/api/models/tiny-mlx/download", headers=headers)

    assert refused.status_code == 409
    assert "Apple Silicon" in refused.json()["detail"]
    assert hub.requests == []
    # The model it stands in for is still there to download.
    assert client.post("/api/models/tiny/download", headers=headers).status_code == 202
    hub.gate.set()
    _until_downloaded(client, headers, "tiny")
    assert not (manager.models_dir / "tiny-mlx").exists()


def test_an_mlx_build_downloads_on_an_apple_silicon_mac(api, monkeypatch):
    client, headers, hub, manager = api
    monkeypatch.setattr("lectern.runners.mlx.mlx_supported", lambda: True)

    assert client.post("/api/models/tiny-mlx/download", headers=headers).status_code == 202
    hub.gate.set()
    _until_downloaded(client, headers, "tiny-mlx")
    assert (manager.models_dir / "tiny-mlx" / "tiny.gguf").read_bytes() == MODEL


def test_resume_hashes_the_partial_file_off_the_event_loop(paths, sessions, monkeypatch):
    from lectern.downloads import fetch as fetch_module

    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    hub.cut_after["tiny.gguf"] = 40_000
    manager = _manager(paths, sessions, hub)
    entry = _entry()
    asyncio.run(_finish(manager, entry))
    offloaded = []
    real_to_thread = asyncio.to_thread

    async def spy(func, *args):
        offloaded.append(func.__name__)
        return await real_to_thread(func, *args)

    monkeypatch.setattr(fetch_module.asyncio, "to_thread", spy)
    asyncio.run(_finish(manager, entry))

    assert offloaded == ["_hash_file"]
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL


def test_events_endpoint_streams_download_events(
    paths, sessions, monkeypatch, free_port, live_server
):
    from lectern.catalog import catalog as catalog_module

    entry = _entry()
    real = catalog_module.load()
    monkeypatch.setattr(catalog_module, "load", lambda: real.model_copy(update={"models": [entry]}))
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub, disk_free=0)  # start() fails at once, and says so
    port = free_port
    app = create_app(paths, port=port, downloads=manager)

    with live_server(app, port) as base, httpx2.Client(base_url=base, timeout=10) as client:
        headers = {"X-Lectern-Token": app.state.token, "Origin": base}
        with client.stream("GET", "/api/models/events", headers=headers) as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            lines = stream.iter_lines()
            assert next(lines) == ": connected"
            client.post("/api/models/tiny/download", headers=headers)
            data = next(line for line in lines if line.startswith("data: "))

    event = json.loads(data.removeprefix("data: "))
    assert (event["type"], event["model_id"], event["status"]) == ("download", "tiny", "failed")
    assert "disk space" in event["error"]


def test_events_endpoint_needs_the_token(paths):
    app = create_app(paths)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    assert client.get("/api/models/events").status_code == 401


def test_a_deleted_model_no_longer_counts_as_downloaded(paths, sessions):
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub)
    entry = _entry()
    asyncio.run(_finish(manager, entry))
    assert manager.state(entry).status == "done"

    (paths.models / "tiny" / "tiny.gguf").unlink()

    state = manager.state(entry)
    assert (state.status, state.bytes_done) == ("none", len(MMPROJ))
    asyncio.run(_finish(manager, entry))
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL
