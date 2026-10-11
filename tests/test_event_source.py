"""The loop every event stream runs (`lectern.api.sse.follow`): what it hands on, and when it
ends. Its transports, the HTTP stream and the WebSocket, are tested where they are served."""

from __future__ import annotations

import asyncio

from lectern.api import sse


async def _never_gone() -> bool:
    return False


def _run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


def test_it_hands_on_what_came_first_and_then_what_arrives():
    async def go() -> list:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        stopping = asyncio.Event()
        source = sse.follow(queue, [{"n": 1}, {"n": 2}], stopping, _never_gone)
        seen = [await anext(source), await anext(source)]
        queue.put_nowait({"n": 3})
        seen.append(await anext(source))
        await source.aclose()
        return seen

    assert _run(go()) == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_a_quiet_wait_is_handed_on_as_none(monkeypatch):
    monkeypatch.setattr(sse, "KEEPALIVE_SECONDS", 0)  # no real wait: quiet at once

    async def go() -> object:
        source = sse.follow(asyncio.Queue(), [], asyncio.Event(), _never_gone)
        quiet = await anext(source)
        await source.aclose()
        return quiet

    assert _run(go()) is None


def test_it_ends_when_the_app_starts_shutting_down():
    async def go() -> list:
        stopping = asyncio.Event()
        source = sse.follow(asyncio.Queue(), [{"n": 1}], stopping, _never_gone)
        seen = [await anext(source)]
        # Set while it waits for the next event: the wait ends, and so does the stream.
        asyncio.get_running_loop().call_soon(stopping.set)
        seen.extend([event async for event in source])
        return seen

    assert _run(go()) == [{"n": 1}]


def test_it_ends_when_the_listener_is_gone():
    async def go() -> list:
        answers = iter([False, True])

        async def gone() -> bool:
            return next(answers)

        queue: asyncio.Queue[dict] = asyncio.Queue()
        queue.put_nowait({"n": 1})
        source = sse.follow(queue, [], asyncio.Event(), gone)
        return [event async for event in source]

    assert _run(go()) == [{"n": 1}]


def test_closing_it_mid_wait_leaves_no_task_behind():
    """A browser that goes away cancels the stream while it waits. The two waits it started
    (the next event, the shutdown) must be cancelled with it: left pending they are reported
    as "Task was destroyed but it is pending!" when collected."""

    async def go() -> set:
        before = asyncio.all_tasks()
        source = sse.follow(asyncio.Queue(), [], asyncio.Event(), _never_gone)
        waiting = asyncio.ensure_future(anext(source))
        await asyncio.sleep(0)  # let it reach its wait
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await asyncio.sleep(0)  # let the cancelled waits finish
        return {t for t in asyncio.all_tasks() - before if not t.done()}

    assert _run(go()) == set()
