from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest

from lectern.core.auth import LOCAL_USER
from lectern.hardware.tiers import Tier
from lectern.modules.ocr import store as ocr_store
from lectern.modules.ocr.jobs import RESTARTED, OcrJobs
from lectern.projects import files
from lectern.projects.tables import ArtifactRow, OcrSessionRow
from lectern.runners.base import MeasuredMemory, RunnerError
from lectern.runtime.planner import ModelShape
from lectern.runtime.supervisor import ModelChoice, ModelRuntime, NotAvailable, Priority

from ...projects.conftest import write_scan_pdf

GIB = 1024**3
SHAPE = ModelShape(layers=8, kv_layers=8, kv_heads=1, key_length=64, value_length=64)


class FakeServer:
    """Every loaded OCR model answers here. `answer(model_id, n)` gives the n-th request's
    reply text, or an int for an HTTP error; `hold` makes each request wait for a go."""

    def __init__(self, answer=None, clean=None) -> None:  # noqa: ANN001
        self.answer = answer or (lambda model, n: f"page {n} read by {model}")
        # Cleanup (M3a): a request with a JSON schema gets one paragraph back, or clean()'s.
        self.clean = clean or (lambda text: {"blocks": [{"type": "paragraph", "text": text}],
                                             "doubts": [], "fixes": []})  # fmt: skip
        self.requests: list[str] = []  # OCR readings, by model
        self.cleanups: list[str] = []  # the page text each cleanup got
        self.clean_messages: list[str] = []  # each cleanup's whole message
        self.hold: asyncio.Queue[None] | None = None

    def handle(self, model_id: str):  # noqa: ANN201
        server = self

        class Handle:
            base_url, pid = "http://127.0.0.1:1", 4242

            def client(self) -> httpx2.AsyncClient:
                async def handler(request: httpx2.Request) -> httpx2.Response:
                    await asyncio.sleep(0)
                    body = json.loads(request.content)
                    if "response_format" in body:  # a cleanup
                        page = body["messages"][1]["content"].split("<page>\n", 1)[1]
                        page = page.rsplit("\n</page>", 1)[0]
                        server.cleanups.append(page)
                        server.clean_messages.append(body["messages"][1]["content"])
                        reply = server.clean(page)
                        content = reply if isinstance(reply, str) else json.dumps(reply)
                        choice = {"message": {"content": content}, "finish_reason": "stop"}
                        return httpx2.Response(200, json={"choices": [choice]})
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
    # Each model loaded once: ocr to read every page, then main to clean them (M3a).
    assert runner.started == ["ocr-model", "main-model"]
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


def test_after_a_chat_takes_the_gpu_ocr_waits_for_the_chat_to_be_idle(store, tmp_path):
    server = FakeServer()
    server.hold = asyncio.Queue()
    runtime, runner = _runtime(server, budget_gib=6)  # main (5) and ocr (2) don't fit together
    runtime.chat_idle_resume = 0.3
    project, sid = _session(store, tmp_path, pages=2)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        while not server.hold._getters:  # page 1 is being read
            await asyncio.sleep(0)
        chat = asyncio.create_task(chat_calls())
        await asyncio.sleep(0)
        server.hold.put_nowait(None)  # page 1 ends; the chat goes before page 2
        server.hold.put_nowait(None)  # the chat's first call
        server.hold.put_nowait(None)  # its second, which mustn't find OCR loaded in between
        await chat
        server.hold.put_nowait(None)  # page 2, once the chat has been idle
        await jobs.idle()

    async def chat_calls() -> None:
        # A reply of two model calls, in a turn as the TurnManager runs it; the person wrote
        # after the job started, so page 2 also waits out the idle window after the reply.
        await runtime.turn_started(message=True)
        try:
            for _ in range(2):
                async with runtime.lease("main", Priority.CHAT) as lease:
                    await lease.client.post("/v1/chat/completions", json={})
                await asyncio.sleep(0.02)
        finally:
            await runtime.turn_ended()

    asyncio.run(go())

    # One swap each way for the chat, then main again to clean the pages (M3a).
    assert runner.started == ["ocr-model", "main-model", "ocr-model", "main-model"]
    assert any(e["type"] == "waiting" for e in _events(jobs, project, sid))
    assert _status(store, project, sid).status == "done"


def test_a_model_still_loaded_after_a_chat_is_used_at_once(store, tmp_path):
    server = FakeServer()
    runtime, _ = _runtime(server, budget_gib=16)  # both fit: no swap, no wait
    runtime.chat_idle_resume = 60.0
    project, sid = _session(store, tmp_path, pages=2)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        async with runtime.lease("main", Priority.CHAT):
            pass  # a chat just ended
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())
    assert _status(store, project, sid).status == "done"
    assert not any(e["type"] == "waiting" for e in _events(jobs, project, sid))


def test_pages_are_read_the_way_the_model_chosen_for_the_role_is_asked(
    store, tmp_path, monkeypatch
):
    """Each OCR model has its own prompt and page size in the catalog: the ones used are the
    chosen model's, not the recommended model's."""
    from lectern.catalog import catalog
    from lectern.modules.ocr import jobs as jobs_module

    real = catalog.load()
    paddle = real.get("paddleocr-vl-1.6")
    other = paddle.model_copy(
        update={
            "id": "other-ocr",
            "defaults": {},
            "ocr": paddle.ocr.model_copy(update={"prompt": "Read it my way.", "max_side": 999}),
        }
    )
    fake = real.model_copy(update={"models": [*real.models, other]})
    monkeypatch.setattr(catalog, "load", lambda: fake)
    asked: list[tuple[str, int]] = []
    recognise = jobs_module.recognise

    async def recording(client, *, image, prompt, max_side):  # noqa: ANN001, ANN202
        asked.append((prompt, max_side))
        return await recognise(client, image=image, prompt=prompt, max_side=max_side)

    monkeypatch.setattr(jobs_module, "recognise", recording)
    server = FakeServer()
    runtime, _ = _runtime(server)
    runtime.chosen_models = lambda: {"ocr": "other-ocr"}
    project, sid = _session(store, tmp_path, pages=1)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())
    assert asked == [("Read it my way.", 999)]


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


def test_reading_one_page_of_a_paused_session_leaves_it_paused(store, tmp_path):
    server = FakeServer()
    server.hold = asyncio.Queue()
    runtime, _ = _runtime(server)
    project, sid = _session(store, tmp_path)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        while not server.hold._getters:
            await asyncio.sleep(0)
        await jobs.pause(project, sid)
        server.hold.put_nowait(None)  # page 1 finishes, then the job pauses
        await jobs.idle()
        server.hold = None
        await jobs.rerun(project, sid, 1, lease_key="ocr.hand@qwen3-vl-8b")
        await jobs.idle()

    asyncio.run(go())

    paused = _status(store, project, sid)
    assert (paused.status, paused.paused_reason) == ("paused", "Paused")
    pages = ocr_store.pages(store.db(project), sid)
    assert [(p.status, p.model_id) for p in pages] == [
        ("done", "qwen3-vl-8b"), ("pending", None), ("pending", None)
    ]  # fmt: skip
    assert paused.document_version == 0  # no Document of unread pages: Resume assembles it


def test_a_session_is_never_shown_queued_after_its_job_started(store, tmp_path, monkeypatch):
    import time

    server = FakeServer()
    runtime, _ = _runtime(server)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, sid1 = _session(store, tmp_path / "a", pages=1)
    second, sid2 = _session(store, tmp_path / "b", pages=1)
    jobs = OcrJobs(runtime, store)
    real = jobs._set_status

    def slow_queued(project, session_id, status, **kw):  # noqa: ANN001, ANN202
        if status == "queued" and session_id == sid2:
            time.sleep(0.2)  # the write lands late, as a busy disk might make it
        real(project, session_id, status, **kw)

    monkeypatch.setattr(jobs, "_set_status", slow_queued)

    async def go():  # noqa: ANN202
        await jobs.start(first, sid1)
        await jobs.start(second, sid2)  # the first job finishes meanwhile
        await jobs.idle()

    asyncio.run(go())

    statuses = [e["status"] for e in _events(jobs, second, sid2) if e["type"] == "session"]
    assert statuses == ["queued", "running", "done"]


def test_pause_takes_effect_while_the_job_waits_for_the_chat(store, tmp_path):
    server = FakeServer()
    runtime, runner = _runtime(server, budget_gib=6)  # main (5) and ocr (2) don't fit together
    runtime.chat_idle_resume = 30.0  # the job would wait half a minute for the chat
    project, sid = _session(store, tmp_path, pages=2)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await runtime.turn_started(message=True)  # a reply is being written
        async with runtime.lease("main", Priority.CHAT):
            pass
        await jobs.start(project, sid)
        while not any(e["type"] == "waiting" for e in _events(jobs, project, sid)):
            await asyncio.sleep(0.01)
        await jobs.pause(project, sid)
        await asyncio.wait_for(jobs.idle(), 5)  # not after the reply, nor a hold
        await runtime.turn_ended()

    asyncio.run(go())

    paused = _status(store, project, sid)
    assert (paused.status, paused.paused_reason) == ("paused", "Paused")
    assert [p.status for p in ocr_store.pages(store.db(project), sid)] == ["pending", "pending"]
    assert server.requests == [] and runner.started == ["main-model"]  # no page read, no swap


# --- cleanup and the Document (M3a) ---------------------------------------------------------


def _run_job(store, tmp_path, server, *, pages=2, budget_gib=16, after=None):  # noqa: ANN001, ANN202
    runtime, runner = _runtime(server, budget_gib=budget_gib)
    project, sid = _session(store, tmp_path, pages=pages)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()
        if after:
            await after(jobs, project, sid)
            await jobs.idle()

    asyncio.run(go())
    return project, sid, runtime, runner, jobs


def test_after_reading_the_pages_are_cleaned_and_a_document_published(store, tmp_path):
    def clean(text: str) -> dict:
        return {
            "blocks": [{"type": "heading", "level": 2, "text": "Договір"},
                       {"type": "paragraph", "text": f"{text} до 1? числа"}],
            "doubts": [{"block": 1, "raw": "1?", "candidates": ["10", "15"], "reason": "smudged"}],
            "fixes": [],
        }  # fmt: skip

    project, sid, runtime, _, jobs = _run_job(store, tmp_path, FakeServer(clean=clean))

    folder = project.root / "lease-scan"
    doc = json.loads((folder / "document.json").read_text())
    assert doc["schema"] == "lectern.document/1" and doc["version"] == 1
    assert [b["id"] for b in doc["blocks"]] == ["b1.1", "b1.2", "b2.1", "b2.2"]
    assert doc["blocks"][1]["doubts"][0]["id"] == "d1.1"
    md = (folder / "document.md").read_text()
    assert "{#b1.2 p=1} page 1 read by ocr-model до ⟦1? | open | 10 · 15⟧ числа" in md
    ocr = _status(store, project, sid)
    assert (ocr.status, ocr.document_version) == ("done", 1)
    with store.db(project)() as session:
        row = session.get(ArtifactRow, ocr.document_artifact_id)
    assert (row.path, row.from_kind, row.from_id, row.text_status) == (
        "lease-scan/document.md", "session", sid, "indexed"
    )  # fmt: skip
    kinds = [e.get("phase") for e in _events(jobs, project, sid) if e["type"] == "phase"]
    assert kinds == ["rasterise", "recognise", "clean", "assemble"]
    assert any(e["type"] == "document" and e["version"] == 1 for e in _events(jobs, project, sid))


def test_on_a_small_gpu_the_document_costs_one_swap(store, tmp_path):
    _, _, runtime, runner, _ = _run_job(store, tmp_path, FakeServer(), pages=3, budget_gib=6)
    assert runner.started == ["ocr-model", "main-model"] and runtime.swaps == 1


def test_unsure_ocr_words_are_marked_for_cleanup_and_become_doubts_if_kept(store, tmp_path):
    server = FakeServer()

    def answer(model: str, n: int) -> str:
        return "Сума 250,00 грн"

    server.answer = answer
    runtime, _ = _runtime(server)
    project, sid = _session(store, tmp_path, pages=1)
    folder = project.root / "lease-scan" / "pages"
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        real = ocr_store.save_reading

        def with_tokens(root, db, ocr, page_id, *, model_id, reading):  # noqa: ANN001, ANN202
            tokens = [("Сума", -0.1), (" 250", -4.5), (",00", -0.1), (" грн", -0.1)]
            return real(root, db, ocr, page_id, model_id=model_id,
                        reading=replace(reading, tokens=tokens))  # fmt: skip

        ocr_store.save_reading = with_tokens
        try:
            await jobs.start(project, sid)
            await jobs.idle()
        finally:
            ocr_store.save_reading = real

    asyncio.run(go())

    assert server.cleanups == ["Сума ⟨250,00?⟩ грн"]  # marked for cleanup to weigh
    doc = json.loads((folder.parent / "document.json").read_text())
    (block,) = doc["blocks"]
    # The fake cleanup kept the mark, as a careless model might: assembly removes it, and the
    # word, still as the OCR model read it, becomes a doubt.
    assert block["text"] == "Сума {d1.1} грн"
    assert [(d["id"], d["raw"], d["source"]) for d in block["doubts"]] == [
        ("d1.1", "250,00", "ocr")
    ]


def test_reading_a_page_again_cleans_only_it_and_keeps_the_other_ids(store, tmp_path):
    async def reread(jobs, project, sid):  # noqa: ANN001, ANN202
        await jobs.rerun(project, sid, 2, lease_key="ocr.hand@qwen3-vl-8b")

    server = FakeServer(lambda model, n: f"Page {n} read by {model}.")  # whole sentences
    project, sid, *_ = _run_job(store, tmp_path, server, after=reread)

    assert len(server.cleanups) == 3  # pages 1 and 2, then page 2 again
    doc = json.loads((project.root / "lease-scan" / "document.json").read_text())
    assert doc["version"] == 2 and [b["id"] for b in doc["blocks"]] == ["b1.1", "b2.1"]
    assert "read by qwen3-vl-8b" in doc["blocks"][1]["text"]
    assert _status(store, project, sid).document_version == 2


def test_a_page_whose_cleanup_fails_is_a_placeholder_in_the_document(store, tmp_path):
    def clean(text: str) -> dict | str:
        if "page 2" in text:
            return "{not json"
        return {"blocks": [{"type": "paragraph", "text": text}], "doubts": [], "fixes": []}

    project, sid, *_ = _run_job(store, tmp_path, FakeServer(clean=clean))

    first, second = ocr_store.pages(store.db(project), sid)
    assert (first.clean_status, second.clean_status) == ("done", "failed")
    assert "wasn't valid, twice" in second.clean_error
    md = (project.root / "lease-scan" / "document.md").read_text()
    assert "{#p2 p=2} [Page 2 wasn't cleaned: The chat model's cleanup wasn't valid" in md
    assert _status(store, project, sid).status == "done"


def test_pause_during_cleanup_stops_after_the_current_page(store, tmp_path):
    server = FakeServer()
    runtime, _ = _runtime(server)
    project, sid = _session(store, tmp_path, pages=3)
    jobs = OcrJobs(runtime, store)
    real = server.clean

    def clean_then_pause(text: str) -> dict:
        if not server.cleanups[:-1]:  # the first cleanup: press Pause meanwhile
            asyncio.get_running_loop().create_task(jobs.pause(project, sid))
        return real(text)

    server.clean = clean_then_pause

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())
    assert len(server.cleanups) == 1
    assert [p.clean_status for p in ocr_store.pages(store.db(project), sid)] == [
        "done", "pending", "pending"
    ]  # fmt: skip
    assert _status(store, project, sid).status == "paused"


def _with_a_doubt(text: str) -> dict:
    return {"blocks": [{"type": "paragraph", "text": f"{text} до 1? числа"}],
            "doubts": [{"block": 0, "raw": "1?", "candidates": ["15"]}], "fixes": []}  # fmt: skip


def test_a_page_cleaned_again_keeps_its_decision_and_the_cleanup_is_told_of_it(store, tmp_path):
    async def decide_then_clean_again(jobs, project, sid):  # noqa: ANN001, ANN202
        await asyncio.to_thread(ocr_store.decide, project.root, store.db(project), sid, "d1.1",
                                value="15", action="accept", user_id=LOCAL_USER.id)  # fmt: skip
        await jobs.reclean(project, sid, 1)

    server = FakeServer(clean=_with_a_doubt)
    project, sid, *_ = _run_job(store, tmp_path, server, after=decide_then_clean_again)

    ocr = _status(store, project, sid)
    doc = ocr_store.load_document(project.root, ocr)
    assert ocr.document_version == 3  # assembled, decided, cleaned again and assembled
    doubts = {d.id: (d.status, d.value) for b in doc.blocks for d in b.doubts}
    assert doubts == {"d1.1": ("resolved", "15"), "d2.1": ("open", None)}
    assert len(server.cleanups) == 3 and len(server.requests) == 2  # page 1 wasn't read again
    assert "Readings the person confirmed" not in server.clean_messages[0]
    assert "«1?» is «15»" in server.clean_messages[-1]
    pages = ocr_store.pages(store.db(project), sid)
    assert [(p.reading, p.clean_status) for p in pages] == [(1, "done"), (1, "done")]


def test_a_decision_made_while_the_job_assembles_is_in_the_last_version(
    store, tmp_path, monkeypatch
):
    import threading

    assembling, go_on = threading.Event(), threading.Event()
    real_inputs = ocr_store.assembly_inputs

    def slow_inputs(*args):  # noqa: ANN002, ANN202
        if ocr_store.load_document(args[0], args[2]) is not None:  # the second assembly
            assembling.set()
            go_on.wait(5)
        return real_inputs(*args)

    async def decide_while_assembling(jobs, project, sid):  # noqa: ANN001, ANN202
        monkeypatch.setattr(ocr_store, "assembly_inputs", slow_inputs)
        await jobs.reclean(project, sid, 2)
        await asyncio.to_thread(assembling.wait, 5)
        decision = asyncio.create_task(asyncio.to_thread(
            ocr_store.decide, project.root, store.db(project), sid, "d1.1", value="15",
            action="edit", user_id=LOCAL_USER.id))  # fmt: skip
        await asyncio.sleep(0.05)
        go_on.set()
        await decision

    server = FakeServer(clean=_with_a_doubt)
    project, sid, *_ = _run_job(store, tmp_path, server, after=decide_while_assembling)

    ocr = _status(store, project, sid)
    doc = ocr_store.load_document(project.root, ocr)
    assert ocr.document_version == 3  # neither publish wrote over the other
    assert doc.blocks[0].doubts[0].value == "15"


def test_a_finished_or_failed_session_tells_whoever_waits_on_it(store, tmp_path):
    heard: list[tuple[int, str, str]] = []

    async def on_status(project, session_id, status, notice):  # noqa: ANN001, ANN202
        heard.append((session_id, status, notice))

    server = FakeServer(clean=_with_a_doubt)
    runtime, _ = _runtime(server)
    project, sid = _session(store, tmp_path, pages=2)
    jobs = OcrJobs(runtime, store)
    jobs.on_status = on_status

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())
    # Not "finished" while questions are open: the Document isn't final yet
    assert heard == [
        (sid, "done", "OCR · lease-scan.pdf isn't finished yet: 2 pages read · 2 open questions.")
    ]

    heard.clear()
    failing, _ = _runtime(FakeServer(), fail=True)
    (tmp_path / "b").mkdir()
    project2, sid2 = _session(store, tmp_path / "b", pages=1)
    jobs2 = OcrJobs(failing, store)
    jobs2.on_status = on_status

    async def go2():  # noqa: ANN202
        await jobs2.start(project2, sid2)
        await jobs2.idle()

    asyncio.run(go2())
    ((_, status, notice),) = heard
    assert status == "failed" and notice.startswith("OCR · lease-scan.pdf stopped: ")


def test_a_page_waiting_for_the_chat_is_not_shown_as_being_read(store, tmp_path):
    """Seen on the test server: page 1 said "reading…" for two minutes while the job only
    waited for the chat. A page is read (and said to be) once the model is the job's."""
    server = FakeServer()
    runtime, _ = _runtime(server, budget_gib=6)  # main (5) and ocr (2) don't fit together
    runtime.chat_idle_resume = 30.0
    project, sid = _session(store, tmp_path, pages=1)
    jobs = OcrJobs(runtime, store)
    seen: dict = {}

    async def go():  # noqa: ANN202
        await runtime.turn_started(message=True)  # a reply is being written
        async with runtime.lease("main", Priority.CHAT):
            pass
        await jobs.start(project, sid)
        while not any(e["type"] == "waiting" for e in _events(jobs, project, sid)):
            await asyncio.sleep(0.01)
        seen["waiting"] = next(e for e in _events(jobs, project, sid) if e["type"] == "waiting")
        seen["pages"] = [p.status for p in ocr_store.pages(store.db(project), sid)]
        seen["reading"] = [e for e in _events(jobs, project, sid)
                           if e["type"] == "page" and e.get("status") == "recognising"]  # fmt: skip
        await runtime.turn_ended()  # the reply is done: the job goes
        await asyncio.wait_for(jobs.idle(), 5)

    asyncio.run(go())
    assert seen["waiting"]["resume_in"] is None  # until the reply ends, not a countdown
    assert seen["pages"] == ["pending"] and seen["reading"] == []
    assert _status(store, project, sid).status == "done"


def _plain(text: str) -> dict:
    return {"blocks": [{"type": "paragraph", "text": text}], "doubts": [], "fixes": []}


def test_a_session_is_clear_once_done_with_nothing_open_and_every_page_cleaned(store, tmp_path):
    project, sid, *_, jobs = _run_job(store, tmp_path, FakeServer(clean=_plain))
    summary = jobs.summary(project, sid)
    assert (summary["clear"], summary["open_doubts"], summary["failed_pages"]) == (True, 0, [])


def test_open_questions_or_a_page_that_wasnt_cleaned_leave_it_not_clear(store, tmp_path):
    project, sid, *_, jobs = _run_job(store, tmp_path, FakeServer(clean=_with_a_doubt))
    summary = jobs.summary(project, sid)
    assert summary["status"] == "done" and summary["open_doubts"] == 2
    assert summary["clear"] is False and summary["failed_pages"] == []

    def clean(text: str) -> dict | str:
        return "{not json" if "page 2" in text else _plain(text)

    (tmp_path / "b").mkdir()
    project, sid, *_, jobs = _run_job(store, tmp_path / "b", FakeServer(clean=clean))
    summary = jobs.summary(project, sid)
    assert (summary["status"], summary["clear"], summary["failed_pages"]) == ("done", False, [2])


def test_a_clear_session_is_told_as_finished(store, tmp_path):
    heard: list[str] = []

    async def on_status(project, session_id, status, notice):  # noqa: ANN001, ANN202
        heard.append(notice)

    runtime, _ = _runtime(FakeServer(clean=_plain))
    project, sid = _session(store, tmp_path, pages=2)
    jobs = OcrJobs(runtime, store)
    jobs.on_status = on_status

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())
    assert heard == ["OCR · lease-scan.pdf finished · 2 pages."]


def test_a_session_still_at_work_isnt_clear(store, tmp_path):
    runtime, _ = _runtime(FakeServer())
    project, sid = _session(store, tmp_path, pages=1)
    jobs = OcrJobs(runtime, store)
    summary = jobs.summary(project, sid)  # never started
    assert summary["status"] != "done" and summary["clear"] is False


def test_cleanup_leases_the_chat_model_by_the_key_for_schema_work(store, tmp_path):
    # Cleanup's answer is held to a JSON schema. Where the role's own model can't be (an MLX
    # build), the runtime's schema_key names the one that can; cleanup leases by that key.
    runtime, runner = _runtime(FakeServer())
    assert runtime.schema_key("main") == "main"  # as built: the role itself
    runtime.schema_key = lambda role: f"{role}@gguf-chat"
    project, sid = _session(store, tmp_path, pages=2)
    jobs = OcrJobs(runtime, store)

    async def go():  # noqa: ANN202
        await jobs.start(project, sid)
        await jobs.idle()

    asyncio.run(go())
    assert runner.started == ["ocr-model", "gguf-chat"]
    assert _status(store, project, sid).status == "done"
