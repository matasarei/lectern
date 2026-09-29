from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from lectern.hardware.sandbox import SandboxRuntime
from lectern.modules.sandbox import container
from lectern.modules.sandbox.container import LOCKDOWN, Sandbox, SandboxError, _bind, _collect

RUNNING = SandboxRuntime(runtime="docker", version="29", engine_running=True, found=("docker",))


class FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", code: int = 0, hang=False):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self._code = code
        self._done = asyncio.Event()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        if not hang:
            self.finish()

    def finish(self, code: int | None = None) -> None:
        if not self.stdout.at_eof():
            self.stdout.feed_eof()
            self.stderr.feed_eof()
        self.returncode = self._code if code is None else code
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode  # type: ignore[return-value]

    def kill(self) -> None:
        self.finish(-9)


class FakeEngine:
    """Records every command. `run` answers with a process made by `make_run`, inside the
    event loop (asyncio streams need one)."""

    def __init__(self, make_run=None, image_code: int = 0):  # noqa: ANN001
        self.calls: list[tuple[str, ...]] = []
        self.make_run = make_run
        self.run_process: FakeProcess | None = None
        self.image_code = image_code

    async def __call__(self, *argv, **_kwargs):
        self.calls.append(argv)
        if argv[1] == "run":
            self.run_process = self.make_run()
            return self.run_process
        if argv[1] == "kill":
            self.run_process.kill()
            return FakeProcess()
        if argv[1:3] == ("image", "inspect"):
            return FakeProcess(code=self.image_code)
        if argv[1] == "build":
            return FakeProcess(stdout=b"Step 1/3 : FROM python:3.12-slim\nSuccessfully built\n")
        raise AssertionError(argv)


def sandbox_with(engine: FakeEngine, runtime: SandboxRuntime = RUNNING) -> Sandbox:
    return Sandbox(detect=lambda: runtime, which=lambda name: f"/usr/bin/{name}", exec_=engine)


def job(tmp_path: Path) -> tuple[Path, dict[str, Path], Path]:
    script = tmp_path / "main.py"
    script.write_text("print(1)")
    data = tmp_path / "rent,1998.csv"
    data.write_text("a\n1\n")
    return script, {"rent.csv": data}, tmp_path / "job"


def test_a_run_is_locked_down_and_mounts_only_what_it_was_given(tmp_path):
    engine = FakeEngine(lambda: FakeProcess(stdout=b"42\n"))
    script, inputs, job_dir = job(tmp_path)

    result = asyncio.run(sandbox_with(engine).run(script, inputs, job_dir))

    argv = engine.calls[0]
    assert argv[:3] == ("/usr/bin/docker", "run", "--rm")
    assert argv[3] == "--name" and argv[4].startswith("lectern-job-")
    assert list(argv[5 : 5 + len(LOCKDOWN)]) == LOCKDOWN
    assert {"--network", "none"} <= set(argv) and "--read-only" in argv
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert mounts == [
        f"type=bind,source={script},target=/job/main.py,readonly",
        f'type=bind,"source={inputs["rent.csv"]}",target=/in/rent.csv,readonly',
        f"type=bind,source={job_dir / 'out'},target=/out",
    ]
    assert argv[-3:] == ("lectern-sandbox:1", "python", "/job/main.py")
    assert (result.ok, result.stdout, result.exit_code) == (True, "42\n", 0)
    assert oct((job_dir / "out").stat().st_mode & 0o777) == "0o777"


def test_a_run_that_takes_too_long_is_killed(tmp_path):
    engine = FakeEngine(lambda: FakeProcess(stdout=b"partial\n", hang=True))
    script, inputs, job_dir = job(tmp_path)

    result = asyncio.run(sandbox_with(engine).run(script, inputs, job_dir, timeout=0.05))

    assert engine.calls[1][1:] == ("kill", engine.calls[0][4])
    assert result.timed_out and result.exit_code is None and not result.ok
    assert result.stdout == "partial\n"
    assert "Stopped after 0 s" in result.stderr


def test_output_beyond_the_cap_is_cut_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(container, "STREAM_CAP", 10)
    engine = FakeEngine(lambda: FakeProcess(stdout=b"x" * 25, stderr=b"err"))
    script, inputs, job_dir = job(tmp_path)
    result = asyncio.run(sandbox_with(engine).run(script, inputs, job_dir))
    assert (result.stdout, result.stderr, result.truncated) == ("x" * 10, "err", True)


def test_only_regular_files_within_the_caps_become_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(container, "OUT_FILES_MAX", 2)
    out = tmp_path / "out"
    (out / "sub").mkdir(parents=True)
    for name in ("a.csv", "b.csv", "sub/c.csv"):
        (out / name).write_text(name)
    os.symlink("/etc/passwd", out / "link")

    kept, dropped = _collect(out)

    assert [p.relative_to(out).as_posix() for p in kept] == ["a.csv", "b.csv"]
    assert dropped == ["sub/c.csv"]


def test_mount_values_quote_awkward_paths():
    assert _bind(Path("/p/a,b"), "/in/x", readonly=True) == (
        'type=bind,"source=/p/a,b",target=/in/x,readonly'
    )
    assert _bind(Path('/p/say "hi"'), "/out", readonly=False) == (
        'type=bind,"source=/p/say ""hi""",target=/out'
    )


def test_no_engine_is_an_error_not_a_crash(tmp_path):
    stopped = SandboxRuntime(
        runtime="docker", version=None, engine_running=False, found=("docker",)
    )
    script, inputs, job_dir = job(tmp_path)
    with pytest.raises(SandboxError, match="Neither Podman nor Docker is running"):
        asyncio.run(sandbox_with(FakeEngine(), stopped).run(script, inputs, job_dir))


def test_image_check_and_build(tmp_path):
    missing = FakeEngine(image_code=1)
    lines: list[str] = []
    box = sandbox_with(missing)

    assert asyncio.run(box.image_present()) is False
    asyncio.run(box.build(lines.append))

    assert missing.calls[1][1:4] == ("build", "-t", "lectern-sandbox:1")
    assert Path(missing.calls[1][4]) == container.IMAGE_DIR
    assert (container.IMAGE_DIR / "Dockerfile").is_file()
    assert lines == ["Step 1/3 : FROM python:3.12-slim", "Successfully built"]


# --- a real engine --------------------------------------------------------------------------

REAL_IMAGE = os.environ.get("LECTERN_TEST_SANDBOX_IMAGE")  # e.g. python:3-slim


@pytest.mark.sandbox
@pytest.mark.skipif(not REAL_IMAGE, reason="set LECTERN_TEST_SANDBOX_IMAGE to a Python image")
def test_a_real_container_has_no_network_a_read_only_root_and_an_unprivileged_user(tmp_path):
    script = tmp_path / "main.py"
    script.write_text(
        "import os, urllib.request\n"
        "print('uid', os.getuid())\n"
        "try:\n"
        "    urllib.request.urlopen('http://example.com', timeout=3); print('network open')\n"
        "except Exception: print('network blocked')\n"
        "for target in ('/job/x', '/in/data.csv', '/usr/x'):\n"
        "    try:\n"
        "        open(target, 'w'); print('wrote', target)\n"
        "    except OSError: print('refused', target)\n"
        "print('input', open('/in/data.csv').read().strip())\n"
        "open('/out/result.txt', 'w').write('done')\n"
    )
    data = tmp_path / "data.csv"
    data.write_text("a,b\n1,2")
    box = Sandbox(image=REAL_IMAGE)

    result = asyncio.run(box.run(script, {"data.csv": data}, tmp_path / "job", timeout=60))

    assert result.ok, result.stderr
    lines = result.stdout.splitlines()
    assert "uid 65534" in lines and "network blocked" in lines
    assert {"refused /job/x", "refused /in/data.csv", "refused /usr/x"} <= set(lines)
    assert "input a,b" in result.stdout
    assert [p.name for p in result.outputs] == ["result.txt"]
    assert data.read_text() == "a,b\n1,2"  # the input is untouched


@pytest.mark.sandbox
@pytest.mark.skipif(not REAL_IMAGE, reason="set LECTERN_TEST_SANDBOX_IMAGE to a Python image")
def test_a_real_run_is_killed_after_its_time_limit(tmp_path):
    script = tmp_path / "main.py"
    script.write_text("import time\nprint('start', flush=True)\ntime.sleep(30)\n")
    box = Sandbox(image=REAL_IMAGE)
    result = asyncio.run(box.run(script, {}, tmp_path / "job", timeout=3))
    assert result.timed_out and "start" in result.stdout
    engine = shutil.which("docker") or shutil.which("podman")
    assert engine is not None
