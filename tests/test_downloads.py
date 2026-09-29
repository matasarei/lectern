from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import socket
import threading

import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient

from lectern.app import create_app
from lectern.catalog.catalog import CatalogEntry
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.downloads.manager import DISK_HEADROOM, DownloadManager

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


@pytest.fixture
def api(paths, sessions, monkeypatch):
    from lectern.catalog import catalog as catalog_module

    entry = _entry()
    real = catalog_module.load()
    restricted = real.get("chandra-ocr-2")
    fake = real.model_copy(update={"models": [entry, restricted]})
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


def test_unknown_model_is_404(api):
    client, headers, _, _ = api
    assert client.post("/api/models/nope/download", headers=headers).status_code == 404


def test_restricted_licence_needs_explicit_acceptance(api):
    client, headers, hub, _ = api

    refused = client.post("/api/models/chandra-ocr-2/download", headers=headers)

    assert refused.status_code == 409
    assert "licence" in refused.json()["detail"]
    assert hub.requests == []


def test_resume_hashes_the_partial_file_off_the_event_loop(paths, sessions, monkeypatch):
    from lectern.downloads import manager as manager_module

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

    monkeypatch.setattr(manager_module.asyncio, "to_thread", spy)
    asyncio.run(_finish(manager, entry))

    assert offloaded == ["_hash_file"]
    assert (paths.models / "tiny" / "tiny.gguf").read_bytes() == MODEL


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _running(app, port):
    """A real uvicorn server in a thread. TestClient can't read an endless SSE stream: it
    waits for the whole response body before returning."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_events_endpoint_streams_download_events(paths, sessions, monkeypatch):
    from lectern.catalog import catalog as catalog_module

    entry = _entry()
    real = catalog_module.load()
    monkeypatch.setattr(catalog_module, "load", lambda: real.model_copy(update={"models": [entry]}))
    hub = FakeHub({"tiny.gguf": MODEL, "mmproj.gguf": MMPROJ})
    manager = _manager(paths, sessions, hub, disk_free=0)  # start() fails at once, and says so
    port = _free_port()
    app = create_app(paths, port=port, downloads=manager)

    with _running(app, port) as base, httpx2.Client(base_url=base, timeout=10) as client:
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
