from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from lectern.core.auth import LOCAL_USER
from lectern.hardware.tiers import Tier
from lectern.modules.ocr import jobs as ocr_jobs
from lectern.modules.ocr import store as ocr_store
from lectern.modules.ocr.jobs import RESTARTED, OcrJobs
from lectern.projects import files
from lectern.projects.tables import OcrSessionRow
from lectern.runners.base import MeasuredMemory, RunnerError
from lectern.runtime import supervisor
from lectern.runtime.planner import ModelShape
from lectern.runtime.supervisor import ModelChoice, ModelRuntime, NotAvailable, Priority

from ...projects.conftest import write_scan_pdf

GIB = 1024**3
SHAPE = ModelShape(layers=8, kv_layers=8, kv_heads=1, key_length=64, value_length=64)


class FakeServer:
    """Every loaded OCR model answers here. `answer(model_id, n)` gives the n-th request's
    reply text, or an int for an HTTP error; `hold` makes each request wait for a go."""

    def __init__(self, answer=None) -> None:  # noqa: ANN001
        self.answer = answer or (lambda model, n: f"page {n} read by {model}")
        self.requests: list[str] = []
        self.hold: asyncio.Queue[None] | None = None

    def handle(self, model_id: str):  # noqa: ANN201
        server = self

        class Handle:
            base_url, pid = "http://127.0.0.1:1", 4242

            def client(self) -> httpx2.AsyncClient:
                async def handler(request: httpx2.Request) -> httpx2.Response:
                    await asyncio.sleep(0)
                    if server.hold is not None:
                        await server.hold.get()
                    server.requests.append(model_id)
                    reply = server.answer(model_id, len(server.requests))
                    if isinstance(reply, int):
                        return httpx2.Response(reply, json={"error": {"message": "boom"}})
                    choice = {"message": {"content": reply}, "finish_reason": "stop"}
                    return httpx2.Response(200, json={"choices": [choice], "usage": {}})

                return httpx2.AsyncClient(
                    base_url=self.base_url, transport=httpx2.MockTransport(handler)
                )

            def is_running(self) -> bool:
                return True

            def measured_memory(self) -> MeasuredMemory:
                return MeasuredMemory(device_bytes=None)

            async def stop(self) -> None:
                pass

        Handle.model_id = model_id
        return Handle()


class FakeRunner:
    def __init__(self, server: FakeServer, *, fail: bool = False) -> None:
        self.server, self.fail, self.started = server, fail, []

    async def start(self, spec):  # noqa: ANN001, ANN201
        await asyncio.sleep(0)
        if self.fail:
            raise RunnerError("llama-server for it wasn't ready.\nlog tail")
        self.started.append(spec.model_id)
        return self.server.handle(spec.model_id)


def _runtime(server: FakeServer, *, budget_gib: float = 16, fail: bool = False,
             missing: tuple[str, ...] = ()):  # noqa: ANN202  # fmt: skip
    runner = FakeRunner(server, fail=fail)

    def resolve(key: str) -> ModelChoice:
        role, _, model = key.partition("@")
        if key in missing:
            raise NotAvailable(f"{key} isn't downloaded yet. Download it first.")
        size = 5 if role == "main" else 2
        return ModelChoice(role, model or f"{role}-model", Path(f"/m/{key}.gguf"),
                           size * GIB, SHAPE, 4096)  # fmt: skip

    runtime = ModelRuntime(budget=int(budget_gib * GIB), unified=False, tier=Tier.RECOMMENDED,
                           resolve=resolve, runner_for=lambda choice: runner)  # fmt: skip
    return runtime, runner


def _session(store, tmp_path: Path, pages: int = 3):  # noqa: ANN001, ANN202
    project = store.create(LOCAL_USER, "Scans")
    pdf = write_scan_pdf(tmp_path / "lease-scan.pdf", pages=pages)
    with store.db(project).begin() as session:
        artifact = files.write_bytes(project.root, session, owner_id=LOCAL_USER.id,
                                     name="lease-scan.pdf", data=pdf.read_bytes(),
                                     origin=files.Origin("import"))  # fmt: skip
    row, _ = ocr_store.create_session(
        project.root, store.db(project), owner_id=LOCAL_USER.id, artifact_id=artifact.id
    )
    return project, row.id


def _status(store, project, session_id: int) -> OcrSessionRow:  # noqa: ANN001
    with store.db(project)() as session:
        return session.get(OcrSessionRow, session_id)


def _events(jobs: OcrJobs, project, session_id: int) -> list[dict]:  # noqa: ANN001
    return jobs.channel((project.id, session_id)).events


def test_a_session_is_rasterised_then_every_page_read(store, tmp_path):
    server = FakeServer()
    runtime, runner = _runtime(server)
    project, sid = _session(store, tmp_path)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())

    assert _status(store, project, sid).status == "done"
    pages = ocr_store.pages(store.db(project), sid)
    assert [(p.status, p.model_id) for p in pages] == [("done", "ocr-model")] * 3
    folder = project.root / "lease-scan" / "pages"
    assert (folder / "p003.ocr.md").read_text() == "page 3 read by ocr-model\n"
    assert runner.started == ["ocr-model"]  # loaded once, leased per page
    kinds = [(e["type"], e.get("phase") or e.get("status")) for e in _events(jobs, project, sid)]
    assert kinds[:4] == [("session", "queued"), ("session", "running"),
                         ("phase", "rasterise"), ("phase", "recognise")]  # fmt: skip
    assert kinds[-1] == ("session", "done")


def test_a_page_that_fails_is_recorded_and_the_rest_are_read(store, tmp_path):
    loop = " ".join(f"w{i}" for i in range(15))
    server = FakeServer(lambda model, n: {2: 500, 3: f"{loop}\n{loop}"}.get(n, "ok"))
    runtime, _ = _runtime(server)
    project, sid = _session(store, tmp_path)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())

    first, second, third = ocr_store.pages(store.db(project), sid)
    assert first.status == "done" and not first.looped
    assert second.status == "failed" and "answered 500" in second.error
    assert third.status == "done" and third.looped
    assert _status(store, project, sid).status == "done"


def test_pause_stops_after_the_current_page_and_resume_goes_on(store, tmp_path):
    server = FakeServer()
    server.hold = asyncio.Queue()
    runtime, _ = _runtime(server)
    project, sid = _session(store, tmp_path)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        while not server.hold._getters:  # the first page is being read
            await asyncio.sleep(0)
        await jobs.pause(project, sid)
        server.hold.put_nowait(None)  # page 1 finishes
        await jobs.idle()
        paused = _status(store, project, sid)
        assert (paused.status, paused.paused_reason) == ("paused", "Paused")
        assert [p.status for p in ocr_store.pages(store.db(project), sid)] == [
            "done", "pending", "pending"
        ]  # fmt: skip
        server.hold = None
        await jobs.resume(project, sid)
        await jobs.idle()

    asyncio.run(go())
    assert _status(store, project, sid).status == "done"
    assert len(server.requests) == 3  # page 1 wasn't read twice


def test_after_a_restart_the_session_is_paused_and_nothing_loads(store, tmp_path):
    server = FakeServer()
    runtime, runner = _runtime(server)
    project, sid = _session(store, tmp_path)
    with store.db(project).begin() as session:
        session.get(OcrSessionRow, sid).status = "running"  # the app stopped mid-batch

    settled = OcrJobs(runtime, store).settle(project, sid)  # a fresh process reads it

    assert (settled.status, settled.paused_reason) == ("paused", RESTARTED)
    assert _status(store, project, sid).status == "paused"
    assert runner.started == []


def test_one_job_runs_at_a_time_and_the_next_waits_queued(store, tmp_path):
    server = FakeServer()
    server.hold = asyncio.Queue()
    runtime, _ = _runtime(server)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, sid1 = _session(store, tmp_path / "a", pages=1)
    second, sid2 = _session(store, tmp_path / "b", pages=1)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(first, sid1)
        await jobs.start(second, sid2)
        while not server.hold._getters:
            await asyncio.sleep(0)
        assert _status(store, first, sid1).status == "running"
        assert _status(store, second, sid2).status == "queued"
        server.hold.put_nowait(None)
        server.hold.put_nowait(None)
        await jobs.idle()

    asyncio.run(go())
    assert _status(store, first, sid1).status == _status(store, second, sid2).status == "done"


def test_after_a_chat_takes_the_gpu_ocr_waits_for_two_idle_minutes(store, tmp_path, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock["now"])
    server = FakeServer()
    server.hold = asyncio.Queue()
    runtime, runner = _runtime(server, budget_gib=6)  # main (5) and ocr (2) don't fit together
    project, sid = _session(store, tmp_path, pages=2)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds
        await asyncio.sleep(0)

    jobs = OcrJobs(runtime, store, sleep=fake_sleep)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        while not server.hold._getters:  # page 1 is being read
            await asyncio.sleep(0)
        chat = asyncio.create_task(chat_once())
        await asyncio.sleep(0)
        server.hold.put_nowait(None)  # page 1 ends; the chat goes before page 2
        server.hold.put_nowait(None)  # the chat's reply
        await chat
        server.hold.put_nowait(None)  # page 2, once OCR has waited
        await jobs.idle()

    async def chat_once() -> None:
        async with runtime.lease("main", Priority.CHAT) as lease:
            await lease.client.post("/v1/chat/completions", json={})

    asyncio.run(go())

    assert runner.started == ["ocr-model", "main-model", "ocr-model"]  # a swap each way
    assert sum(slept) >= ocr_jobs.CHAT_IDLE_RESUME  # not straight back after the reply
    assert any(e["type"] == "waiting" for e in _events(jobs, project, sid))
    assert _status(store, project, sid).status == "done"


def test_a_model_still_loaded_after_a_chat_is_used_at_once(store, tmp_path, monkeypatch):
    server = FakeServer()
    runtime, _ = _runtime(server, budget_gib=16)  # both fit: no swap, no wait
    project, sid = _session(store, tmp_path, pages=2)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(0)

    jobs = OcrJobs(runtime, store, sleep=fake_sleep)

    async def go():  # noqa: ANN202
        async with runtime.lease("main", Priority.CHAT):
            pass  # a chat just ended
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())
    assert slept == [] and _status(store, project, sid).status == "done"


def test_a_page_read_again_with_another_model_keeps_both_readings(store, tmp_path):
    server = FakeServer()
    runtime, _ = _runtime(server)
    project, sid = _session(store, tmp_path, pages=2)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()
        await jobs.rerun(project, sid, 2, lease_key="ocr.hand@chandra-ocr-2")
        await jobs.idle()

    asyncio.run(go())

    first, second = ocr_store.pages(store.db(project), sid)
    assert (first.model_id, second.model_id) == ("ocr-model", "chandra-ocr-2")
    folder = project.root / "lease-scan" / "pages"
    assert (folder / "p002.ocr-model.ocr.md").is_file()
    assert (folder / "p002.ocr.md").read_text() == "page 3 read by chandra-ocr-2\n"
    assert server.requests == ["ocr-model", "ocr-model", "chandra-ocr-2"]


@pytest.mark.parametrize(("missing", "fail"), [(("ocr",), False), ((), True)])
def test_a_model_that_cant_load_stops_the_session_with_the_reason(store, tmp_path, missing, fail):  # noqa: ANN001
    runtime, _ = _runtime(FakeServer(), missing=missing, fail=fail)
    project, sid = _session(store, tmp_path, pages=2)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())

    stopped = _status(store, project, sid)
    assert stopped.status == "failed"
    assert stopped.error in ("ocr isn't downloaded yet. Download it first.",
                             "llama-server for it wasn't ready.")  # fmt: skip
    assert [p.status for p in ocr_store.pages(store.db(project), sid)] == ["pending", "pending"]


def test_a_source_that_cant_be_paged_fails_the_session(store, tmp_path):
    runtime, _ = _runtime(FakeServer())
    project, sid = _session(store, tmp_path, pages=1)
    (project.root / "lease-scan.pdf").write_bytes(b"%PDF-1.4 broken")
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())
    stopped = _status(store, project, sid)
    assert stopped.status == "failed" and "couldn't be opened as a PDF" in stopped.error
