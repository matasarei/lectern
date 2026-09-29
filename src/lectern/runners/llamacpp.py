"""llama-server as a runner: one process per model on 127.0.0.1, protected by a key file.

The key is written to a file (mode 0600) and passed with --api-key-file: a key on the command
line would be visible to other users in `ps`. Other websites and local programs can reach a
localhost port, so the key is what keeps them out.

The log runs at verbosity 4, which adds the memory lines this runner reads back: llama.cpp's
own projection ("projected to use N MiB of device memory") and the real buffer sizes per
device. At the default level they aren't printed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import socket
import sys
from pathlib import Path

import httpx2

from lectern.runners.base import MeasuredMemory, RunnerError, RunnerSpec

MIB = 1024**2
READY_TIMEOUT = 120.0
STOP_GRACE = 10.0
LOG_VERBOSITY = "4"
PORT_ATTEMPTS = 3
BIND_FAILED = "couldn't bind HTTP server socket"  # llama-server b11236, server.cpp

_PROJECTED = re.compile(r"projected to use (\d+) MiB of device memory")
_BUFFER = re.compile(
    r"\s(?P<device>[A-Za-z]+\d*)(?:_Mapped)?\s+(?:model|KV|RS|compute|output) buffer size =\s+"
    r"(?P<mib>[\d.]+) MiB"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_args(spec: RunnerSpec, *, port: int, key_file: Path, slot_dir: Path) -> list[str]:
    args = [
        "-m", str(spec.model_file),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--api-key-file", str(key_file),
        "-c", str(spec.ctx),
        "-ctk", spec.kv_type,
        "-ctv", spec.kv_type,
        "--fit", "on",
        "--fit-target", str(spec.fit_target_mib),
        "--jinja",
        "--slot-save-path", str(slot_dir),
        "-lv", LOG_VERBOSITY,
    ]  # fmt: skip
    if spec.mmproj is not None:
        args += ["--mmproj", str(spec.mmproj)]
    return [*args, *spec.extra_args]


def parse_memory(log: str) -> MeasuredMemory:
    """Device memory from the startup log: the sum of every non-CPU buffer, and the fit
    projection. Either can be missing (older builds, CPU-only runs)."""
    projected = _PROJECTED.search(log)
    device_mib = 0.0
    found = False
    for match in _BUFFER.finditer(log):
        if not match["device"].upper().startswith("CPU"):
            device_mib += float(match["mib"])
            found = True
    return MeasuredMemory(
        device_bytes=int(device_mib * MIB) if found else None,
        projected_bytes=int(projected[1]) * MIB if projected else None,
    )


def _write_key(path: Path) -> str:
    key = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


class LlamaCppHandle:
    def __init__(
        self, *, model_id: str, process: asyncio.subprocess.Process, port: int, key: str,
        log_file: Path,
    ) -> None:  # fmt: skip
        self.model_id = model_id
        self.process = process
        self.pid = process.pid
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._key = key
        self.log_file = log_file

    def client(self, timeout: float = 600.0) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self._key}"} if self._key else {},
            timeout=httpx2.Timeout(timeout, connect=5.0),
        )

    def is_running(self) -> bool:
        return self.process.returncode is None

    def log_tail(self, lines: int = 20) -> str:
        try:
            return "\n".join(self.log_file.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def measured_memory(self) -> MeasuredMemory:
        try:
            return parse_memory(self.log_file.read_text(errors="replace"))
        except OSError:
            return MeasuredMemory(device_bytes=None)

    async def stop(self) -> None:
        if self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), STOP_GRACE)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()


class LlamaCppRunner:
    def __init__(self, server: Path, work_dir: Path, *, ready_timeout: float = READY_TIMEOUT):
        self.server = server
        self.work_dir = work_dir
        self.ready_timeout = ready_timeout

    async def start(self, spec: RunnerSpec) -> LlamaCppHandle:
        run_dir = self.work_dir / spec.model_id
        run_dir.mkdir(parents=True, exist_ok=True)
        key_file = run_dir / "key"
        key = _write_key(key_file)
        slot_dir = run_dir / "slots"
        slot_dir.mkdir(exist_ok=True)
        log_file = run_dir / "server.log"

        env = dict(os.environ)
        if sys.platform.startswith("linux"):
            # The CUDA runtime libraries were unpacked next to llama-server.
            env["LD_LIBRARY_PATH"] = os.pathsep.join(
                p for p in (str(self.server.parent), env.get("LD_LIBRARY_PATH")) if p
            )
        # free_port() can't reserve the port: another program may take it before llama-server
        # binds. llama-server binds before loading the model, so that failure is quick and
        # recognisable; try again on a new port.
        for attempt in range(1, PORT_ATTEMPTS + 1):
            port = free_port()
            with log_file.open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    str(self.server),
                    *build_args(spec, port=port, key_file=key_file, slot_dir=slot_dir),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
            handle = LlamaCppHandle(
                model_id=spec.model_id, process=process, port=port, key=key, log_file=log_file
            )
            try:
                await self._wait_ready(handle)
            except RunnerError:
                await handle.stop()
                if attempt < PORT_ATTEMPTS and BIND_FAILED in handle.log_tail():
                    continue
                raise
            except BaseException:
                await handle.stop()
                raise
            return handle
        raise AssertionError("unreachable")  # the last attempt returns or raises

    async def _wait_ready(self, handle: LlamaCppHandle) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ready_timeout
        async with httpx2.AsyncClient(base_url=handle.base_url, timeout=2.0) as client:
            while loop.time() < deadline:
                if not handle.is_running():
                    raise RunnerError(
                        f"llama-server for {handle.model_id} exited with code "
                        f"{handle.process.returncode}.\n{handle.log_tail()}"
                    )
                with contextlib.suppress(httpx2.HTTPError):
                    if (await client.get("/health")).status_code == 200:
                        return
                await asyncio.sleep(0.25)
        raise RunnerError(
            f"llama-server for {handle.model_id} wasn't ready after {self.ready_timeout:.0f} s."
            f"\n{handle.log_tail()}"
        )


class InstallingLlamaCppRunner:
    """A LlamaCppRunner that installs its llama.cpp build on first use."""

    def __init__(self, installer, variant: str, work_dir: Path) -> None:  # noqa: ANN001
        self.installer = installer
        self.variant = variant
        self.work_dir = work_dir

    async def start(self, spec: RunnerSpec) -> LlamaCppHandle:
        server = await self.installer.install(self.variant)
        return await LlamaCppRunner(server, self.work_dir).start(spec)
