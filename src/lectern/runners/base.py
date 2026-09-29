"""What every runner provides: start a model as its own process, answer on an
OpenAI-compatible HTTP API on 127.0.0.1, report the memory it uses, stop cleanly.

One process per model: stopping the process is what gives the memory back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx2


@dataclass(frozen=True)
class RunnerSpec:
    model_id: str
    model_file: Path  # the first part, for split GGUFs
    ctx: int
    kv_type: str  # "f16" or "q8_0"
    fit_target_mib: int  # device memory llama.cpp must leave free
    mmproj: Path | None = None
    extra_args: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MeasuredMemory:
    device_bytes: int | None  # what the model actually holds on the GPU, when known
    projected_bytes: int | None = None  # what the runner projected before loading


class RunnerError(Exception):
    pass


class RunnerHandle(Protocol):
    model_id: str
    base_url: str
    pid: int

    def client(self) -> httpx2.AsyncClient: ...
    def is_running(self) -> bool: ...
    def measured_memory(self) -> MeasuredMemory: ...
    async def stop(self) -> None: ...


class Runner(Protocol):
    async def start(self, spec: RunnerSpec) -> RunnerHandle: ...
