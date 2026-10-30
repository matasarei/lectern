"""The Model Runtime: which models are loaded, who is using them, and what gets unloaded.

`async with runtime.lease("main", Priority.CHAT) as lease:` gives an OpenAI-compatible client
for the role's model, loading it first if needed. Rules:

- A model that fits next to the loaded ones is simply loaded.
- Otherwise idle models (no lease) are unloaded, least recently used first. Each such
  unload-to-make-room counts as a swap.
- A model in use is never unloaded: the request waits until its lease ends.
- Waiting requests are served chat first, then in arrival order. So a chat that arrives during
  an OCR job gets the GPU after the current page, and the job continues afterwards.
- With `chat_idle_resume` set, a job whose model would unload another one waits until the
  chat has been idle that long (a chat still waiting for the GPU counts as active). A chat
  reply is several model calls; without this, a waiting job takes the GPU between two of
  them and every message costs two swaps.
- A runner that died is noticed on the next lease and started again.
- On Windows, which backs GPU memory with commit (RAM plus page file), a model that fits beside
  the loaded ones but would leave less than COMMIT_MARGIN of commit unloads an idle one first.

One asyncio.Condition guards all of it. Loading happens with the lock held: there is one GPU,
so loads are serial anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

import httpx2

from lectern.downloads.manager import EventHub
from lectern.hardware.tiers import Tier
from lectern.runners.base import Runner, RunnerHandle, RunnerSpec
from lectern.runtime import planner

log = logging.getLogger(__name__)

CHAT_IDLE_RESUME = 120.0  # seconds the chat is idle before a job may unload it (docs/models.md)
GIB = 1024**3
COMMIT_MARGIN = 2 * GIB  # of commit kept free on Windows after a model loads
# What a llama-server commits on Windows, against the plan's estimate: measured on the M4
# laptop (CUDA, docs/acceptance/m4.md) at 1.58x for PaddleOCR-VL (3.25 of 2.06 GB) and 1.36x
# for Qwen3.5 9B (8.28 of 6.08 GB).
COMMIT_PER_ESTIMATED = 1.5


class Priority(IntEnum):
    CHAT = 0
    JOB = 1


@dataclass(frozen=True)
class ModelChoice:
    """Everything the runtime needs to load a role's model."""

    role: str
    model_id: str
    model_file: Path
    weights: int  # bytes on disk, projector included
    shape: planner.ModelShape
    tier_ctx: int
    mmproj: Path | None = None
    format: str = "gguf"  # or "mlx"


class NotAvailable(Exception):
    """The role's model can't be loaded (not downloaded, doesn't fit, no runner)."""


class ModelInUse(Exception):
    """A copy of the model is leased, or a reply is being written with it."""


@dataclass
class Resident:
    choice: ModelChoice
    plan: planner.FitPlan
    handle: RunnerHandle
    bytes: int  # measured after load when the runner reports it, else the plan's estimate
    leases: int = 0
    last_used: float = field(default_factory=time.monotonic)


@dataclass
class Lease:
    role: str
    model_id: str
    client: httpx2.AsyncClient
    plan: planner.FitPlan


class ModelRuntime:
    def __init__(
        self,
        *,
        budget: int,
        unified: bool,
        tier: Tier,
        resolve: Callable[[str], ModelChoice],
        runner_for: Callable[[ModelChoice], Runner],
        chat_idle_resume: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        commit_available: Callable[[], int | None] = lambda: None,
    ) -> None:
        self.budget = budget
        self.unified = unified
        self.tier = tier
        self.resolve = resolve
        self.runner_for = runner_for
        self.chat_idle_resume = chat_idle_resume
        # What every "when" here is read from. A test hands in its own: on Windows the real one
        # moves in 15 ms steps, and two readings a moment apart are often the same.
        self.clock = clock
        # Bytes the OS can still commit, where that's what runs out first (Windows); None
        # elsewhere, and then only the GPU budget counts.
        self.commit_available = commit_available
        self.events = EventHub()
        self.swaps = 0
        self._residents: dict[str, Resident] = {}  # by role
        self._crashed: set[str] = set()
        self._queue: list[tuple[int, int]] = []  # (priority, ticket), served in sorted order
        self._tickets = itertools.count()
        self._cond = asyncio.Condition()
        self._chat_leases = 0
        self._chat_ended: float | None = None  # when the last chat lease ended
        # What a wait is for, when it isn't a model in use: the engine downloading, or a model
        # loading. Shown in the chat, the OCR tab and Models & memory.
        self.activity: dict | None = None
        self.engine: Any = None  # wiring.Engine: the llama.cpp build, for first-run to fetch
        # The key OCR cleanup leases by, whose answer is held to a JSON schema: the chat's own
        # model, unless wiring knows better. A model can be chosen for cleanup, and on a Mac
        # with MLX on the chat's model is an MLX build, whose server can't do that; wiring
        # then names a GGUF model. And the model cleanup uses when none is chosen for it (a
        # catalog id; None here, where there's no catalog to pick from).
        self.cleanup_key: Callable[[], str] = lambda: "main"
        self.cleanup_default: Callable[[], str | None] = lambda: None
        # The model chosen for each role in Models & memory (role → catalog id); wiring reads
        # it from the settings. Nothing chosen: every role gets the one recommended here.
        self.chosen_models: Callable[[], Mapping[str, str | None]] = dict
        self._loading: dict | None = None  # what activity goes back to after the engine
        # The chat's turns, from the TurnManager: a job never takes the GPU mid-reply, and
        # waits out the idle window only for a message sent after the job started.
        self._turns = 0
        self._waiting_turns = 0  # of those, how many wait for the person (a card): model idle
        # What the running turns have written with. A turn leases its model once per model
        # call; between two of them nothing is leased, and the reply's model still stays.
        self._turn_models: set[str] = set()
        # Copies loaded by name ("main@<model>") that a change of settings found in use: no
        # lease asks for one by that name again (cleanup's next page asks for the new one), so
        # each goes when its last lease ends rather than when something needs its room.
        self._unload_when_free: set[str] = set()
        self._last_message_at: float | None = None
        self._skip_hold = False  # "Start now": the next job admission ignores the window
        self._held: dict | None = None  # the job waiting for the chat, for state()

    # --- reporting ---------------------------------------------------------------------------

    def resident_bytes(self, *, excluding: str | None = None) -> int:
        return sum(r.bytes for role, r in self._residents.items() if role != excluding)

    def state(self) -> dict:
        for role, resident in list(self._residents.items()):
            if not resident.handle.is_running():
                self._crashed.add(role)
        return {
            "budget": self.budget,
            "unified": self.unified,
            "used": self.resident_bytes(),
            "swaps": self.swaps,
            "waiting": len(self._queue),
            "activity": self.activity,
            "hold": None
            if self._held is None
            else {"role": self._held["role"], "resume_in": self._held["resume_in"]},  # fmt: skip
            "residents": [
                {
                    "role": role,
                    "model_id": r.choice.model_id,
                    "bytes": r.bytes,
                    "ctx": r.plan.ctx,
                    "kv_type": r.plan.kv_type,
                    "on_gpu": r.plan.on_gpu,
                    "leases": r.leases,
                    "status": "crashed" if role in self._crashed else "loaded",
                    "pid": r.handle.pid,
                }
                for role, r in self._residents.items()
            ],
        }

    def needs_room(self, role: str) -> bool:
        """Loading the role's model now would unload another one (a swap). False when it's
        already loaded, when nothing else is, or when it fits beside what is."""
        if (role in self._residents and role not in self._crashed) or not self._residents:
            return False
        choice = self.resolve(role)
        try:
            plan = planner.plan(
                weights=choice.weights,
                shape=choice.shape,
                tier=self.tier,
                tier_ctx=choice.tier_ctx,
                budget=self.budget,
                resident=self.resident_bytes(),
                unified=self.unified,
                chat=False,
            )
        except planner.DoesNotFit:
            return True
        if not plan.coresident:
            return True
        # Short of commit, an idle model goes; with every model in use it loads beside them.
        short = self._commit_short(plan) is not None
        return short and self._least_recently_used_idle() is not None

    def chat_idle_seconds(self) -> float:
        """How long since the chat last used a model: 0 while it is or is waiting for one,
        infinity if it never has."""
        if self._chat_active():
            return 0.0
        if self._chat_ended is None:
            return float("inf")
        return self.clock() - self._chat_ended

    def _chat_active(self) -> bool:
        """A reply is being written: a chat lease, a chat waiting for one, or a turn running
        between its model calls (tools). A turn waiting for the person's answer to a card
        isn't: nothing is being written, and the person may take minutes."""
        return bool(
            self._chat_leases
            or self._turns > self._waiting_turns
            or any(p == Priority.CHAT for p, _ in self._queue)
        )

    # --- the chat's turns (from the TurnManager) ---------------------------------------------

    async def turn_started(self, *, message: bool) -> None:
        """A chat turn begins: the person's message (message=True), or a turn the app runs,
        such as a follow-up after OCR (False)."""
        async with self._cond:
            self._turns += 1
            if message:
                self._last_message_at = self.clock()
            self._emit("turn", status="started")  # pages show the hold change at once
            self._cond.notify_all()

    async def turn_ended(self) -> None:
        async with self._cond:
            self._turns = max(0, self._turns - 1)
            if not self._turns:
                self._turn_models.clear()
            self._chat_ended = self.clock()
            self._emit("turn", status="ended")
            self._cond.notify_all()

    @contextlib.asynccontextmanager
    async def waiting_for_person(self) -> AsyncIterator[None]:
        """A turn waits for the person to answer a card: a held job may use the GPU meanwhile.
        Once answered, the turn's next model call queues as a chat and waits only for the
        job's page in progress."""
        async with self._cond:
            self._waiting_turns += 1
            self._emit("turn", status="waiting")
            self._cond.notify_all()
        try:
            yield
        finally:
            async with self._cond:
                self._waiting_turns = max(0, self._waiting_turns - 1)
                self._emit("turn", status="resumed")
                self._cond.notify_all()

    async def skip_hold(self) -> None:
        """Start now: the next job that waits for the chat goes at once (between the chat's
        model calls if it must; never taking a model in use)."""
        async with self._cond:
            if self._held is not None:  # only for a job waiting now, never a later one
                self._skip_hold = True
                self._cond.notify_all()

    def engine_progress(self, variant: str, done: int, total: int) -> None:
        """The llama.cpp build downloading (the runner's installer reports it, throttled)."""
        finished = done >= total
        # It downloads during a model's load (the first start), which the status goes back to.
        self.activity = self._loading if finished else {
            "kind": "engine", "variant": variant, "done": done, "total": total
        }  # fmt: skip
        self._emit("engine", variant=variant, done=done, total=total)

    def _emit(self, event: str, **data: object) -> None:
        self.events.publish({"type": "runtime", "event": event, "swaps": self.swaps, **data})

    # --- leasing -----------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def lease(
        self, role: str, priority: Priority = Priority.JOB, *, since: float | None = None
    ) -> AsyncIterator[Lease]:
        """`since`: when the job asking started (by `clock`), so a page of a job that
        began before the person's last message waits for the chat, and one the chat itself
        started doesn't. Defaults to now."""
        resident = await self._acquire(role, priority, since)
        chat = priority is Priority.CHAT
        self._chat_leases += chat
        client = resident.handle.client()
        try:
            yield Lease(role, resident.choice.model_id, client, resident.plan)
        finally:
            await client.aclose()
            async with self._cond:
                resident.leases -= 1
                resident.last_used = self.clock()
                if chat:
                    self._chat_leases -= 1
                    self._chat_ended = resident.last_used
                self._emit("release", role=role, model_id=resident.choice.model_id)
                if (
                    not resident.leases
                    and role in self._unload_when_free
                    and self._residents.get(role) is resident
                ):
                    await self._unload(role, reason="changed")
                self._cond.notify_all()

    async def _acquire(self, role: str, priority: Priority, since: float | None) -> Resident:
        ticket = (int(priority), next(self._tickets))
        since = self.clock() if since is None else since
        async with self._cond:
            self._queue.append(ticket)
            self._queue.sort()
            try:
                waited = False
                while True:
                    head = self._queue[0] == ticket
                    hold = self._job_hold(role, since) if head and priority is Priority.JOB else 0
                    if hold > 0:
                        until_reply = hold == float("inf")
                        self._held = {
                            "role": role, "resume_in": None if until_reply else round(hold),
                            "ticket": ticket,
                        }  # fmt: skip
                        if not waited:
                            self._emit("wait", role=role, priority="job", reason="chat")
                            waited = True
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._cond.wait(), None if until_reply else hold)
                        continue
                    if priority is Priority.JOB and head:
                        self._held = None
                        self._skip_hold = False
                    if head:
                        resident = await self._try_admit(role, chat=priority is Priority.CHAT)
                        if resident is not None:
                            resident.leases += 1
                            if priority is Priority.CHAT and self._turns:
                                self._turn_models.add(role)
                            self._emit("lease", role=role, model_id=resident.choice.model_id)
                            return resident
                    if not waited:
                        self._emit("wait", role=role, priority=priority.name.lower())
                        waited = True
                    await self._cond.wait()
            finally:
                self._queue.remove(ticket)
                if self._held is not None and self._held["ticket"] == ticket:
                    self._held = None  # cancelled while held (Pause): nothing waits any more
                self._cond.notify_all()

    def job_hold(self, role: str, since: float | None = None) -> float:
        """How long a job lease on `role` would wait for the chat now: 0, inf (until the reply
        being written ends), or seconds of the idle window. For the OCR tab's status."""
        return self._job_hold(role, self.clock() if since is None else since)

    def _job_hold(self, role: str, since: float) -> float:
        """With the lock held. How long a job's lease on `role` must still wait for the chat,
        only when loading it would unload another model: until the reply being written ends
        (inf), and then, only if the person wrote after the job started, until the chat has
        been quiet for chat_idle_resume seconds. A chat-started job doesn't wait for its own
        chat's quiet time; a chat reply's model calls aren't split by a swap."""
        if self.chat_idle_resume <= 0 or self._skip_hold or not self.needs_room(role):
            return 0.0
        if self._chat_active():
            return float("inf")
        if self._last_message_at is None or self._last_message_at <= since:
            return 0.0
        return max(0.0, self.chat_idle_resume - self.chat_idle_seconds())

    async def _try_admit(self, role: str, *, chat: bool) -> Resident | None:
        """With the lock held. The role's resident, or a newly loaded one, or None to wait."""
        current = self._residents.get(role)
        choice: ModelChoice | None = None
        if current is not None:
            if current.handle.is_running() and role not in self._crashed:
                choice = self._changed_choice(role, current)
                if choice is None:
                    return current
                await self._unload(role, reason="changed")
            else:
                self._emit("crash", role=role, model_id=current.choice.model_id)
                self._residents.pop(role)
                self._crashed.discard(role)
                await current.handle.stop()

        choice = choice or self.resolve(role)
        while True:
            try:
                plan = planner.plan(
                    weights=choice.weights,
                    shape=choice.shape,
                    tier=self.tier,
                    tier_ctx=choice.tier_ctx,
                    budget=self.budget,
                    resident=self.resident_bytes(),
                    unified=self.unified,
                    chat=chat,
                )
            except planner.DoesNotFit as exc:
                raise NotAvailable(str(exc)) from exc
            short = self._commit_short(plan) if plan.coresident else None
            if (plan.coresident and short is None) or not self._residents:
                return await self._load(role, choice, plan)
            victim = self._least_recently_used_idle()
            if victim is None and short is not None:
                # Short of commit with everything in use: loaded as before rather than kept
                # waiting; Windows may still grow its page file.
                return await self._load(role, choice, plan)
            if victim is None:
                return None  # everything loaded is in use: wait for a release
            if short is not None:
                log.warning("Unloading %s before loading %s: %s", victim, role, short)
            await self._unload(victim, reason="commit" if short is not None else "make room")
            self.swaps += 1

    def _changed_choice(self, role: str, current: Resident) -> ModelChoice | None:
        """The model the role asks for now, when it isn't the one loaded for it any more: a
        setting changed what the role runs on, or the model it prefers finished downloading.
        None keeps what's loaded: it's in use (leased, or a reply is being written with it),
        it's still the one, or the other can't be had."""
        if current.leases or role in self._turn_models:
            return None
        try:
            choice = self.resolve(role)
        except NotAvailable:
            return None
        return None if choice.model_id == current.choice.model_id else choice

    def _commit_short(self, plan: planner.FitPlan) -> str | None:
        """Why loading this plan would leave too little commit, or None when it wouldn't (or
        the OS doesn't count commit)."""
        available = self.commit_available()
        if available is None:
            return None
        cost = int(plan.est_bytes * COMMIT_PER_ESTIMATED)
        if available - cost >= COMMIT_MARGIN:
            return None
        return (
            f"{available / GIB:.1f} GB of commit left, and the model takes about "
            f"{cost / GIB:.1f} GB of it; {COMMIT_MARGIN / GIB:.0f} GB is kept free"
        )

    def _least_recently_used_idle(self) -> str | None:
        idle = [(r.last_used, role) for role, r in self._residents.items() if r.leases == 0]
        return min(idle)[1] if idle else None

    async def _load(self, role: str, choice: ModelChoice, plan: planner.FitPlan) -> Resident:
        self._emit("loading", role=role, model_id=choice.model_id, bytes=plan.est_bytes)
        self._loading = {"kind": "loading", "role": role, "model_id": choice.model_id}
        self.activity = self._loading
        try:
            return await self._start(role, choice, plan)
        finally:
            self._loading = self.activity = None

    async def _start(self, role: str, choice: ModelChoice, plan: planner.FitPlan) -> Resident:
        spec = RunnerSpec(
            model_id=choice.model_id,
            model_file=choice.model_file,
            ctx=plan.ctx,
            kv_type=plan.kv_type,
            fit_target_mib=plan.fit_target_mib,
            mmproj=choice.mmproj,
        )
        handle = await self.runner_for(choice).start(spec)
        measured = handle.measured_memory()
        used = measured.device_bytes or measured.projected_bytes or plan.est_bytes
        resident = Resident(
            choice=choice, plan=plan, handle=handle, bytes=used, last_used=self.clock()
        )
        self._residents[role] = resident
        self._emit("loaded", role=role, model_id=choice.model_id, bytes=used, ctx=plan.ctx)
        return resident

    async def _unload(self, role: str, *, reason: str) -> None:
        resident = self._residents.pop(role)
        self._crashed.discard(role)
        self._unload_when_free.discard(role)
        await resident.handle.stop()
        self._emit("unloaded", role=role, model_id=resident.choice.model_id, reason=reason)

    async def unload_idle(self, role: str | None = None) -> list[str]:
        """Unload idle models (all, or one role). Used to measure a cold first reply."""
        async with self._cond:
            roles = [r for r, res in self._residents.items() if res.leases == 0]
            if role is not None:
                roles = [r for r in roles if r == role]
            return await self._unload_all(roles)

    async def unload_between_replies(self, role: str | None = None) -> list[str]:
        """Unload idle models (all, or one role's, a copy loaded for it by name included),
        except one a reply is being written with: for a setting that changes which models
        are wanted. That model changes over with the next turn. A role's own model that a job
        is using changes over with its next lease. A copy loaded by name that a job is using
        goes when the job is done with it: nothing asks for it by that name again."""
        async with self._cond:
            keys = [r for r in self._residents if role is None or r.partition("@")[0] == role]
            in_use = [r for r in keys if self._residents[r].leases]
            self._unload_when_free.update(r for r in in_use if "@" in r)
            idle = [r for r in keys if r not in in_use and r not in self._turn_models]
            return await self._unload_all(idle)

    async def without_model(
        self, model_id: str, action: Callable[[], Awaitable[None]], *, wait: bool = False
    ) -> None:
        """Run `action` with no copy of the model loaded and none able to load until it's
        done: for removing the model's files, or putting an updated copy in their place. Idle
        copies are unloaded first, under whatever key they were loaded. When a copy is leased
        or a reply is being written with it: raises ModelInUse, having done nothing, or with
        `wait` waits until it's free. The runtime's lock is held throughout, so a lease that
        arrives meanwhile waits and then finds the model as `action` left it."""
        async with self._cond:
            while True:
                keys = [k for k, r in self._residents.items() if r.choice.model_id == model_id]
                if not any(self._residents[k].leases or k in self._turn_models for k in keys):
                    break
                if not wait:
                    raise ModelInUse(model_id)
                await self._cond.wait()  # a lease given back, or a turn ended
            await self._unload_all(keys)
            await action()

    async def _unload_all(self, roles: list[str]) -> list[str]:
        for role in roles:
            await self._unload(role, reason="requested")
        self._cond.notify_all()
        return roles

    async def stop_all(self) -> None:
        async with self._cond:
            for role in list(self._residents):
                await self._unload(role, reason="shutdown")
