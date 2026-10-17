"""The Sandbox module's agent tools: code.run (and code.write, which builds on it).

Scripts live in the project's `scripts/` folder. Their inputs are project files, mounted
read-only; what a script writes to /out becomes project files in `scripts/out/`, From = the
script. A run with the same script and inputs as a stored run returns that run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from lectern.agent import model_download
from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult, page
from lectern.modules.sandbox.container import IMAGE, RunResult, Sandbox, SandboxError
from lectern.projects import files
from lectern.projects.tables import ArtifactRow, ScriptRunRow
from lectern.runners.base import RunnerError
from lectern.runtime.supervisor import NotAvailable, Priority
from lectern.runtime.wiring import role_model

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


def named_in(ctx: ToolContext, text: str) -> list[str]:
    """The project files `text` names, by path or by file name (case aside), outside
    scripts/. A file name two files share names neither: the brief doesn't say which."""
    lowered = text.casefold()
    rows = [r for r in _live(ctx) if not r.path.startswith(f"{SCRIPTS}/")]

    def said(name: str) -> bool:
        return re.search(rf"(?<![\w./-]){re.escape(name.casefold())}(?![\w-])", lowered) is not None

    by_name: dict[str, list[str]] = {}
    for row in rows:
        by_name.setdefault(Path(row.path).name.casefold(), []).append(row.path)
    return [
        row.path
        for row in rows
        if said(row.path)
        or (said(Path(row.path).name) and len(by_name[Path(row.path).name.casefold()]) == 1)
    ]


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
            script_sha=script_sha(ctx, script),
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


def script_sha(ctx: ToolContext, script: ArtifactRow) -> str:
    """The script as it is on disk now: someone may have edited it since it was written, and
    a stored run of the old text mustn't be returned for the new one."""
    path = files.inside(ctx.project.root, script.path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stored_run(ctx: ToolContext, script: ArtifactRow, inputs: list[Input]) -> ScriptRunRow | None:
    """The latest successful run of this exact script on these exact inputs."""
    with ctx.sessions() as session:
        return session.scalars(
            select(ScriptRunRow)
            .where(
                ScriptRunRow.script_id == script.id,
                ScriptRunRow.script_sha == script_sha(ctx, script),
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


# --- code.write ------------------------------------------------------------------------------

MAX_FIXES = 3
CODER_MAX_TOKENS = 1500
CODER_TEMPERATURE = 0.2
PREVIEW_LINES = 5
PREVIEW_LINE_CHARS = 300
SHEET_SUFFIXES = {".xlsx", ".xlsm"}
# Run in the sandbox before the coder is asked: what pandas, the reader its script will use,
# makes of each spreadsheet in /in. The server itself has no Excel reader.
SHEET_PREVIEW = f"""from pathlib import Path

import pandas as pd

for path in sorted(Path("/in").iterdir()):
    print(f"== {{path.name}}")
    sheets = pd.read_excel(path, sheet_name=None, nrows={PREVIEW_LINES})
    for name, frame in sheets.items():
        columns = ", ".join(str(c) for c in frame.columns)
        # One sheet is just the file's table: a name shown would end up in the output
        # ("Total hours in Week 1"), and the chat would take that for a part of it.
        print(f"sheet {{name!r}}: {{columns}}" if len(sheets) > 1 else f"columns: {{columns}}")
        print(frame.to_string(index=False))
"""

CODER_SYSTEM = """You write one Python 3.12 script for a sandbox. Rules:
- Input files are in /in (read them from there, e.g. /in/rent.csv). There is no network.
- Only the standard library and numpy, pandas, python-dateutil and openpyxl are installed.
- Print the result to standard output, clearly labelled. Write files only to /out.
- Compute everything; never hard-code a result you were asked to compute.
- Reply with the whole script in one ```python block, and nothing else."""


class WriteParams(BaseModel):
    name: str = Field(
        pattern=r"^[a-z0-9][a-z0-9_-]{0,60}$",
        description='A short file name for the script, e.g. "penalty-by-due-day".',
    )
    brief: str = Field(
        min_length=10,
        max_length=4000,
        description="What the script must compute, with every number and date it needs.",
    )
    inputs: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Project files the script reads (they appear in /in).",
    )
    expected_output: str = Field(
        default="", max_length=1000, description="What it should print, e.g. one line per due day."
    )


async def _code_write(ctx: ToolContext, params: WriteParams) -> ToolResult:
    # A call with no inputs whose brief names a project file ("Read hours.xlsx and sum…") is
    # given that file: seen on the 4B, the script found an empty /in and the turn ran out of
    # model calls on fixes that couldn't help.
    names = params.inputs or await run_in_threadpool(named_in, ctx, params.brief)
    inputs = await run_in_threadpool(find_inputs, ctx, names)
    box = await ensure_image(ctx)
    await ensure_coder(ctx)
    job_dir = ctx.project.root / files.DATA_DIR / "jobs" / uuid.uuid4().hex[:12]
    try:
        return await _write_and_test(ctx, box, params, inputs, job_dir)
    except (NotAvailable, RunnerError) as exc:
        # The coding model couldn't be loaded (too big for the memory left, or its server
        # didn't start). The chat carries on, and the model says the result isn't computed.
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise ToolError(
            f"The coding model couldn't start: {reason} Show the formula and say the result "
            "is not computed."
        ) from None
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def _write_and_test(
    ctx: ToolContext, box: Sandbox, params: WriteParams, inputs: list[Input], job_dir: Path
) -> ToolResult:
    sheets = await _sheet_previews(ctx, box, inputs, job_dir)
    request = await run_in_threadpool(coder_request, ctx, params, inputs, sheets)
    messages = [{"role": "system", "content": CODER_SYSTEM}, {"role": "user", "content": request}]
    loop_time = asyncio.get_running_loop().time
    started = loop_time()
    async with ctx.runtime.lease("code", Priority.CHAT) as lease:
        ctx.emit(
            {
                "type": "progress",
                "label": f"Coding model {lease.model_id} ready",
                "seconds": round(loop_time() - started, 1),
            }
        )
        problem = ""
        for attempt in range(MAX_FIXES + 1):
            if problem:
                messages.append({"role": "user", "content": problem})
                ctx.emit(
                    {"type": "progress", "label": f"Fixing the script ({attempt}/{MAX_FIXES})"}
                )
            reply = await _ask_coder(lease.client, messages)
            messages.append({"role": "assistant", "content": reply})
            code = extract_code(reply)
            if code is None:
                problem = "There was no ```python block. Reply with the whole script in one."
                continue
            try:
                compile(code, "main.py", "exec")  # parses only; nothing runs here
            except SyntaxError as exc:
                problem = f"The script doesn't compile: {exc.msg} (line {exc.lineno}). Fix it."
                continue
            attempt_dir = job_dir / f"try-{attempt}"
            attempt_dir.mkdir(parents=True)
            candidate = attempt_dir / "main.py"
            candidate.write_text(code)
            ctx.emit({"type": "progress", "label": "Test run in the sandbox"})
            try:
                result = await box.run(candidate, {i.name: i.path for i in inputs}, attempt_dir)
            except SandboxError as exc:
                raise ToolError(f"{exc} Say the result is not computed.") from None
            if result.ok:
                return await run_in_threadpool(
                    _save_script, ctx, params, inputs, code, result, lease.model_id
                )
            problem = (
                "The test run failed"
                + (" (it took too long)" if result.timed_out else f" with exit {result.exit_code}")
                + f". Error output:\n{result.stderr[-ERROR_TAIL:]}\nFix the script."
            )
    raise ToolError(
        f"Couldn't write a working script after {MAX_FIXES} fixes. Last problem: {problem[:600]} "
        "Show the formula and say the result is not computed."
    )


async def _ask_coder(client, messages: list[dict]) -> str:  # noqa: ANN001
    response = await client.post(
        "/v1/chat/completions",
        json={
            "messages": messages,
            "max_tokens": CODER_MAX_TOKENS,
            "temperature": CODER_TEMPERATURE,
            "stream": False,
        },
    )
    if response.status_code != 200:
        raise ToolError(f"The coding model failed: {response.text[:300]}")
    return response.json()["choices"][0]["message"].get("content") or ""


def extract_code(reply: str) -> str | None:
    """The script from the coder's reply: its fenced Python block (or, failing that, the
    whole reply when it has no fence at all and compiles)."""
    blocks = re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", reply, flags=re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    if "```" in reply:
        return None
    try:
        compile(reply, "main.py", "exec")
    except SyntaxError:
        return None
    return reply.strip() + "\n"


async def _sheet_previews(
    ctx: ToolContext, box: Sandbox, inputs: list[Input], job_dir: Path
) -> dict[str, str]:
    """Each spreadsheet input's sheets, columns and first rows, as pandas reads them in the
    sandbox, by file name. Empty when there are none, or when the sandbox can't read them
    (the coder then gets the plain description, as before)."""
    sheets = {i.name: i.path for i in inputs if i.path.suffix.lower() in SHEET_SUFFIXES}
    if not sheets:
        return {}
    ctx.emit({"type": "progress", "label": "Reading the spreadsheet in the sandbox"})
    folder = job_dir / "sheets"
    folder.mkdir(parents=True)
    script = folder / "main.py"
    script.write_text(SHEET_PREVIEW)
    try:
        result = await box.run(script, sheets, folder)
    except SandboxError:
        return {}
    if not result.ok:
        return {}
    shown: dict[str, str] = {}
    for part in result.stdout.split("== ")[1:]:
        name, _, text = part.partition("\n")
        if name in sheets and text.strip():
            shown[name] = text.rstrip()
    return shown


def coder_request(
    ctx: ToolContext,
    params: WriteParams,
    inputs: list[Input],
    sheets: dict[str, str] | None = None,
) -> str:
    """The brief, the expected output, and what each input file looks like (`sheets`: what
    pandas read in each spreadsheet, by file name)."""
    parts = [f"Task:\n{params.brief}"]
    if params.expected_output:
        parts.append(f"Expected output:\n{params.expected_output}")
    if inputs:
        previews = [_preview(i, (sheets or {}).get(i.name)) for i in inputs]
        parts.append("Input files:\n" + "\n\n".join(previews))
    else:
        parts.append("There are no input files; everything needed is in the task.")
    return "\n\n".join(parts)


def _preview(item: Input, sheet: str | None = None) -> str:
    size = item.path.stat().st_size
    header = f"/in/{item.name} ({size} bytes)"
    suffix = item.path.suffix.lower()
    if suffix in SHEET_SUFFIXES and sheet:
        return (
            f"{header}: an Excel workbook; read it with pandas.read_excel. Its sheets, their "
            f"columns and first rows, as pandas reads them:\n{sheet}"
        )
    if suffix in SHEET_SUFFIXES:
        return f"{header}: an Excel workbook; read it with pandas.read_excel or openpyxl."
    try:
        with item.path.open(encoding="utf-8", errors="replace") as handle:
            lines = [next(handle, "") for _ in range(PREVIEW_LINES)]
    except OSError:
        return f"{header}: unreadable."
    if "\x00" in "".join(lines):
        return f"{header}: a binary file."
    shown = "".join(line[:PREVIEW_LINE_CHARS] for line in lines if line).rstrip()
    return f"{header}, first lines:\n{shown}"


def _save_script(
    ctx: ToolContext,
    params: WriteParams,
    inputs: list[Input],
    code: str,
    result: RunResult,
    model_id: str,
) -> ToolResult:
    with ctx.sessions.begin() as session:
        script = files.write_text(
            ctx.project.root, session, owner_id=ctx.user.id, name=f"{params.name}.py",
            text=code, origin=files.Origin("session", id=ctx.session_id), folder=SCRIPTS,
        )  # fmt: skip
        manifest = {
            "entry": script.path,
            "image": IMAGE,
            "inputs": [{"path": _input_path(session, i), "sha256": i.sha256} for i in inputs],
            "expected_output": params.expected_output,
            "brief": params.brief,
            "model": model_id,
            "test_run": {
                "exit_code": result.exit_code,
                "seconds": result.seconds,
                "stdout": result.stdout[:2000],
            },
        }
        files.write_text(
            ctx.project.root, session, owner_id=ctx.user.id,
            name=f"{Path(script.path).stem}.json",
            text=json.dumps(manifest, indent=2, ensure_ascii=False),
            origin=files.Origin("artifact", id=script.id), folder=SCRIPTS,
        )  # fmt: skip
    _store_run(ctx, script, inputs, result, test_run=True)
    lines = code.count("\n")
    # The output isn't given here, only that the test passed: the answer must come from
    # code.run, the one place results are read from (and which returns this test run at once).
    return ToolResult(
        f"Wrote {script.path} ({lines} lines) with {model_id}; its test run passed. "
        f'Now call code.run(name="{Path(script.path).stem}") and answer from its output.',
        refs=[{"kind": "artifact", "id": script.id, "name": script.path, "ref": None}],
    )


def _input_path(session, item: Input) -> str:  # noqa: ANN001
    row = session.get(ArtifactRow, item.artifact_id)
    return row.path if row is not None else item.name


async def ensure_coder(ctx: ToolContext) -> None:
    """The coding model on disk: asks with a card to download it the first time, and waits
    for the download, reporting its progress."""
    entry = role_model(ctx.runtime, "code")
    if entry is None:
        raise ToolError("No coding model is set for this computer. Say the result is not computed.")
    if model_download.on_disk(ctx, entry):
        return
    # No card for a download this person couldn't allow.
    if (why := model_download.why_not(ctx, entry)) is not None:
        raise ToolError(
            f"The coding model isn't downloaded yet, and {why}. Show the formula and say the "
            "result is not computed."
        )
    question = (
        f"Download {entry.title} ({entry.size / 1e9:.1f} GB, {entry.licence}) to write scripts?"
    )
    if not await ctx.confirm(question):
        raise ToolError(
            "The person didn't allow downloading the coding model, so no script can be written. "
            "Show the formula and say the result is not computed."
        )
    state = await model_download.download(ctx, entry)
    if state.status == "failed":
        raise ToolError(f"The coding model's download failed: {state.error}")
    if state.status == "paused":
        raise ToolError(
            "The coding model's download was stopped before it finished. Show the formula and "
            "say the result is not computed; asking again resumes the download."
        )


CODE_WRITE = AgentTool(
    name="code.write",
    description=(
        "Have the coding model write a Python script for an exact result (arithmetic, dates, "
        "sums, percentages, tables). Give a brief with every number it needs. The script is "
        "saved to scripts/ and test-run; then call code.run to get the result."
    ),
    params=WriteParams,
    run=_code_write,
    max_tokens=600,
)
