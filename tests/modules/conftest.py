from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lectern.agent.tools import ToolContext
from lectern.core.auth import LOCAL_USER
from lectern.downloads.manager import DownloadState, EventHub
from lectern.modules.sandbox.container import RunResult
from lectern.projects import files


class FakeDownloads:
    def __init__(self, downloaded: bool = True, fail: bool = False, end: str | None = None):
        self.downloaded = downloaded
        self.fail = fail
        self.end = end or ("failed" if fail else "done")
        self.events = EventHub()
        self.started: list[str] = []

    def is_downloaded(self, entry) -> bool:  # noqa: ANN001
        return self.downloaded

    def start(self, entry) -> DownloadState:  # noqa: ANN001
        self.started.append(entry.id)
        loop = asyncio.get_running_loop()
        half = DownloadState(entry.id, "downloading", entry.size // 2, entry.size)
        done = entry.size // 2 if self.end == "paused" else entry.size
        error = "disk full" if self.fail else None
        end = DownloadState(entry.id, self.end, done, entry.size, error=error)
        loop.call_soon(self.events.publish, {"type": "download", **half.to_dict()})
        loop.call_soon(self.events.publish, {"type": "download", **end.to_dict()})
        return DownloadState(entry.id, "queued", 0, entry.size)


class FakeSandbox:
    """Stands in for the container engine. `script(code, inputs)` decides what a run prints:
    by default it echoes the input names. Records every run."""

    def __init__(self, *, image: bool = True, behave=None):  # noqa: ANN001
        self.image = image
        self.builds = 0
        self.runs: list[dict] = []
        self.behave = behave or (lambda code, inputs, out: RunResult(0, "ok\n", "", 0.1))

    async def image_present(self) -> bool:
        return self.image

    async def build(self, on_line) -> None:  # noqa: ANN001
        on_line("Step 1/3 : FROM python:3.12-slim")
        self.builds += 1
        self.image = True

    async def run(self, script: Path, inputs: dict[str, Path], job_dir: Path, **_kw) -> RunResult:
        await asyncio.sleep(0)
        out = job_dir / "out"
        out.mkdir(parents=True, exist_ok=True)
        code = script.read_text()
        self.runs.append({"code": code, "inputs": {k: v.read_text() for k, v in inputs.items()}})
        result = self.behave(code, inputs, out)
        result.outputs = sorted(p for p in out.iterdir() if p.is_file())
        return result


@pytest.fixture
def make_ctx(store):
    def make(sandbox=None, answers=(), **kwargs):  # noqa: ANN001
        project = store.create(LOCAL_USER, "Numbers")
        asked: list[str] = []
        events: list[dict] = []
        replies = list(answers)

        async def confirm(question: str) -> bool:
            asked.append(question)
            return replies.pop(0) if replies else False

        ctx = ToolContext(
            project=project, sessions=store.db(project), user=LOCAL_USER, session_id=1,
            confirm=confirm, emit=events.append, sandbox=sandbox, **kwargs,
        )  # fmt: skip
        ctx.asked, ctx.events = asked, events  # type: ignore[attr-defined]
        return ctx

    return make


def add(ctx: ToolContext, name: str, text: str, folder: str = "") -> int:
    with ctx.sessions.begin() as session:
        return files.write_text(
            ctx.project.root, session, owner_id=ctx.user.id, name=name, text=text,
            origin=files.Origin("import"), folder=folder,
        ).id  # fmt: skip
