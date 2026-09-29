"""The Model Runtime: which models are loaded, who is using them, and what gets unloaded.

`async with runtime.lease("main", Priority.CHAT) as lease:` gives an OpenAI-compatible client
for the role's model, loading it first if needed. Rules:

- A model that fits next to the loaded ones is simply loaded.
- Otherwise idle models (no lease) are unloaded, least recently used first. Each such
  unload-to-make-room counts as a swap.
- A model in use is never unloaded: the request waits until its lease ends.
- Waiting requests are served chat first, then in arrival order. So a chat that arrives during
  an OCR job gets the GPU after the current page, and the job continues afterwards.
- A runner that died is noticed on the next lease and started again.

One asyncio.Condition guards all of it. Loading happens with the lock held: there is one GPU,
so loads are serial anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

import httpx2

from lectern.downloads.manager import EventHub
from lectern.hardware.tiers import Tier
from lectern.runners.base import Runner, RunnerHandle, RunnerSpec
from lectern.runtime import planner


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


class NotAvailable(Exception):
    """The role's model can't be loaded (not downloaded, doesn't fit, no runner)."""


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
    ) -> None:
        self.budget = budget
        self.unified = unified
        self.tier = tier
        self.resolve = resolve
        self.runner_for = runner_for
        self.events = EventHub()
        self.swaps = 0
        self._residents: dict[str, Resident] = {}  # by role
        self._crashed: set[str] = set()
        self._queue: list[tuple[int, int]] = []  # (priority, ticket), served in sorted order
        self._tickets = itertools.count()
        self._cond = asyncio.Condition()

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

    def _emit(self, event: str, **data: object) -> None:
        self.events.publish({"type": "runtime", "event": event, "swaps": self.swaps, **data})

    # --- leasing -----------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def lease(self, role: str, priority: Priority = Priority.JOB) -> AsyncIterator[Lease]:
        resident = await self._acquire(role, priority)
        client = resident.handle.client()
        try:
            yield Lease(role, resident.choice.model_id, client, resident.plan)
        finally:
            await client.aclose()
            async with self._cond:
                resident.leases -= 1
                resident.last_used = time.monotonic()
                self._emit("release", role=role, model_id=resident.choice.model_id)
                self._cond.notify_all()

    async def _acquire(self, role: str, priority: Priority) -> Resident:
        ticket = (int(priority), next(self._tickets))
        async with self._cond:
            self._queue.append(ticket)
            self._queue.sort()
            try:
                waited = False
                while True:
                    if self._queue[0] == ticket:
                        resident = await self._try_admit(role, chat=priority is Priority.CHAT)
                        if resident is not None:
                            resident.leases += 1
                            self._emit("lease", role=role, model_id=resident.choice.model_id)
                            return resident
                    if not waited:
                        self._emit("wait", role=role, priority=priority.name.lower())
                        waited = True
                    await self._cond.wait()
            finally:
                self._queue.remove(ticket)
                self._cond.notify_all()

    async def _try_admit(self, role: str, *, chat: bool) -> Resident | None:
        """With the lock held. The role's resident, or a newly loaded one, or None to wait."""
        current = self._residents.get(role)
        if current is not None:
            if current.handle.is_running() and role not in self._crashed:
                return current
            self._emit("crash", role=role, model_id=current.choice.model_id)
            self._residents.pop(role)
            self._crashed.discard(role)
            await current.handle.stop()

        choice = self.resolve(role)
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
            if plan.coresident or not self._residents:
                return await self._load(role, choice, plan)
            victim = self._least_recently_used_idle()
            if victim is None:
                return None  # everything loaded is in use: wait for a release
            await self._unload(victim, reason="make room")
            self.swaps += 1

    def _least_recently_used_idle(self) -> str | None:
        idle = [(r.last_used, role) for role, r in self._residents.items() if r.leases == 0]
        return min(idle)[1] if idle else None

    async def _load(self, role: str, choice: ModelChoice, plan: planner.FitPlan) -> Resident:
        self._emit("loading", role=role, model_id=choice.model_id, bytes=plan.est_bytes)
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
        resident = Resident(choice=choice, plan=plan, handle=handle, bytes=used)
        self._residents[role] = resident
        self._emit("loaded", role=role, model_id=choice.model_id, bytes=used, ctx=plan.ctx)
        return resident

    async def _unload(self, role: str, *, reason: str) -> None:
        resident = self._residents.pop(role)
        self._crashed.discard(role)
        await resident.handle.stop()
        self._emit("unloaded", role=role, model_id=resident.choice.model_id, reason=reason)

    async def unload_idle(self, role: str | None = None) -> list[str]:
        """Unload idle models (all, or one role). Used to measure a cold first reply."""
        async with self._cond:
            roles = [r for r, res in self._residents.items() if res.leases == 0]
            if role is not None:
                roles = [r for r in roles if r == role]
            for r in roles:
                await self._unload(r, reason="requested")
            self._cond.notify_all()
            return roles

    async def stop_all(self) -> None:
        async with self._cond:
            for role in list(self._residents):
                await self._unload(role, reason="shutdown")
