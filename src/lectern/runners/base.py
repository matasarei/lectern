"""What every runner provides: start a model as its own process, answer on an
OpenAI-compatible HTTP API on 127.0.0.1, report the memory it uses, stop cleanly.

One process per model: stopping the process is what gives the memory back.
"""

from __future__ import annotations

import hashlib
import re
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


def run_dir_name(model_id: str) -> str:
    """The folder a runner keeps its key and log in: the model id in lower case, with anything
    but letters, digits, dots, dashes and underscores replaced, so the path never needs quoting.
    A hash of the original is added when anything changed, so two ids can't share a folder,
    not even on macOS and Windows disks, where "Qwen" and "qwen" are the same name."""
    safe = re.sub(r"[^a-z0-9._-]+", "-", model_id.lower()).strip("-.")
    if safe == model_id:
        return safe
    return f"{safe or 'model'}-{hashlib.sha256(model_id.encode()).hexdigest()[:8]}"


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
