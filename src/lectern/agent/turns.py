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

from lectern.agent.loop import TurnRun, run_turn, store_message
from lectern.agent.tools import Registry
from lectern.core.auth import User
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
        run = TurnRun(
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
            run.registry = await run_in_threadpool(self._registry)
            status = await run_turn(run)
            channel.publish({"type": "turn", "status": status})
            channel.settle()
            return status

        self._turns[key] = Turn(asyncio.create_task(turn(), name=f"turn-{key}"))
        return message_id

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
