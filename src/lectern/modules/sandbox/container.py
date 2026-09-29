"""Runs a script in a locked-down container, through the Podman or Docker command line.

A run has no network, a read-only root filesystem, no capabilities, an unprivileged user,
limits on processes, memory and CPU, and a time limit. It sees only its script, the input
files it was given (read-only, in /in) and an empty /out. Nothing else from the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from lectern.hardware import sandbox as engines

IMAGE = "lectern-sandbox:1"
IMAGE_DIR = Path(__file__).parent / "image"
RUN_TIMEOUT = 60.0
BUILD_TIMEOUT = 15 * 60.0
STREAM_CAP = 1024 * 1024  # bytes kept of stdout and of stderr
OUT_FILES_MAX = 20
OUT_BYTES_MAX = 50 * 1024 * 1024

# The limits every run gets. Kept in one place so a test can check them as a whole.
LOCKDOWN = [
    "--network", "none",
    "--read-only",
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges",
    "--user", "65534:65534",
    "--pids-limit", "128",
    "--memory", "1g",
    "--cpus", "2",
    "--tmpfs", "/tmp",  # noqa: S108 — the container's own /tmp, in memory, not the host's
]  # fmt: skip


class SandboxError(Exception):
    """The container engine is missing, or it failed (not the script: that's a RunResult)."""


@dataclass
class RunResult:
    exit_code: int | None  # None when it was killed for taking too long
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False
    truncated: bool = False  # stdout or stderr was longer than STREAM_CAP
    outputs: list[Path] = field(default_factory=list)  # files in /out, within the caps
    dropped: list[str] = field(default_factory=list)  # files in /out over the caps

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


Exec = Callable[..., Awaitable[asyncio.subprocess.Process]]


class Sandbox:
    def __init__(
        self,
        *,
        image: str = IMAGE,
        detect: Callable[[], engines.SandboxRuntime] = engines.detect,
        which: Callable[[str], str | None] = shutil.which,
        exec_: Exec = asyncio.create_subprocess_exec,
    ) -> None:
        self.image = image
        self._detect = detect
        self._which = which
        self._exec = exec_

    async def engine(self) -> str:
        """The running engine's command-line binary. Raises SandboxError when none runs."""
        found = await run_in_threadpool(self._detect)
        if not found.engine_running or found.runtime is None:
            raise SandboxError("Neither Podman nor Docker is running.")
        return self._which(found.runtime) or found.runtime

    async def image_present(self) -> bool:
        engine = await self.engine()
        process = await self._exec(
            engine, "image", "inspect", self.image,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        return await process.wait() == 0

    async def build(self, on_line: Callable[[str], None] = lambda _line: None) -> None:
        """Build the image from the pinned Dockerfile, reporting each line of output."""
        engine = await self.engine()
        process = await self._exec(
            engine, "build", "-t", self.image, str(IMAGE_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        last: list[str] = []

        async def follow() -> None:
            assert process.stdout is not None  # noqa: S101 — PIPE was asked for
            async for raw in process.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    last[:] = [*last[-19:], line]
                    on_line(line)

        try:
            await asyncio.wait_for(asyncio.gather(follow(), process.wait()), BUILD_TIMEOUT)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise SandboxError("Building the sandbox took too long and was stopped.") from None
        if process.returncode != 0:
            raise SandboxError("Building the sandbox failed:\n" + "\n".join(last[-5:]))

    def run_argv(
        self, engine: str, name: str, script: Path, inputs: dict[str, Path], out_dir: Path
    ) -> list[str]:
        argv = [engine, "run", "--rm", "--name", name, *LOCKDOWN]
        argv += ["--mount", _bind(script, "/job/main.py", readonly=True)]
        for input_name, path in inputs.items():
            argv += ["--mount", _bind(path, f"/in/{input_name}", readonly=True)]
        argv += ["--mount", _bind(out_dir, "/out", readonly=False)]
        argv += [self.image, "python", "/job/main.py"]
        return argv

    async def run(
        self,
        script: Path,
        inputs: dict[str, Path],
        job_dir: Path,
        *,
        timeout: float = RUN_TIMEOUT,
    ) -> RunResult:
        """Run `script` with `inputs` (name in /in → file) and collect what it writes to
        /out. `job_dir` is a fresh folder this run may use; the caller removes it."""
        engine = await self.engine()
        out_dir = job_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        # The container's user (65534) must be able to write to /out, so the folder is open to
        # all; its parent is ours alone, so no other account on this machine can reach it.
        # (The engine resolves the mount itself; the container never walks the parent.)
        job_dir.chmod(0o700)
        out_dir.chmod(0o777)
        name = f"lectern-job-{uuid.uuid4().hex[:12]}"
        argv = self.run_argv(engine, name, script, inputs, out_dir)
        loop = asyncio.get_running_loop()
        started = loop.time()
        process = await self._exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        streams = asyncio.gather(_read(process.stdout), _read(process.stderr))
        timed_out = False
        try:
            (stdout, cut_out), (stderr, cut_err) = await asyncio.wait_for(
                asyncio.shield(streams), timeout
            )
            await process.wait()
        except TimeoutError:
            timed_out = True
            killer = await self._exec(
                engine, "kill", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            await killer.wait()
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            (stdout, cut_out), (stderr, cut_err) = await streams
            await process.wait()
        outputs, dropped = _collect(out_dir)
        return RunResult(
            exit_code=None if timed_out else process.returncode,
            stdout=stdout,
            stderr=stderr + (f"\n[Stopped after {timeout:.0f} s.]" if timed_out else ""),
            seconds=round(loop.time() - started, 2),
            timed_out=timed_out,
            truncated=cut_out or cut_err,
            outputs=outputs,
            dropped=dropped,
        )


def _bind(source: Path, target: str, *, readonly: bool) -> str:
    """A --mount value. Fields are quoted CSV-style, so paths with commas or quotes (or a
    Windows drive's colon) survive; -v would split them."""

    def quoted(field_: str) -> str:
        return (
            f'"{field_.replace(chr(34), chr(34) * 2)}"'
            if any(c in field_ for c in ',"')
            else field_
        )

    parts = ["type=bind", quoted(f"source={source}"), quoted(f"target={target}")]
    if readonly:
        parts.append("readonly")
    return ",".join(parts)


async def _read(stream: asyncio.StreamReader | None) -> tuple[str, bool]:
    """The stream's text, up to STREAM_CAP bytes; the rest is read and thrown away so the
    process never blocks on a full pipe."""
    if stream is None:
        return "", False
    kept = bytearray()
    cut = False
    while chunk := await stream.read(65536):
        room = STREAM_CAP - len(kept)
        if room > 0:
            kept += chunk[:room]
        if len(chunk) > room:
            cut = True
    return kept.decode(errors="replace"), cut


def _collect(out_dir: Path) -> tuple[list[Path], list[str]]:
    """Regular files the script wrote to /out (no links), within the count and size caps."""
    kept: list[Path] = []
    dropped: list[str] = []
    total = 0
    for path in sorted(out_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        if len(kept) >= OUT_FILES_MAX or total + size > OUT_BYTES_MAX:
            dropped.append(path.relative_to(out_dir).as_posix())
            continue
        kept.append(path)
        total += size
    return kept, dropped
