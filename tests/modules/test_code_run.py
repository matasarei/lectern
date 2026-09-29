from __future__ import annotations

import asyncio
import json

import pytest

from lectern.agent.tools import ToolError
from lectern.modules import ToolEnv
from lectern.modules.sandbox import SandboxModule
from lectern.modules.sandbox.container import RunResult
from lectern.modules.sandbox.tools import CODE_RUN
from lectern.projects import files
from lectern.projects.tables import ScriptRunRow

from .conftest import FakeSandbox, add


def run(ctx, **arguments):
    return asyncio.run(CODE_RUN.run(ctx, CODE_RUN.validate(arguments)))


def test_offered_only_while_an_engine_runs():
    assert SandboxModule().tools(ToolEnv(sandbox=False)) == []
    assert [t.name for t in SandboxModule().tools(ToolEnv(sandbox=True))] == [
        "code.write", "code.run"
    ]  # fmt: skip


def test_runs_a_script_with_its_inputs_and_keeps_the_run(make_ctx):
    box = FakeSandbox(behave=lambda code, inputs, out: RunResult(0, "sum 3\n", "", 0.4))
    ctx = make_ctx(box)
    add(ctx, "sum.py", "print(3)", folder="scripts")
    add(ctx, "rent.csv", "a\n1\n2\n")

    result = run(ctx, name="sum", inputs=["rent.csv"])

    assert result.text.startswith("Ran scripts/sum.py (exit 0, 0.4 s).\nOutput:\nsum 3")
    assert box.runs == [{"code": "print(3)", "inputs": {"rent.csv": "a\n1\n2\n"}}]
    with ctx.sessions() as session:
        stored = session.query(ScriptRunRow).one()
    assert (stored.exit_code, stored.stdout, stored.test_run) == (0, "sum 3\n", False)


def test_an_identical_run_comes_from_the_store(make_ctx):
    box = FakeSandbox()
    ctx = make_ctx(box)
    add(ctx, "a.py", "print(1)", folder="scripts")
    add(ctx, "in.csv", "x")

    run(ctx, name="scripts/a.py", inputs=["in.csv"])
    again = run(ctx, name="a", inputs=["in.csv"])

    assert len(box.runs) == 1
    assert "the stored result of an identical earlier run" in again.text
    add(ctx, "in2.csv", "y")
    run(ctx, name="a", inputs=["in2.csv"])  # other inputs: runs again
    assert len(box.runs) == 2


def test_files_written_to_out_become_project_files_from_the_script(make_ctx):
    def behave(code, inputs, out):  # noqa: ANN001
        (out / "table.csv").write_text("due,penalty\n10,62.50\n")
        return RunResult(0, "wrote table.csv\n", "", 0.2)

    ctx = make_ctx(FakeSandbox(behave=behave))
    script_id = add(ctx, "penalty.py", "...", folder="scripts")

    result = run(ctx, name="penalty", inputs=[])

    assert "Files written: scripts/out/table.csv" in result.text
    with ctx.sessions() as session:
        row = [r for r in files.live(session) if r.path == "scripts/out/table.csv"][0]
    assert (row.kind, row.from_kind, row.from_id) == ("generated", "artifact", script_id)
    assert (ctx.project.root / "scripts" / "out" / "table.csv").read_text().startswith("due")
    assert not any((ctx.project.root / ".lectern" / "jobs").iterdir())


def test_a_failing_script_shows_its_errors_and_is_not_reused(make_ctx):
    box = FakeSandbox(behave=lambda c, i, o: RunResult(1, "", "ZeroDivisionError: x", 0.1))
    ctx = make_ctx(box)
    add(ctx, "bad.py", "1/0", folder="scripts")

    first = run(ctx, name="bad", inputs=[])
    run(ctx, name="bad", inputs=[])

    assert "(exit 1," in first.text and "Errors:\nZeroDivisionError: x" in first.text
    assert len(box.runs) == 2


def test_long_output_comes_in_pages(make_ctx):
    lines = "".join(f"row {i:04d} value {i * 7}\n" for i in range(400))
    ctx = make_ctx(FakeSandbox(behave=lambda c, i, o: RunResult(0, lines, "", 0.1)))
    add(ctx, "many.py", "...", folder="scripts")

    first = run(ctx, name="many", inputs=[])
    second = run(ctx, name="many", inputs=[], cursor=first.cursor)

    assert first.cursor and "row 0000" in first.text and "row 0399" not in first.text
    assert "row 0000" not in second.text


def test_the_manifest_gives_the_default_inputs(make_ctx):
    box = FakeSandbox()
    ctx = make_ctx(box)
    add(ctx, "rent.csv", "a\n1\n")
    add(ctx, "sum.py", "...", folder="scripts")
    add(ctx, "sum.json", json.dumps({"inputs": [{"path": "rent.csv"}]}), folder="scripts")

    run(ctx, name="sum")

    assert box.runs[0]["inputs"] == {"rent.csv": "a\n1\n"}


def test_the_image_is_built_after_a_card(make_ctx):
    box = FakeSandbox(image=False)
    ctx = make_ctx(box, answers=[True])
    add(ctx, "a.py", "...", folder="scripts")

    run(ctx, name="a", inputs=[])

    assert ctx.asked == [
        "Build the script sandbox? It downloads about 250 MB once (Python and a few libraries)."
    ]
    assert box.builds == 1 and len(box.runs) == 1
    assert any(e.get("line", "").startswith("Step 1/3") for e in ctx.events)


def test_no_image_and_dont_allow_means_not_computed(make_ctx):
    box = FakeSandbox(image=False)
    ctx = make_ctx(box, answers=[False])
    add(ctx, "a.py", "...", folder="scripts")
    with pytest.raises(ToolError, match="not computed"):
        run(ctx, name="a", inputs=[])
    assert box.builds == 0 and box.runs == []


@pytest.mark.parametrize(
    ("name", "inputs", "error"),
    [
        ("missing", [], "No script 'missing' in scripts/"),
        ("a", ["nope.csv"], "No file called exactly 'nope.csv'"),
    ],
)
def test_unknown_scripts_and_inputs_are_errors(make_ctx, name, inputs, error):
    ctx = make_ctx(FakeSandbox())
    add(ctx, "a.py", "...", folder="scripts")
    with pytest.raises(ToolError, match=error):
        run(ctx, name=name, inputs=inputs)


def test_a_file_outside_scripts_is_not_a_script(make_ctx):
    ctx = make_ctx(FakeSandbox())
    add(ctx, "evil.py", "import os")
    with pytest.raises(ToolError, match="No script 'evil'"):
        run(ctx, name="evil", inputs=[])


def test_a_script_edited_on_disk_runs_again_instead_of_returning_the_old_result(make_ctx):
    box = FakeSandbox()
    ctx = make_ctx(box)
    add(ctx, "a.py", "print(1)", folder="scripts")
    run(ctx, name="a", inputs=[])

    (ctx.project.root / "scripts" / "a.py").write_text("print(2)")  # edited by hand
    again = run(ctx, name="a", inputs=[])

    assert len(box.runs) == 2 and box.runs[1]["code"] == "print(2)"
    assert "stored result" not in again.text
    run(ctx, name="a", inputs=[])  # unchanged since: stored
    assert len(box.runs) == 2
