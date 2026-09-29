"""The Sandbox module's agent tools: code.run (and code.write, which builds on it).

Scripts live in the project's `scripts/` folder. Their inputs are project files, mounted
read-only; what a script writes to /out becomes project files in `scripts/out/`, From = the
script. A run with the same script and inputs as a stored run returns that run.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult, page
from lectern.modules.sandbox.container import RunResult, Sandbox, SandboxError
from lectern.projects import files
from lectern.projects.tables import ArtifactRow, ScriptRunRow

SCRIPTS = "scripts"
OUTPUTS = "scripts/out"
ERROR_TAIL = 2000  # characters of stderr shown to the model
BUILD_QUESTION = (
    "Build the script sandbox? It downloads about 250 MB once (Python and a few libraries)."
)
DECLINED_BUILD = (
    "The person didn't allow building the script sandbox, so scripts can't run. Show the "
    "formula and say the result is not computed."
)


@dataclass(frozen=True)
class Input:
    name: str  # as the script sees it: /in/<name>
    path: Path
    sha256: str
    artifact_id: int


# --- finding scripts and inputs --------------------------------------------------------------


def _live(ctx: ToolContext) -> list[ArtifactRow]:
    with ctx.sessions() as session:
        return [r for r in files.live(session) if r.owner_id == ctx.user.id]


def find_script(ctx: ToolContext, name: str) -> ArtifactRow:
    """A script in scripts/, by its name with or without the folder and the .py."""
    stem = name.removeprefix(f"{SCRIPTS}/").removesuffix(".py")
    wanted = f"{SCRIPTS}/{stem}.py".casefold()
    for row in _live(ctx):
        if row.path.casefold() == wanted:
            return row
    known = ", ".join(r.path for r in _live(ctx) if r.path.startswith(f"{SCRIPTS}/")) or "none"
    raise ToolError(f"No script {name!r} in {SCRIPTS}/. Scripts: {known}.")


def find_inputs(ctx: ToolContext, names: list[str]) -> list[Input]:
    """Project files by exact name (case aside). Each is mounted as /in/<its file name>."""
    rows = _live(ctx)
    found: list[Input] = []
    for name in names:
        matches = [r for r in rows if r.path.casefold() == name.casefold()]
        if len(matches) != 1:
            raise ToolError(f"No file called exactly {name!r}. Use project.find for its name.")
        row = matches[0]
        found.append(
            Input(Path(row.path).name, files.inside(ctx.project.root, row.path), row.sha256, row.id)
        )
    seen = [i.name for i in found]
    if len(seen) != len(set(seen)):
        raise ToolError("Two inputs have the same file name; the script couldn't tell them apart.")
    return found


def inputs_sha(inputs: list[Input]) -> str:
    listing = "\n".join(sorted(f"{i.name}:{i.sha256}" for i in inputs))
    return hashlib.sha256(listing.encode()).hexdigest()


# --- the sandbox itself ----------------------------------------------------------------------


def sandbox_of(ctx: ToolContext) -> Sandbox:
    if ctx.sandbox is None:
        raise ToolError("The script sandbox isn't available here. Say the result is not computed.")
    return ctx.sandbox


async def ensure_image(ctx: ToolContext) -> Sandbox:
    """The sandbox, with its image built: asks with a card the first time."""
    box = sandbox_of(ctx)
    try:
        if await box.image_present():
            return box
        if not await ctx.confirm(BUILD_QUESTION):
            raise ToolError(DECLINED_BUILD)
        ctx.emit({"type": "progress", "label": "Building the script sandbox", "line": ""})
        await box.build(
            lambda line: ctx.emit(
                {"type": "progress", "label": "Building the script sandbox", "line": line[-200:]}
            )
        )
    except SandboxError as exc:
        raise ToolError(f"{exc} Say the result is not computed.") from None
    return box


async def run_script(
    ctx: ToolContext, script: ArtifactRow, inputs: list[Input], *, test_run: bool
) -> tuple[ScriptRunRow, RunResult]:
    """Run a stored script, keep the run, and turn its /out files into project files."""
    box = await ensure_image(ctx)
    job_dir = ctx.project.root / files.DATA_DIR / "jobs" / uuid.uuid4().hex[:12]
    try:
        result = await box.run(
            files.inside(ctx.project.root, script.path),
            {i.name: i.path for i in inputs},
            job_dir,
        )
        run = await run_in_threadpool(_store_run, ctx, script, inputs, result, test_run)
    except SandboxError as exc:
        raise ToolError(f"{exc} Say the result is not computed.") from None
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
    return run, result


def _store_run(
    ctx: ToolContext, script: ArtifactRow, inputs: list[Input], result: RunResult, test_run: bool
) -> ScriptRunRow:
    with ctx.sessions.begin() as session:
        outputs = []
        for path in result.outputs:
            row = files.write_bytes(
                ctx.project.root,
                session,
                owner_id=ctx.user.id,
                name=path.name,
                data=path.read_bytes(),
                origin=files.Origin("artifact", id=script.id),
                folder=OUTPUTS,
            )
            outputs.append(row.id)
        run = ScriptRunRow(
            script_id=script.id,
            script_sha=script.sha256,
            inputs_sha=inputs_sha(inputs),
            test_run=test_run,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            stdout=result.stdout,
            stderr=result.stderr,
            outputs=json.dumps(outputs),
            seconds=result.seconds,
        )
        session.add(run)
        session.flush()
        return run


def stored_run(ctx: ToolContext, script: ArtifactRow, inputs: list[Input]) -> ScriptRunRow | None:
    """The latest successful run of this exact script on these exact inputs."""
    with ctx.sessions() as session:
        return session.scalars(
            select(ScriptRunRow)
            .where(
                ScriptRunRow.script_id == script.id,
                ScriptRunRow.script_sha == script.sha256,
                ScriptRunRow.inputs_sha == inputs_sha(inputs),
                ScriptRunRow.exit_code == 0,
            )
            .order_by(ScriptRunRow.id.desc())
        ).first()


def describe_run(
    ctx: ToolContext, script: ArtifactRow, run: ScriptRunRow, *, cursor: str | None,
    max_tokens: int, cached: bool,
) -> ToolResult:  # fmt: skip
    """What the model gets back: the outcome, stdout in pages, the files written, errors."""
    with ctx.sessions() as session:
        outputs = [session.get(ArtifactRow, i) for i in json.loads(run.outputs)]
    outputs = [o for o in outputs if o is not None and o.trashed_at is None]
    status = "stopped: took too long" if run.timed_out else f"exit {run.exit_code}"
    head = f"Ran {script.path} ({status}, {run.seconds:g} s)" + (
        " — the stored result of an identical earlier run." if cached else "."
    )
    body, next_cursor = page(run.stdout or "(no output)", cursor, max_tokens)
    text = f"{head}\nOutput:\n{body}"
    if outputs:
        text += "\nFiles written: " + ", ".join(o.path for o in outputs)
    if run.exit_code != 0 and run.stderr:
        text += f"\nErrors:\n{run.stderr[-ERROR_TAIL:]}"
    refs = [{"kind": "artifact", "id": script.id, "name": script.path, "ref": None}] + [
        {"kind": "artifact", "id": o.id, "name": o.path, "ref": None} for o in outputs
    ]
    return ToolResult(text, refs=refs, cursor=next_cursor)


# --- code.run --------------------------------------------------------------------------------


class RunParams(BaseModel):
    name: str = Field(
        min_length=1, max_length=200, description='The script in scripts/, e.g. "penalty".'
    )
    inputs: list[str] | None = Field(
        default=None,
        max_length=20,
        description="Project files to give it (read-only, in /in). Defaults to the files it "
        "was written for.",
    )
    cursor: str | None = Field(
        default=None, description="From a previous, cut-off result: the next page of output."
    )


async def _code_run(ctx: ToolContext, params: RunParams) -> ToolResult:
    script = await run_in_threadpool(find_script, ctx, params.name)
    names = params.inputs if params.inputs is not None else _manifest_inputs(ctx, script)
    inputs = await run_in_threadpool(find_inputs, ctx, names)
    run = await run_in_threadpool(stored_run, ctx, script, inputs)
    cached = run is not None
    if run is None:
        run, _ = await run_script(ctx, script, inputs, test_run=False)
    return await run_in_threadpool(
        lambda: describe_run(
            ctx, script, run, cursor=params.cursor, max_tokens=CODE_RUN.max_tokens, cached=cached
        )
    )


def _manifest_inputs(ctx: ToolContext, script: ArtifactRow) -> list[str]:
    """The inputs recorded in scripts/<name>.json, if it's there."""
    manifest = files.inside(ctx.project.root, script.path).with_suffix(".json")
    try:
        return [i["path"] for i in json.loads(manifest.read_text())["inputs"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


CODE_RUN = AgentTool(
    name="code.run",
    description=(
        "Run a script from scripts/ in the sandbox (no network) and get its output. Use it "
        "for exact results. Files it writes to /out are saved to scripts/out/."
    ),
    params=RunParams,
    run=_code_run,
    max_tokens=800,
)
