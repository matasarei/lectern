from __future__ import annotations

import asyncio
import contextlib
import dataclasses
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


def _resolving_to(runtime: ModelRuntime, model_id: str) -> None:
    """From now on every role asks for `model_id`: a setting changed, or a download finished."""
    before = runtime.resolve
    runtime.resolve = lambda role: dataclasses.replace(before(role), model_id=model_id)


def test_an_idle_model_gives_way_when_its_role_asks_for_another():
    runtime, runner = _runtime(8, {"main": 5})

    async def go():
        async with runtime.events.listen() as queue:
            async with runtime.lease("main"):
                pass
            _resolving_to(runtime, "main-mlx")
            async with runtime.lease("main") as lease:
                served = (await lease.client.get("/v1/models")).json()
            async with runtime.lease("main"):
                pass  # and the new one is reused, not loaded again
            return served, [queue.get_nowait() for _ in range(queue.qsize())]

    served, events = asyncio.run(go())
    assert served == {"model": "main-mlx"}
    assert runner.started == ["main-model", "main-mlx"]
    assert not runner.handles[0].alive  # the old one was stopped, not left behind
    assert [(r["role"], r["model_id"]) for r in runtime.state()["residents"]] == [
        ("main", "main-mlx")
    ]
    unloaded = [e for e in events if e["event"] == "unloaded"]
    assert [(e["model_id"], e["reason"]) for e in unloaded] == [("main-model", "changed")]
    assert runtime.swaps == 0  # a change of model isn't a swap for room


def test_a_model_in_use_stays_until_it_is_idle_even_if_its_role_asks_for_another():
    runtime, runner = _runtime(8, {"main": 5})

    async def go():
        async with runtime.lease("main"):
            _resolving_to(runtime, "main-mlx")
            async with runtime.lease("main") as second:  # a reply is being written on it
                during = (await second.client.get("/v1/models")).json()
            assert runner.handles[0].alive
        async with runtime.lease("main") as lease:
            return during, (await lease.client.get("/v1/models")).json()

    during, after = asyncio.run(go())
    assert during == {"model": "main-model"} and after == {"model": "main-mlx"}
    assert runner.started == ["main-model", "main-mlx"]


def test_a_reply_being_written_keeps_its_model_between_its_model_calls():
    """A turn leases the model once per model call, so between a tool call and the next one
    nothing is leased. The model still mustn't change under the reply: it changes for the
    next turn."""
    runtime, runner = _runtime(8, {"main": 5})

    async def served(priority: Priority = Priority.CHAT) -> str:
        async with runtime.lease("main", priority) as lease:
            return (await lease.client.get("/v1/models")).json()["model"]

    async def go():
        await runtime.turn_started(message=True)
        first = await served()
        _resolving_to(runtime, "main-mlx")  # its MLX build finished downloading, say
        second = await served()  # the same reply, after a tool call
        by_a_job = await served(Priority.JOB)  # OCR cleanup, on the same model meanwhile
        await runtime.turn_ended()
        await runtime.turn_started(message=True)
        next_turn = await served()
        await runtime.turn_ended()
        return first, second, by_a_job, next_turn

    assert asyncio.run(go()) == ("main-model", "main-model", "main-model", "main-mlx")
    assert runner.started == ["main-model", "main-mlx"]


def test_a_change_of_settings_unloads_what_no_reply_is_being_written_with():
    runtime, runner = _runtime(8, {"ocr": 1, "main": 5})

    async def go():
        async with runtime.lease("ocr"):
            pass
        await runtime.turn_started(message=True)
        async with runtime.lease("main", Priority.CHAT):
            pass
        mid_reply = await runtime.unload_between_replies()
        left = [r["role"] for r in runtime.state()["residents"]]
        await runtime.turn_ended()
        return mid_reply, left, await runtime.unload_between_replies()

    assert asyncio.run(go()) == (["ocr"], ["main"], ["main"])
    assert runner.started == ["ocr-model", "main-model"]


def test_a_change_to_one_role_unloads_that_roles_models_only():
    sizes = {"ocr": 1, "main": 2, "main@other": 2, "code": 1}
    runtime, _ = _runtime(16, sizes)

    async def go():
        for key in sizes:
            async with runtime.lease(key):
                pass
        unloaded = await runtime.unload_between_replies("main")
        return unloaded, [r["role"] for r in runtime.state()["residents"]]

    # The role's own model, and a copy of one loaded for it by name.
    assert asyncio.run(go()) == (["main", "main@other"], ["ocr", "code"])


def test_a_loaded_model_stays_when_what_its_role_asks_for_cannot_be_had():
    runtime, runner = _runtime(8, {"main": 5})

    def gone(role: str) -> ModelChoice:
        raise NotAvailable("The model's file was deleted.")

    async def go():
        async with runtime.lease("main"):
            pass
        runtime.resolve = gone
        async with runtime.lease("main") as lease:
            return (await lease.client.get("/v1/models")).json()

    assert asyncio.run(go()) == {"model": "main-model"}
    assert runner.started == ["main-model"]


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


def test_a_job_that_would_unload_the_chats_model_waits_for_the_chat_to_be_idle():
    """A reply is several model calls; a job asking in between waits for the reply to end,
    so the reply isn't split by a swap. (The turn is declared, as the TurnManager does;
    the idle window after the reply is in test_a_message_after_the_job_started_…)"""
    runtime, runner = _runtime(6, {"main": 5, "ocr": 2})  # they don't fit together
    runtime.chat_idle_resume = 0.3
    events: list[dict] = []

    async def go():  # noqa: ANN202
        async with runtime.events.listen() as queue:
            await runtime.turn_started(message=True)
            async with runtime.lease("main", Priority.CHAT):
                pass  # the chat's first model call
            job = asyncio.create_task(job_page())
            await asyncio.sleep(0.05)
            async with runtime.lease("main", Priority.CHAT):  # its next call, 50 ms later
                pass
            during = list(runner.started)
            await runtime.turn_ended()
            await asyncio.wait_for(job, 5)
            while not queue.empty():
                events.append(queue.get_nowait())
            return during

    async def job_page() -> None:
        async with runtime.lease("ocr", Priority.JOB):
            pass

    during = asyncio.run(go())
    assert during == ["main-model"]  # no swap between the chat's calls
    assert runner.started == ["main-model", "ocr-model"]
    assert any(e["event"] == "wait" and e.get("reason") == "chat" for e in events)


def test_a_job_whose_model_fits_beside_the_chats_is_not_held():
    runtime, runner = _runtime(16, {"main": 5, "ocr": 2})
    runtime.chat_idle_resume = 3600.0  # a hold would never end within the test

    async def go():  # noqa: ANN202
        async with runtime.events.listen() as queue:
            async with runtime.lease("main", Priority.CHAT):
                pass
            async with runtime.lease("ocr", Priority.JOB):
                pass
            return [queue.get_nowait() for _ in range(queue.qsize())]

    events = asyncio.run(asyncio.wait_for(go(), 5))
    assert not any(e["event"] == "wait" for e in events)  # admitted at once, beside main
    assert runner.started == ["main-model", "ocr-model"]


def test_a_chat_waiting_for_the_gpu_counts_as_active():
    runtime, _ = _runtime(6, {"main": 5, "ocr": 2})

    async def go():  # noqa: ANN202
        async with runtime.lease("ocr", Priority.JOB):
            chat = asyncio.create_task(chat_once())
            await _until(lambda: any(p == Priority.CHAT for p, _ in runtime._queue))
            idle = runtime.chat_idle_seconds()
        await chat
        return idle

    async def chat_once() -> None:
        async with runtime.lease("main", Priority.CHAT):
            pass

    assert asyncio.run(go()) == 0.0


class GatedRunner(FakeRunner):
    """Its starts wait for `gate`, so a load can be seen while it's happening."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def start(self, spec: RunnerSpec) -> FakeHandle:
        await self.gate.wait()
        return await super().start(spec)


def test_the_runtime_says_which_model_it_is_loading():
    runner = GatedRunner()
    runtime, _ = _runtime(8, {"main": 5}, runner)

    async def go():  # noqa: ANN202
        async with runtime.events.listen() as queue:
            chat = asyncio.create_task(lease_once())
            await _until(lambda: runtime.state()["activity"] is not None)
            during = runtime.state()["activity"]
            runner.gate.set()
            await chat
            events = [queue.get_nowait() for _ in range(queue.qsize())]
            return during, runtime.state()["activity"], events

    async def lease_once() -> None:
        async with runtime.lease("main", Priority.CHAT):
            pass

    during, after, events = asyncio.run(go())
    assert during == {"kind": "loading", "role": "main", "model_id": "main-model"}
    assert after is None
    assert [e["event"] for e in events][:2] == ["loading", "loaded"]


def test_the_engine_download_shows_as_activity_until_it_is_done():
    runtime, _ = _runtime(8, {"main": 5})

    async def go():  # noqa: ANN202
        async with runtime.events.listen() as queue:
            runtime.engine_progress("cuda-12", 40, 400)
            during = runtime.state()["activity"]
            runtime.engine_progress("cuda-12", 400, 400)
            events = [queue.get_nowait() for _ in range(queue.qsize())]
            return during, runtime.state()["activity"], events

    during, after, events = asyncio.run(go())
    assert during == {"kind": "engine", "variant": "cuda-12", "done": 40, "total": 400}
    assert after is None
    assert [(e["event"], e.get("done")) for e in events] == [("engine", 40), ("engine", 400)]


def test_a_job_the_chat_started_waits_for_the_reply_not_for_the_idle_window():
    """The chat called ocr.start mid-reply: the job waits until that reply is written, then
    goes, without the idle window (the message that asked for it came before it)."""
    import time

    runtime, runner = _runtime(6, {"main": 5, "ocr": 2})  # they don't fit together
    runtime.chat_idle_resume = 3600.0  # a window would never end within the test

    async def go():  # noqa: ANN202
        await runtime.turn_started(message=True)
        async with runtime.lease("main", Priority.CHAT):
            pass  # the call that ran ocr.start
        job = asyncio.create_task(job_page(time.monotonic()))
        await _until(lambda: runtime.state()["hold"] is not None)
        hold = runtime.state()["hold"]
        async with runtime.lease("main", Priority.CHAT):
            pass  # the reply's last call: main is still there, no swap in between
        started_before_end = list(runner.started)
        await runtime.turn_ended()
        await asyncio.wait_for(job, 5)
        return hold, started_before_end

    async def job_page(since: float) -> None:
        async with runtime.lease("ocr", Priority.JOB, since=since):
            pass

    hold, before = asyncio.run(go())
    assert hold == {"role": "ocr", "resume_in": None}  # waiting for the reply to end
    assert before == ["main-model"]
    assert runner.started == ["main-model", "ocr-model"]


def test_a_message_after_the_job_started_holds_it_for_the_idle_window():
    """The person writes while a job runs: its next page waits until the chat has been
    quiet for the window, as before."""
    import time

    runtime, _ = _runtime(6, {"main": 5, "ocr": 2})
    runtime.chat_idle_resume = 0.3

    async def go():  # noqa: ANN202
        since = time.monotonic()  # the job started
        await asyncio.sleep(0.01)
        await runtime.turn_started(message=True)
        async with runtime.lease("main", Priority.CHAT):
            pass
        await runtime.turn_ended()
        started = time.monotonic()
        async with runtime.lease("ocr", Priority.JOB, since=since):
            return time.monotonic() - started

    assert asyncio.run(go()) >= 0.25


def test_start_now_lets_a_held_job_in():
    import time

    runtime, runner = _runtime(6, {"main": 5, "ocr": 2})
    runtime.chat_idle_resume = 3600.0

    async def go():  # noqa: ANN202
        since = time.monotonic()
        await runtime.turn_started(message=True)
        async with runtime.lease("main", Priority.CHAT):
            pass
        await runtime.turn_ended()
        job = asyncio.create_task(job_page(since))
        await _until(lambda: runtime.state()["hold"] is not None)
        hold = runtime.state()["hold"]
        await runtime.skip_hold()
        await asyncio.wait_for(job, 5)
        return hold, runtime.state()["hold"]

    async def job_page(since: float) -> None:
        async with runtime.lease("ocr", Priority.JOB, since=since):
            pass

    hold, after = asyncio.run(go())
    assert hold["role"] == "ocr" and 3590 < hold["resume_in"] <= 3600
    assert after is None
    assert runner.started == ["main-model", "ocr-model"]


def test_a_follow_up_turn_is_not_a_message():
    """A turn the app starts (a follow-up after OCR) keeps the chat busy while it runs, but
    doesn't bring back the idle window for the jobs that were already running."""
    import time

    runtime, _ = _runtime(6, {"main": 5, "ocr": 2})
    runtime.chat_idle_resume = 3600.0

    async def go():  # noqa: ANN202
        since = time.monotonic()
        await asyncio.sleep(0.01)
        await runtime.turn_started(message=False)
        async with runtime.lease("main", Priority.CHAT):
            pass
        await runtime.turn_ended()
        async with runtime.lease("ocr", Priority.JOB, since=since):
            return True

    assert asyncio.run(asyncio.wait_for(go(), 5))


def test_turns_are_reported_so_the_pages_can_update_the_hold():
    runtime, _ = _runtime(8, {"main": 5})

    async def go():  # noqa: ANN202
        async with runtime.events.listen() as queue:
            await runtime.turn_started(message=True)
            await runtime.turn_ended()
            return [(e["event"], e.get("status")) for e in
                    (queue.get_nowait() for _ in range(queue.qsize()))]  # fmt: skip

    assert asyncio.run(go()) == [("turn", "started"), ("turn", "ended")]


def test_a_held_job_that_is_cancelled_leaves_no_hold_behind():
    """Pause cancels a page's lease while it waits for the chat: the hold the pages show
    goes with it, rather than saying "OCR waits for the chat" until some other job runs."""
    import time

    runtime, _ = _runtime(6, {"main": 5, "ocr": 2})
    runtime.chat_idle_resume = 3600.0

    async def go():  # noqa: ANN202
        await runtime.turn_started(message=True)
        async with runtime.lease("main", Priority.CHAT):
            pass
        job = asyncio.create_task(job_page(time.monotonic()))
        await _until(lambda: runtime.state()["hold"] is not None)
        job.cancel()  # as Pause does
        with contextlib.suppress(asyncio.CancelledError):
            await job
        await runtime.turn_ended()
        return runtime.state()["hold"]

    async def job_page(since: float) -> None:
        async with runtime.lease("ocr", Priority.JOB, since=since):
            pass

    assert asyncio.run(go()) is None


def test_start_now_with_no_job_waiting_does_not_skip_a_later_hold():
    """A click that lands just as the page started mustn't let some later job, after a new
    message, skip its wait."""
    import time

    runtime, _ = _runtime(6, {"main": 5, "ocr": 2})
    runtime.chat_idle_resume = 3600.0

    async def go():  # noqa: ANN202
        since = time.monotonic()
        await runtime.skip_hold()  # nothing is waiting
        await asyncio.sleep(0.01)
        await runtime.turn_started(message=True)
        async with runtime.lease("main", Priority.CHAT):
            pass
        await runtime.turn_ended()
        return runtime.job_hold("ocr", since)

    assert asyncio.run(go()) > 3000  # still held: the stray click was ignored


def test_a_turn_waiting_for_a_card_lets_a_held_job_in():
    """The chat asks the person (a card) mid-turn: the model is idle, so a held job goes.
    Once answered, the chat's next call waits for the job's page, not the other way round;
    no idle window follows (the job started after the message)."""
    import time

    runtime, runner = _runtime(6, {"main": 5, "ocr": 2})
    runtime.chat_idle_resume = 3600.0
    order: list[str] = []
    page_may_end = asyncio.Event()

    async def go():  # noqa: ANN202
        await runtime.turn_started(message=True)
        async with runtime.lease("main", Priority.CHAT):
            pass  # the call that ran ocr.start
        job_since = time.monotonic()  # the job started after the message that asked for it
        job = asyncio.create_task(job_page(job_since))
        await _until(lambda: runtime.state()["hold"] is not None)
        held = runtime.state()["hold"]
        async with runtime.waiting_for_person():  # the export card
            await _until(lambda: "page" in order)
        reply = asyncio.create_task(chat_call())
        await _until(lambda: runtime.state()["waiting"] > 0)  # the reply queues behind the page
        order.append("answered")
        page_may_end.set()
        await asyncio.wait_for(asyncio.gather(job, reply), 5)
        await runtime.turn_ended()
        await asyncio.wait_for(job_page(job_since), 5)  # its next page: no 3600 s window
        return held

    async def job_page(since: float) -> None:
        async with runtime.lease("ocr", Priority.JOB, since=since):
            order.append("page")
            await page_may_end.wait()
            order.append("page done")

    async def chat_call() -> None:
        async with runtime.lease("main", Priority.CHAT):
            order.append("reply")

    held = asyncio.run(go())
    assert held == {"role": "ocr", "resume_in": None}
    assert order[:4] == ["page", "answered", "page done", "reply"]
    assert runner.started[:3] == ["main-model", "ocr-model", "main-model"]


def test_waiting_for_a_person_is_reported_and_undone_on_error():
    runtime, _ = _runtime(6, {"main": 5, "ocr": 2})

    async def go():  # noqa: ANN202
        async with runtime.events.listen() as queue:
            await runtime.turn_started(message=True)
            with pytest.raises(RuntimeError):
                async with runtime.waiting_for_person():
                    raise RuntimeError("cancelled card")
            active = runtime.chat_idle_seconds() == 0  # the turn counts again
            return active, [e.get("status") for e in
                            (queue.get_nowait() for _ in range(queue.qsize()))
                            if e["event"] == "turn"]  # fmt: skip

    active, statuses = asyncio.run(go())
    assert active
    assert statuses == ["started", "waiting", "resumed"]
