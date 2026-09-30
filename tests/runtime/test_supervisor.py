from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from lectern.hardware.tiers import Tier
from lectern.runners.base import MeasuredMemory, RunnerSpec
from lectern.runtime.planner import ModelShape
from lectern.runtime.supervisor import ModelChoice, ModelRuntime, NotAvailable, Priority

GIB = 1024**3
SHAPE = ModelShape(layers=8, kv_layers=8, kv_heads=1, key_length=64, value_length=64)


class FakeHandle:
    _pids = iter(range(1000, 100000))

    def __init__(self, spec: RunnerSpec, measured: int | None) -> None:
        self.model_id = spec.model_id
        self.spec = spec
        self.base_url = "http://127.0.0.1:1"
        self.pid = next(self._pids)
        self.alive = True
        self._measured = measured

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=self.base_url,
            transport=httpx2.MockTransport(
                lambda r: httpx2.Response(200, json={"model": self.model_id})
            ),
        )

    def is_running(self) -> bool:
        return self.alive

    def measured_memory(self) -> MeasuredMemory:
        return MeasuredMemory(device_bytes=self._measured)

    async def stop(self) -> None:
        self.alive = False


class FakeRunner:
    def __init__(self, measured: int | None = None) -> None:
        self.started: list[str] = []
        self.handles: list[FakeHandle] = []
        self.measured = measured

    async def start(self, spec: RunnerSpec) -> FakeHandle:
        await asyncio.sleep(0)  # a real start yields to the loop
        self.started.append(spec.model_id)
        handle = FakeHandle(spec, self.measured)
        self.handles.append(handle)
        return handle


def _runtime(budget_gib: float, sizes_gib: dict[str, float], runner: FakeRunner | None = None):
    runner = runner or FakeRunner()

    def resolve(role: str) -> ModelChoice:
        return ModelChoice(
            role=role,
            model_id=f"{role}-model",
            model_file=Path(f"/m/{role}.gguf"),
            weights=int(sizes_gib[role] * GIB),
            shape=SHAPE,
            tier_ctx=4096,
        )

    runtime = ModelRuntime(
        budget=int(budget_gib * GIB),
        unified=False,
        tier=Tier.RECOMMENDED,
        resolve=resolve,
        runner_for=lambda choice: runner,
    )
    return runtime, runner


async def _until(condition, *, steps: int = 1000) -> None:
    """Let other tasks run until `condition()` holds. Counts loop turns, not seconds."""
    for _ in range(steps):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def test_first_lease_loads_and_later_leases_reuse():
    runtime, runner = _runtime(8, {"main": 5})

    async def go():
        async with runtime.lease("main") as lease:
            assert (await lease.client.get("/v1/models")).json() == {"model": "main-model"}
        async with runtime.lease("main"):
            pass

    asyncio.run(go())
    assert runner.started == ["main-model"]
    assert runtime.swaps == 0


def test_models_that_fit_together_both_stay_loaded():
    runtime, runner = _runtime(8, {"ocr": 1, "main": 5})

    async def go():
        async with runtime.lease("ocr"):
            pass
        async with runtime.lease("main"):
            pass
        return runtime.state()

    state = asyncio.run(go())
    assert {r["role"] for r in state["residents"]} == {"ocr", "main"}
    assert runtime.swaps == 0


def test_models_that_dont_fit_swap_once():
    runtime, runner = _runtime(6, {"ocr": 1.5, "main": 5})

    async def go():
        for _ in range(12):  # phase 1: every page through the OCR model
            async with runtime.lease("ocr"):
                pass
        for _ in range(12):  # phase 2: every page through the main model
            async with runtime.lease("main"):
                pass

    asyncio.run(go())
    assert runner.started == ["ocr-model", "main-model"]
    assert runtime.swaps == 1
    assert [r["role"] for r in runtime.state()["residents"]] == ["main"]


def test_a_model_in_use_is_never_unloaded():
    runtime, runner = _runtime(6, {"ocr": 1.5, "main": 5})
    order = []

    async def go():
        release = asyncio.Event()

        async def ocr_page():
            async with runtime.lease("ocr"):
                order.append("ocr in use")
                await release.wait()
                order.append("ocr done")

        main_asked = asyncio.Event()

        async def main_request():
            await _until(lambda: order == ["ocr in use"])
            main_asked.set()
            async with runtime.lease("main"):
                order.append("main in use")

        job = asyncio.create_task(ocr_page())
        chat = asyncio.create_task(main_request())
        await main_asked.wait()
        await _until(lambda: runtime.state()["waiting"] == 1)  # main waits for the OCR page
        release.set()
        await asyncio.gather(job, chat)

    asyncio.run(go())
    assert order == ["ocr in use", "ocr done", "main in use"]
    assert runtime.swaps == 1


def test_a_chat_arriving_mid_page_goes_before_the_jobs_next_page():
    runtime, runner = _runtime(6, {"ocr": 1.5, "main": 5})
    served = []

    async def go():
        page_one_done = asyncio.Event()

        async def job():
            async with runtime.lease("ocr", Priority.JOB):
                served.append("page 1")
                await page_one_done.wait()
            # The next page is requested right after the first is released...
            async with runtime.lease("ocr", Priority.JOB):
                served.append("page 2")

        chat_asked = asyncio.Event()

        async def chat():
            await _until(lambda: served == ["page 1"])  # ...but this chat arrived during page 1
            chat_asked.set()
            async with runtime.lease("main", Priority.CHAT):
                served.append("chat")

        tasks = [asyncio.create_task(job()), asyncio.create_task(chat())]
        await chat_asked.wait()
        # The job holds page 1, so the only request that can be queued now is the chat's.
        await _until(lambda: runtime.state()["waiting"] == 1)
        page_one_done.set()
        await asyncio.gather(*tasks)

    asyncio.run(go())
    assert served == ["page 1", "chat", "page 2"]
    assert runtime.swaps == 2  # ocr out for the chat, main out for page 2


def test_a_loaded_model_serves_several_leases_at_once():
    runtime, runner = _runtime(6, {"ocr": 1.5})

    async def go():
        async with runtime.lease("ocr"), runtime.lease("ocr"):
            return runtime.state()["residents"][0]["leases"]

    assert asyncio.run(go()) == 2
    assert runner.started == ["ocr-model"]


def test_a_crashed_runner_shows_as_crashed_and_restarts_on_next_lease():
    runtime, runner = _runtime(8, {"main": 5})

    async def go():
        async with runtime.lease("main"):
            pass
        runner.handles[0].alive = False  # killed by hand
        crashed = runtime.state()["residents"][0]["status"]
        async with runtime.lease("main"):
            pass
        return crashed

    assert asyncio.run(go()) == "crashed"
    assert runner.started == ["main-model", "main-model"]
    assert runtime.state()["residents"][0]["status"] == "loaded"


def test_measured_memory_replaces_the_estimate():
    runtime, _ = _runtime(8, {"main": 5}, FakeRunner(measured=int(4.2 * GIB)))

    async def go():
        async with runtime.lease("main"):
            pass

    asyncio.run(go())
    assert runtime.state()["used"] == int(4.2 * GIB)


def test_chat_that_cannot_run_fast_enough_is_refused():
    runtime, runner = _runtime(1, {"main": 20})

    async def go():
        async with runtime.lease("main", Priority.CHAT):
            pass

    with pytest.raises(NotAvailable, match="tokens/s"):
        asyncio.run(go())
    assert runner.started == []
    assert runtime.state()["waiting"] == 0


def test_a_failed_start_leaves_the_queue_clean():
    runtime, runner = _runtime(8, {"main": 5})

    async def boom(spec):
        raise RuntimeError("no binary")

    runner.start = boom  # type: ignore[method-assign]

    async def go():
        with pytest.raises(RuntimeError):
            async with runtime.lease("main"):
                pass
        return runtime.state()

    state = asyncio.run(go())
    assert (state["waiting"], state["residents"]) == (0, [])


def test_events_describe_loads_swaps_and_unloads():
    runtime, _ = _runtime(6, {"ocr": 1.5, "main": 5})

    async def go():
        async with runtime.events.listen() as queue:
            async with runtime.lease("ocr"):
                pass
            async with runtime.lease("main"):
                pass
            await runtime.stop_all()
            return [queue.get_nowait() for _ in range(queue.qsize())]

    events = [(e["event"], e.get("role")) for e in asyncio.run(go())]
    assert ("loaded", "ocr") in events and ("loaded", "main") in events
    assert events.index(("unloaded", "ocr")) < events.index(("loaded", "main"))
    assert events[-1] == ("unloaded", "main")


def test_the_runtime_says_how_long_the_chat_has_been_idle(monkeypatch):
    from lectern.runtime import supervisor

    runtime, _ = _runtime(8, {"main": 5, "ocr": 2})
    clock = {"now": 100.0}
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock["now"])

    async def go():  # noqa: ANN202
        assert runtime.chat_idle_seconds() == float("inf")  # no chat yet
        async with runtime.lease("ocr", Priority.JOB):
            assert runtime.chat_idle_seconds() == float("inf")  # a job isn't the chat
        async with runtime.lease("main", Priority.CHAT):
            clock["now"] = 130.0
            assert runtime.chat_idle_seconds() == 0.0  # replying now
        clock["now"] = 190.0
        return runtime.chat_idle_seconds()

    assert asyncio.run(go()) == 60.0  # ended at 130


def test_the_runtime_says_when_loading_a_model_would_unload_another():
    small, _ = _runtime(6, {"main": 5, "ocr": 2})  # main and ocr don't fit together
    large, _ = _runtime(16, {"main": 5, "ocr": 2})

    async def go(runtime):  # noqa: ANN001, ANN202
        before = runtime.needs_room("ocr")  # nothing loaded yet
        async with runtime.lease("main", Priority.CHAT):
            pass
        return before, runtime.needs_room("ocr"), runtime.needs_room("main")

    assert asyncio.run(go(small)) == (False, True, False)  # main is loaded: no room needed
    assert asyncio.run(go(large)) == (False, False, False)  # ocr fits beside main
