"""Chat turns run as server tasks, one at a time per chat.

Sending a message stores it and starts the turn; the request returns at once. The page
follows the turn over SSE. Every event of the current turn is kept (numbered by `seq`), so a
page that reloads mid-reply gets them all again and then the live ones.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from starlette.concurrency import run_in_threadpool

from lectern.agent import followups
from lectern.agent.loop import TurnRun, run_tool, run_turn, store_message
from lectern.agent.tools import Registry
from lectern.core.auth import LOCAL_USER, User
from lectern.projects.store import NEW_CHAT, UNTITLED, Project, ProjectStore
from lectern.projects.tables import ArtifactRow, SessionRow
from lectern.runtime.supervisor import ModelRuntime

TITLE_WORDS = 6
TITLE_CHARS = 60

Key = tuple[int, int]  # (project id, session id)


class Busy(Exception):
    """The chat is already answering."""


@dataclass
class Channel:
    """A chat's events: the current turn's so far, and whoever is listening."""

    events: list[dict] = field(default_factory=list)
    listeners: set[asyncio.Queue[dict]] = field(default_factory=set)
    seq: int = 0

    def publish(self, event: dict) -> None:
        self.seq += 1
        event = {**event, "seq": self.seq}
        self.events.append(event)
        for queue in list(self.listeners):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    def settle(self) -> None:
        """After a turn: drop its streamed text pieces, which the transcript now holds in
        full. The few events left (model, tools, cards, done) still let a page that
        reattaches show the model line, the stats and any error."""
        self.events = [e for e in self.events if e["type"] != "delta"]

    @contextlib.asynccontextmanager
    async def listen(self) -> AsyncIterator[tuple[list[dict], asyncio.Queue[dict]]]:
        """The events so far and a queue of the ones after them, taken together so nothing
        falls between the two."""
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=4096)
        self.listeners.add(queue)
        try:
            yield list(self.events), queue
        finally:
            self.listeners.discard(queue)


@dataclass
class Turn:
    task: asyncio.Task[str]
    waiting: dict[int, asyncio.Future[bool]] = field(default_factory=dict)


class TurnManager:
    def __init__(
        self,
        runtime: ModelRuntime,
        projects: ProjectStore,
        registry: Registry | Callable[[], Registry],
        *,
        downloads: object = None,
        sandbox: object = None,
        ocr_jobs: object = None,
    ) -> None:
        self.runtime = runtime
        self.downloads = downloads
        self.sandbox = sandbox
        self.ocr_jobs = ocr_jobs
        self.projects = projects
        # A fixed registry, or a function giving each turn's (modules offer tools depending
        # on what's available right now, such as a running container engine).
        self._registry = registry if callable(registry) else (lambda: registry)
        self._channels: dict[Key, Channel] = {}
        self._turns: dict[Key, Turn] = {}
        # Chats whose message is being stored and whose turn hasn't started yet: a second
        # send in that gap must see the chat as busy.
        self._starting: set[Key] = set()
        self._follow_ups: set[asyncio.Task[None]] = set()
        self._follow_up_keys: set[tuple[int, int, str]] = set()  # (project, chat, follow-up)

    def channel(self, key: Key) -> Channel:
        return self._channels.setdefault(key, Channel())

    def state(self, key: Key) -> str:
        if key in self._starting:
            return "running"
        turn = self._turns.get(key)
        if turn is None or turn.task.done():
            return "idle"
        return "waiting" if any(not f.done() for f in turn.waiting.values()) else "running"

    async def send(
        self, project: Project, user: User, session_id: int, text: str, attachments: list[int]
    ) -> int:
        """Store the person's message and start the turn. Returns the message id."""
        key = (project.id, session_id)
        if self.state(key) != "idle":
            raise Busy()
        self._starting.add(key)  # before the first await, so nothing can slip in between
        try:
            return await self._start(key, project, user, session_id, text, attachments)
        finally:
            self._starting.discard(key)

    async def _start(
        self,
        key: Key,
        project: Project,
        user: User,
        session_id: int,
        text: str,
        attachments: list[int],
    ) -> int:
        sessions = await run_in_threadpool(self.projects.db, project)
        valid = await run_in_threadpool(_live_attachments, sessions, user, attachments)
        run = self._new_run(key, project, sessions, user, session_id)
        message_id = await run_in_threadpool(
            lambda: store_message(
                run, role="user", content=text, attachments=json.dumps(valid) if valid else None
            )
        )
        await run_in_threadpool(self._name_from_first_message, project, user, session_id, text)

        channel = self.channel(key)
        channel.events.clear()
        run.emit = channel.publish
        channel.publish(
            {"type": "turn", "status": "running", "message_id": message_id, "text": text,
             "attachments": valid}
        )  # fmt: skip

        async def turn() -> str:
            await self.runtime.turn_started(message=True)  # jobs wait for the reply
            try:
                run.registry = await run_in_threadpool(self._registry)
                status = await run_turn(run)
            finally:
                await self.runtime.turn_ended()
            channel.publish({"type": "turn", "status": status})
            channel.settle()
            await self.resume_follow_ups(project, session_id)  # any whose session ended meanwhile
            return status

        self._turns[key] = Turn(asyncio.create_task(turn(), name=f"turn-{key}"))
        return message_id

    def _new_run(
        self, key: Key, project: Project, sessions: object, user: User, session_id: int
    ) -> TurnRun:
        return TurnRun(
            project=project,
            sessions=sessions,
            user=user,
            session_id=session_id,
            runtime=self.runtime,
            registry=Registry([]),  # built in the turn: checking the engine can take seconds
            emit=lambda event: None,
            decide=lambda confirmation_id: self._decision(key, confirmation_id),
            turn_state=lambda sid: self.state((project.id, sid)),
            downloads=self.downloads,
            sandbox=self.sandbox,
            ocr_jobs=self.ocr_jobs,
        )

    # --- follow-ups: what a chat queued for when an OCR session finishes --------------------

    async def after_session(self, project: Project, ocr_session_id: int, status: str,
                            notice: str) -> None:  # fmt: skip
        """An OCR session stopped. Done: each chat's follow-up for it runs, then the model
        reports. Failed: they're dropped, and the model says so. Anything else (paused): they
        keep waiting. Each runs in its own task, once its chat is idle."""
        if status not in ("done", "failed"):
            return
        sessions = await run_in_threadpool(self.projects.db, project)
        waiting = await run_in_threadpool(followups.waiting_on, sessions, ocr_session_id)
        for chat_id, follow in waiting:
            self._schedule(project, sessions, chat_id, follow, status, notice)

    async def resume_follow_ups(self, project: Project, chat_id: int) -> None:
        """The chat's follow-ups whose session already finished, failed or is gone, run or
        dropped now. The signal from the session is sent once; if the app stopped before a
        follow-up ran, this is what runs it. Called when the chat is opened and after each of
        its turns."""
        sessions = await run_in_threadpool(self.projects.db, project)
        stale = await run_in_threadpool(followups.stale, sessions, chat_id)
        for follow, status, notice in stale:
            self._schedule(project, sessions, chat_id, follow, status, notice)

    def _schedule(self, project: Project, sessions: object, chat_id: int,
                  follow: followups.FollowUp, status: str, notice: str) -> None:  # fmt: skip
        """One task per follow-up at a time, however many signals arrive."""
        key = (project.id, chat_id, follow.id)
        if key in self._follow_up_keys:
            return
        self._follow_up_keys.add(key)
        task = asyncio.create_task(
            self._follow_up(project, sessions, chat_id, follow, status, notice),
            name=f"follow-up-{project.id}-{chat_id}-{follow.id}",
        )
        self._follow_ups.add(task)
        task.add_done_callback(self._follow_ups.discard)
        task.add_done_callback(lambda _t: self._follow_up_keys.discard(key))

    async def follow_ups_settled(self) -> None:
        """For tests and shutdown: until every follow-up and the turn it started are over."""
        # A turn's end can schedule a follow-up, whose turn can end and schedule another.
        while True:
            busy = [*self._follow_ups, *(t.task for t in self._turns.values() if not t.task.done())]
            if not busy:
                return
            await asyncio.gather(*busy, return_exceptions=True)

    async def _follow_up(self, project: Project, sessions: object, chat_id: int,
                         follow: followups.FollowUp, status: str, notice: str) -> None:  # fmt: skip
        key = (project.id, chat_id)
        while self.state(key) != "idle":  # the chat is answering: after its turn
            await self.wait(key)
            await asyncio.sleep(0)
        self._starting.add(key)  # before the first await, so a message can't slip in
        try:
            taken = await run_in_threadpool(followups.take, sessions, chat_id, follow.id)
            if taken is None:  # already run by an earlier signal
                return
            owner = await run_in_threadpool(_owner, sessions, chat_id)
            run = self._new_run(key, project, sessions, owner, chat_id)
            channel = self.channel(key)
            channel.events.clear()
            run.emit = channel.publish
            channel.publish({"type": "turn", "status": "running", "followup": taken.label})

            async def turn() -> str:
                # A turn, but not the person's message: no idle window for running jobs.
                await self.runtime.turn_started(message=False)
                try:
                    return await follow_up_turn()
                finally:
                    await self.runtime.turn_ended()

            async def follow_up_turn() -> str:
                run.registry = await run_in_threadpool(self._registry)
                try:
                    if status == "done":
                        await _run_follow_up(run, taken, notice)
                    else:
                        text = f"{notice} The follow-up ({taken.label}) was dropped."
                        await run_in_threadpool(
                            lambda: store_message(run, role="assistant", content=text)
                        )
                except Exception as exc:  # noqa: BLE001 — a tool's own failure (a full disk…)
                    # The page shows the turn as running until it hears it ended.
                    channel.publish(
                        {"type": "error", "detail": f"The follow-up couldn't run: {exc}"}
                    )
                    channel.publish({"type": "turn", "status": "error"})
                    channel.settle()
                    return "error"
                result = await run_turn(run)
                channel.publish({"type": "turn", "status": result})
                channel.settle()
                await self.resume_follow_ups(project, chat_id)
                return result

            self._turns[key] = Turn(asyncio.create_task(turn(), name=f"turn-{key}"))
        finally:
            self._starting.discard(key)

    def _decision(self, key: Key, confirmation_id: int) -> asyncio.Future[bool]:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._turns[key].waiting[confirmation_id] = future
        return future

    def decide(self, key: Key, confirmation_id: int, approve: bool) -> bool:
        """The person's answer to a card. False when nothing is waiting for it."""
        turn = self._turns.get(key)
        future = turn.waiting.get(confirmation_id) if turn else None
        if future is None or future.done():
            return False
        future.set_result(approve)
        return True

    async def stop(self, key: Key) -> bool:
        turn = self._turns.get(key)
        if turn is None or turn.task.done():
            return False
        turn.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn.task
        return True

    async def stop_all(self) -> None:
        for key in list(self._turns):
            await self.stop(key)

    async def wait(self, key: Key) -> str | None:
        """For tests and shutdown: the turn's final status."""
        turn = self._turns.get(key)
        return await turn.task if turn else None

    def _name_from_first_message(
        self, project: Project, user: User, session_id: int, text: str
    ) -> None:
        """A new chat, and an untitled project, take their name from the first message."""
        title = _title(text)
        if not title:
            return
        with self.projects.db(project).begin() as session:
            chat = session.get(SessionRow, session_id)
            if chat is not None and not chat.titled and chat.title == NEW_CHAT:
                chat.title, chat.titled = title, True
        if project.name == UNTITLED:
            self.projects.rename(user, project.id, title)


def _title(text: str) -> str:
    words = text.split()
    title = " ".join(words[:TITLE_WORDS])
    if len(title) > TITLE_CHARS:
        title = title[:TITLE_CHARS].rsplit(" ", 1)[0]
    return title + ("…" if len(words) > TITLE_WORDS or len(" ".join(words)) > len(title) else "")


def _live_attachments(sessions, user: User, ids: list[int]) -> list[int]:  # noqa: ANN001
    """Only this person's files that are still in the project."""
    with sessions() as session:
        return [
            i
            for i in dict.fromkeys(ids)
            if (row := session.get(ArtifactRow, i)) is not None
            and row.trashed_at is None
            and row.owner_id == user.id
        ]


async def _run_follow_up(run: TurnRun, follow: followups.FollowUp, notice: str) -> None:
    """The follow-up's tool call, stored as the model's (after the notice of what finished)
    with its result, so the model's report reads it like any tool result."""
    call = {
        "id": f"followup-{follow.id}",
        "type": "function",
        "function": {
            "name": follow.tool.replace(".", "_"),
            "arguments": json.dumps(follow.arguments, ensure_ascii=False),
        },
    }
    message_id = await run_in_threadpool(
        lambda: store_message(run, role="assistant", content=notice, tool_calls=json.dumps([call]))
    )
    run.emit({"type": "assistant", "message_id": message_id, "tool_calls": [call]})
    result = await run_tool(run, call, {})
    tool = run.registry.get(call["function"]["name"])
    await run_in_threadpool(
        lambda: store_message(run, role="tool", content=result.text, tool_call_id=call["id"],
                              tool_name=tool.name if tool else follow.tool)
    )  # fmt: skip
    run.emit({"type": "tool_result", "call_id": call["id"], "text": result.text[:600],
              "error": result.text.startswith("Error:"), "refs": result.refs,
              "action": result.action})  # fmt: skip


def _owner(sessions, chat_id: int) -> User:  # noqa: ANN001
    """Who the chat belongs to: a follow-up runs as them."""
    with sessions() as session:
        owner_id = session.get(SessionRow, chat_id).owner_id
    return LOCAL_USER if owner_id == LOCAL_USER.id else User(id=owner_id, name="")
