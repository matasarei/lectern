"""Room without a runner (`ModelRuntime.reserve`): for sd-cli, which runs once per image."""

from __future__ import annotations

import asyncio

import pytest

from lectern.runtime.supervisor import NotAvailable, Priority

from .test_supervisor import GIB, _runtime, _until


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _reserved(runtime) -> list[str]:  # noqa: ANN001
    return [r["role"] for r in runtime.state()["reserved"]]


def test_reserving_unloads_an_idle_model_to_make_room_and_gives_it_back():
    runtime, runner = _runtime(6, {"main": 4})

    async def go():  # noqa: ANN202
        async with runtime.lease("main", Priority.CHAT):
            pass
        async with runtime.reserve("image", 3 * GIB, Priority.CHAT, model_id="pic") as held:
            during = runtime.state()
            assert held.bytes == 3 * GIB
        return during

    during = asyncio.run(asyncio.wait_for(go(), 5))
    assert during["residents"] == []  # the chat model went to make room
    assert during["reserved"] == [{"role": "image", "model_id": "pic", "bytes": 3 * GIB}]
    assert during["used"] == 3 * GIB
    assert runtime.swaps == 1
    assert runtime.state()["reserved"] == [] and runtime.state()["used"] == 0
    assert runner.handles[0].alive is False


def test_room_that_fits_beside_a_loaded_model_unloads_nothing():
    runtime, runner = _runtime(16, {"main": 4})

    async def go():  # noqa: ANN202
        async with runtime.lease("main", Priority.CHAT):
            pass
        async with runtime.reserve("image", 3 * GIB, Priority.CHAT):
            return [r["role"] for r in runtime.state()["residents"]]

    assert asyncio.run(asyncio.wait_for(go(), 5)) == ["main"]
    assert runtime.swaps == 0


def test_reserving_waits_for_a_model_in_use_and_never_unloads_it():
    runtime, runner = _runtime(6, {"ocr": 4})
    order: list[str] = []

    async def go():  # noqa: ANN202
        async with runtime.lease("ocr", Priority.JOB):
            image = asyncio.create_task(make_image())
            await _until(lambda: len(runtime._queue) == 1)
            order.append("page done")
        await asyncio.wait_for(image, 5)

    async def make_image() -> None:
        async with runtime.reserve("image", 3 * GIB, Priority.CHAT):
            order.append("image")

    asyncio.run(asyncio.wait_for(go(), 5))
    assert order == ["page done", "image"]
    assert runtime.swaps == 1  # the OCR model, once its page was done


def test_a_chat_waits_for_the_room_an_image_holds_instead_of_loading_over_it():
    runtime, runner = _runtime(6, {"main": 4})
    order: list[str] = []

    async def go():  # noqa: ANN202
        async with runtime.reserve("image", 3 * GIB, Priority.CHAT):
            chat = asyncio.create_task(chat_once())
            await _until(lambda: len(runtime._queue) == 1)
            order.append("image done")
        await asyncio.wait_for(chat, 5)

    async def chat_once() -> None:
        async with runtime.lease("main", Priority.CHAT):
            order.append("chat")

    asyncio.run(asyncio.wait_for(go(), 5))
    assert order == ["image done", "chat"]
    assert runner.started == ["main-model"]


def test_a_chat_that_fits_beside_the_image_loads_at_once():
    runtime, runner = _runtime(16, {"main": 4})

    async def go():  # noqa: ANN202
        async with (
            runtime.reserve("image", 3 * GIB, Priority.CHAT),
            runtime.lease("main", Priority.CHAT),
        ):
            return runner.started[:]

    assert asyncio.run(asyncio.wait_for(go(), 5)) == ["main-model"]


def test_the_room_is_given_back_when_the_work_fails_or_is_cancelled():
    runtime, _ = _runtime(6, {})

    async def fails() -> None:
        async with runtime.reserve("image", 3 * GIB):
            raise RuntimeError("sd-cli failed")

    async def stopped() -> None:
        async with runtime.reserve("image", 3 * GIB):
            await asyncio.Event().wait()  # until cancelled

    async def go():  # noqa: ANN202
        with pytest.raises(RuntimeError):
            await fails()
        assert _reserved(runtime) == []
        task = asyncio.create_task(stopped())
        await _until(lambda: _reserved(runtime) == ["image"])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _reserved(runtime) == [] and runtime._queue == []

    asyncio.run(asyncio.wait_for(go(), 5))


def test_a_second_image_waits_for_the_first():
    runtime, _ = _runtime(16, {})
    order: list[str] = []

    async def image(name: str, gate: asyncio.Event | None = None) -> None:
        async with runtime.reserve("image", 3 * GIB, Priority.CHAT):
            order.append(f"{name} start")
            if gate is not None:
                await gate.wait()
            order.append(f"{name} end")

    async def go():  # noqa: ANN202
        gate = asyncio.Event()
        first = asyncio.create_task(image("first", gate))
        await _until(lambda: order == ["first start"])
        second = asyncio.create_task(image("second"))
        await _until(lambda: len(runtime._queue) == 1)
        gate.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)

    asyncio.run(asyncio.wait_for(go(), 5))
    assert order == ["first start", "first end", "second start", "second end"]


def test_more_than_the_whole_budget_is_refused_at_once():
    runtime, _ = _runtime(4, {})

    async def go() -> None:
        async with runtime.reserve("image", 5 * GIB):
            pass

    with pytest.raises(NotAvailable, match="4.0 GB to use"):
        asyncio.run(go())
    assert runtime._queue == []


def test_on_a_shared_gpu_room_the_system_cant_spare_is_refused():
    # 8 GB budget, but only 4 GB of RAM free now (2 GB of it kept for everything else).
    runtime, _ = _runtime(8, {}, ram_available=lambda: 4 * GIB)

    async def go() -> None:
        async with runtime.reserve("image", 3 * GIB):
            pass

    with pytest.raises(NotAvailable, match="Closing some apps"):
        asyncio.run(go())


def test_a_job_reservation_begun_before_the_persons_message_waits_for_the_chat():
    runtime, _ = _runtime(6, {"main": 4})
    clock = runtime.clock = Clock()
    runtime.chat_idle_resume = 120.0

    async def go():  # noqa: ANN202
        began = clock.now
        async with runtime.lease("main", Priority.CHAT):
            pass
        clock.now += 1
        await runtime.turn_started(message=True)  # the person wrote after the job began
        clock.now += 1
        await runtime.turn_ended()
        task = asyncio.create_task(job(began))
        await _until(lambda: runtime._held is not None)
        held = dict(runtime._held)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return held

    async def job(since: float) -> None:
        async with runtime.reserve("image", 3 * GIB, Priority.JOB, since=since):
            pass

    held = asyncio.run(asyncio.wait_for(go(), 5))
    assert held["role"] == "image" and held["resume_in"] == 120
    assert runtime._queue == [] and runtime._held is None


def test_a_job_reservation_the_chat_itself_started_does_not_wait_for_the_chats_quiet():
    runtime, _ = _runtime(6, {"main": 4})
    clock = runtime.clock = Clock()
    runtime.chat_idle_resume = 120.0

    async def go():  # noqa: ANN202
        async with runtime.lease("main", Priority.CHAT):
            pass
        await runtime.turn_started(message=True)
        clock.now += 1
        since = clock.now  # the job began with this turn, after the message
        await runtime.turn_ended()
        async with runtime.reserve("image", 3 * GIB, Priority.JOB, since=since):
            return _reserved(runtime)

    assert asyncio.run(asyncio.wait_for(go(), 5)) == ["image"]
    assert runtime.swaps == 1
