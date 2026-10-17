from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from lectern.agent.core_tools import CORE_TOOLS
from lectern.agent.tools import Registry, ToolError
from lectern.agent.turns import TurnManager
from lectern.catalog import catalog
from lectern.core.auth import LOCAL_USER
from lectern.hardware.tiers import Tier
from lectern.modules.sandbox.container import IMAGE, RunResult
from lectern.modules.sandbox.tools import CODE_RUN, CODE_WRITE, extract_code
from lectern.projects import files
from lectern.projects.tables import ScriptRunRow
from lectern.runners.base import RunnerError
from lectern.runtime.supervisor import NotAvailable

from ..agent.fake_model import GIB, FakeHandle, make_runtime, text_reply, tool_reply
from .conftest import FakeDownloads, FakeSandbox, add

GOOD = "```python\nprint('penalty', round(250 * 0.005 * 50, 2))\n```"
BRIEF = {
    "name": "penalty",
    "brief": "Penalty on 250.00 at 0.5% per day for 50 days, payment day included.",
    "expected_output": "one line: penalty <amount>",
}


def reply(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


def write(ctx, **arguments):
    return asyncio.run(CODE_WRITE.run(ctx, CODE_WRITE.validate({**BRIEF, **arguments})))


def ctx_for(make_ctx, replies, *, sandbox=None, downloads=None, answers=(), runtime=None):  # noqa: ANN001
    handle = FakeHandle([reply(r) for r in replies])
    ctx = make_ctx(
        sandbox or FakeSandbox(behave=lambda c, i, o: RunResult(0, "penalty 62.5\n", "", 0.3)),
        answers=answers,
        runtime=runtime or make_runtime(handle),
        downloads=downloads or FakeDownloads(),
    )
    return ctx, handle


def test_writes_tests_and_saves_the_script_with_its_manifest(make_ctx):
    ctx, handle = ctx_for(make_ctx, [GOOD])
    add(ctx, "rent.csv", "month,amount\nApril,250.00\n")

    result = write(ctx, inputs=["rent.csv"])

    assert result.text == (
        "Wrote scripts/penalty.py (1 lines) with qwen3.5-4b; its test run passed. "
        'Now call code.run(name="penalty") and answer from its output.'
    )
    assert "62.5" not in result.text  # the answer comes from code.run, not from here
    script = (ctx.project.root / "scripts" / "penalty.py").read_text()
    assert script == "print('penalty', round(250 * 0.005 * 50, 2))\n"
    manifest = json.loads((ctx.project.root / "scripts" / "penalty.json").read_text())
    assert manifest["entry"] == "scripts/penalty.py" and manifest["model"] == "qwen3.5-4b"
    assert manifest["image"] == IMAGE and manifest["brief"] == BRIEF["brief"]
    assert (
        manifest["inputs"][0]["path"] == "rent.csv" and len(manifest["inputs"][0]["sha256"]) == 64
    )
    assert manifest["test_run"]["exit_code"] == 0
    request = handle.requests[0]
    assert request["messages"][0]["content"].startswith("You write one Python 3.12 script")
    assert "/in/rent.csv" in request["messages"][1]["content"]
    assert "month,amount\nApril,250.00" in request["messages"][1]["content"]
    with ctx.sessions() as session:
        rows = {r.path: r for r in files.live(session)}
        runs = session.scalars(select(ScriptRunRow)).all()
    assert rows["scripts/penalty.py"].from_kind == "session"
    assert rows["scripts/penalty.json"].from_id == rows["scripts/penalty.py"].id
    assert [r.test_run for r in runs] == [True]


def test_code_run_right_after_reuses_the_test_run(make_ctx):
    ctx, _ = ctx_for(make_ctx, [GOOD])
    add(ctx, "rent.csv", "a\n1\n")
    write(ctx, inputs=["rent.csv"])

    result = asyncio.run(CODE_RUN.run(ctx, CODE_RUN.validate({"name": "penalty"})))

    assert "the stored result of an identical earlier run" in result.text
    assert len(ctx.sandbox.runs) == 1  # only the test run


def test_up_to_three_fixes_without_reloading_the_model(make_ctx):
    def behave(code, inputs, out):  # noqa: ANN001
        if "boom" in code:
            return RunResult(1, "", "NameError: name 'boom' is not defined", 0.1)
        return RunResult(0, "penalty 62.5\n", "", 0.1)

    replies = [
        "Sure, here is the idea without code.",
        "```python\nprint(1\n```",
        "```python\nprint(boom)\n```",
        GOOD,
    ]
    ctx, handle = ctx_for(make_ctx, replies, sandbox=FakeSandbox(behave=behave))

    result = write(ctx)

    assert result.text.startswith("Wrote scripts/penalty.py")
    followups = [r["messages"][-1]["content"] for r in handle.requests[1:]]
    assert followups[0].startswith("There was no ```python block")
    assert followups[1].startswith("The script doesn't compile: '(' was never closed")
    assert followups[2].startswith("The test run failed with exit 1. Error output:\nNameError")
    assert ctx.runtime.state()["swaps"] == 0 and len(ctx.runtime.state()["residents"]) == 1


def test_it_gives_up_after_three_fixes_and_saves_nothing(make_ctx):
    ctx, handle = ctx_for(make_ctx, ["no code"] * 4)
    with pytest.raises(ToolError, match="after 3 fixes.*not computed"):
        write(ctx)
    assert len(handle.requests) == 4
    assert not (ctx.project.root / "scripts").exists()


def test_the_coding_model_downloaded_is_the_one_chosen_for_the_role(make_ctx):
    downloads = FakeDownloads(downloaded=False)
    ctx, _ = ctx_for(make_ctx, [GOOD], downloads=downloads, answers=[True])
    ctx.runtime.chosen_models = lambda: {"code": "qwen2.5-coder-7b"}

    write(ctx)

    assert "Qwen2.5-Coder 7B" in ctx.asked[0]
    assert downloads.started == ["qwen2.5-coder-7b"]


def test_the_coding_model_is_downloaded_after_a_card(make_ctx):
    downloads = FakeDownloads(downloaded=False)
    ctx, _ = ctx_for(make_ctx, [GOOD], downloads=downloads, answers=[True])

    write(ctx)

    entry = catalog.load().defaults_for(Tier.MINIMUM)["code"]
    assert ctx.asked == [
        f"Download {entry.title} ({entry.size / 1e9:.1f} GB, {entry.licence}) to write scripts?"
    ]
    assert downloads.started == [entry.id]
    bars = [e for e in ctx.events if e.get("label", "").startswith("Downloading")]
    assert [b["done"] for b in bars] == [entry.size // 2, entry.size]


def test_no_download_means_no_script_and_says_not_computed(make_ctx):
    downloads = FakeDownloads(downloaded=False)
    ctx, handle = ctx_for(make_ctx, [GOOD], downloads=downloads, answers=[False])
    with pytest.raises(ToolError, match="didn't allow downloading.*not computed"):
        write(ctx)
    assert downloads.started == [] and handle.requests == []


def test_a_member_is_told_the_download_is_the_admins_and_no_card_offers_it(make_ctx):
    from dataclasses import replace

    from lectern.core.auth import MEMBER, User

    downloads = FakeDownloads(downloaded=False)
    ctx, handle = ctx_for(make_ctx, [GOOD], downloads=downloads, answers=[True])
    member = replace(ctx, user=User(id=ctx.user.id, name=ctx.user.name, role=MEMBER))
    entry = catalog.load().defaults_for(Tier.MINIMUM)["code"]

    with pytest.raises(ToolError, match=f"only an admin.*{entry.title}.*not computed"):
        write(member)
    assert ctx.asked == [] and downloads.started == [] and handle.requests == []


def test_a_failed_download_is_reported(make_ctx):
    ctx, _ = ctx_for(make_ctx, [GOOD], downloads=FakeDownloads(False, fail=True), answers=[True])
    with pytest.raises(ToolError, match="download failed: disk full"):
        write(ctx)


def test_main_and_code_swap_when_they_dont_fit_together(make_ctx):
    handle = FakeHandle([reply(GOOD)])
    # The fake model reports 2 GB in use once loaded; 2 + 7 > 8 GB, so code can't join main.
    runtime = make_runtime(handle, weights=7 * GIB, per_role=True)

    async def scenario(ctx):
        async with runtime.lease("main"):
            pass  # main is loaded and idle, as after a chat reply
        await CODE_WRITE.run(ctx, CODE_WRITE.validate(BRIEF))
        return runtime.state()

    ctx, _ = ctx_for(make_ctx, [], runtime=runtime)
    state = asyncio.run(scenario(ctx))

    assert state["swaps"] == 1
    assert [r["role"] for r in state["residents"]] == ["code"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("```python\nx = 1\nprint(x)\n```", "x = 1\nprint(x)\n"),
        ("Here:\n```py\nprint(2)\n```\nDone.", "print(2)\n"),
        ("print(3)", "print(3)\n"),
        ("just words, no code", None),
        ("```python\nprint(1)", None),
    ],
)
def test_extract_code(text, code):
    assert extract_code(text) == code


@pytest.mark.parametrize("name", ["../evil", "Penalty", "a b", "", "x" * 80])
def test_script_names_are_plain_slugs(name):
    with pytest.raises(ToolError):
        CODE_WRITE.validate({**BRIEF, "name": name})


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (NotAvailable("Qwen2.5-Coder 1.5B doesn't fit next to the chat model."), "doesn't fit"),
        (RunnerError("llama-server exited with code 1\nlong log"), "exited with code 1"),
    ],
)
def test_a_coding_model_that_cannot_start_means_not_computed(make_ctx, failure, reason):
    class Failing:
        tier = Tier.MINIMUM
        chosen_models = staticmethod(dict)  # nothing chosen: the recommended coding model

        def lease(self, role, priority):  # noqa: ANN001
            raise failure

    ctx, _ = ctx_for(make_ctx, [], runtime=Failing())
    with pytest.raises(ToolError, match=f"coding model couldn't start: .*{reason}.*not computed"):
        write(ctx)
    assert not (ctx.project.root / "scripts").exists()
    assert not any((ctx.project.root / ".lectern" / "jobs").glob("*"))


def test_in_a_turn_the_model_gets_the_error_and_the_turn_goes_on(store):
    handle = FakeHandle(
        [tool_reply("code_write", BRIEF), text_reply("Not computed: 250 × 0.5% × days.")]
    )
    runtime = make_runtime(handle)
    real_lease = runtime.lease

    def lease(role, priority=None):  # noqa: ANN001
        if role == "code":
            raise NotAvailable("The coding model doesn't fit.")
        return real_lease(role, priority) if priority is not None else real_lease(role)

    runtime.lease = lease  # type: ignore[method-assign]
    manager = TurnManager(
        runtime, store, Registry([*CORE_TOOLS, CODE_WRITE]),
        downloads=FakeDownloads(), sandbox=FakeSandbox(),
    )  # fmt: skip
    project = store.create(LOCAL_USER, "Turn")

    async def scenario():
        await manager.send(project, LOCAL_USER, 1, "What's the penalty?", [])
        return await manager.wait((project.id, 1))

    assert asyncio.run(scenario()) == "done"
    tool_result = handle.requests[1]["messages"][-1]["content"]
    assert tool_result.startswith(
        "Error: The coding model couldn't start: The coding model doesn't"
    )


def test_a_download_stopped_midway_ends_the_wait(make_ctx):
    downloads = FakeDownloads(downloaded=False, end="paused")
    ctx, handle = ctx_for(make_ctx, [GOOD], downloads=downloads, answers=[True])
    with pytest.raises(ToolError, match="stopped before it finished.*not computed"):
        write(ctx)
    assert handle.requests == []


def test_a_file_the_brief_names_is_given_to_the_script_when_no_inputs_are(make_ctx):
    """Seen on the 4B: code_write("Read the file hours.xlsx … sum the 'Hours' column") with
    no inputs. /in was empty, the coder was told there were no input files, and its scripts
    failed to open hours.xlsx three fixes running, twice, until the turn ran out of calls."""
    sandbox = FakeSandbox(behave=lambda c, i, o: RunResult(0, "total 39.25\n", "", 0.2))
    ctx, handle = ctx_for(make_ctx, [GOOD], sandbox=sandbox)
    add(ctx, "hours.xlsx", "Day,Hours")
    add(ctx, "notes.md", "not named in the brief")

    write(ctx, name="sum-hours", brief="Read the file hours.xlsx and sum its 'Hours' column.")

    assert list(sandbox.runs[0]["inputs"]) == ["hours.xlsx"]  # mounted, and nothing else
    assert "/in/hours.xlsx" in handle.requests[0]["messages"][1]["content"]
    manifest = json.loads((ctx.project.root / "scripts" / "sum-hours.json").read_text())
    assert [i["path"] for i in manifest["inputs"]] == ["hours.xlsx"]  # code.run gives it too


@pytest.mark.parametrize(
    ("brief", "mounted"),
    [
        ("Sum the hours in data/hours.xlsx, the 'Hours' column.", ["hours.xlsx"]),  # by its path
        ("Sum HOURS.XLSX's 'Hours' column.", ["hours.xlsx"]),  # case aside
        ("Sum the 'Hours' column of week-hours.xlsx.", []),  # another name, not in the project
        ("Penalty on 250.00 at 0.5% per day for 50 days.", []),  # no file at all
    ],
)
def test_only_files_the_brief_names_exactly_are_given(make_ctx, brief, mounted):
    sandbox = FakeSandbox(behave=lambda c, i, o: RunResult(0, "ok\n", "", 0.2))
    ctx, _ = ctx_for(make_ctx, [GOOD], sandbox=sandbox)
    add(ctx, "hours.xlsx", "Day,Hours", folder="data")

    write(ctx, name="sum-hours", brief=brief)

    assert list(sandbox.runs[0]["inputs"]) == mounted


def test_inputs_given_are_used_as_they_are_whatever_the_brief_names(make_ctx):
    sandbox = FakeSandbox(behave=lambda c, i, o: RunResult(0, "ok\n", "", 0.2))
    ctx, _ = ctx_for(make_ctx, [GOOD], sandbox=sandbox)
    add(ctx, "hours.xlsx", "Day,Hours")
    add(ctx, "rent.csv", "month,amount")

    write(
        ctx, name="sum-rent", brief="Sum rent.csv; hours.xlsx is not needed.", inputs=["rent.csv"]
    )

    assert list(sandbox.runs[0]["inputs"]) == ["rent.csv"]


def test_a_name_two_files_share_gives_neither_unless_the_path_is_named(make_ctx):
    sandbox = FakeSandbox(behave=lambda c, i, o: RunResult(0, "ok\n", "", 0.2))
    ctx, _ = ctx_for(make_ctx, [GOOD, GOOD], sandbox=sandbox)
    add(ctx, "hours.xlsx", "Day,Hours", folder="2025")
    add(ctx, "hours.xlsx", "Day,Hours", folder="2026")

    write(ctx, name="sum-hours", brief="Sum the 'Hours' column of hours.xlsx.")
    write(ctx, name="sum-hours-2026", brief="Sum the 'Hours' column of 2026/hours.xlsx.")

    from lectern.modules.sandbox.tools import SHEET_PREVIEW

    tests = [r for r in sandbox.runs if r["code"] != SHEET_PREVIEW]  # not the sheets' preview
    assert [list(run["inputs"]) for run in tests] == [[], ["hours.xlsx"]]


def test_the_coder_sees_a_spreadsheets_columns_as_pandas_reads_them_in_the_sandbox(make_ctx):
    """Seen on the 4B: told only "an Excel workbook", the coder guessed a column 'hours' for
    'Hours', and finding the name took a write and a run more than the turn had left."""
    from lectern.modules.sandbox.tools import SHEET_PREVIEW

    def behave(code, inputs, out):  # noqa: ANN001, ANN202
        if code == SHEET_PREVIEW:  # what pandas prints of each sheet, inside the sandbox
            return RunResult(
                0, "== hours.xlsx\nsheet 'Week 1': Day, Hours\nDay  Hours\nMon    7.5\n", "", 0.4
            )
        return RunResult(0, "total 39.25\n", "", 0.2)

    sandbox = FakeSandbox(behave=behave)
    ctx, handle = ctx_for(make_ctx, [GOOD], sandbox=sandbox)
    add(ctx, "hours.xlsx", "Day,Hours")

    write(ctx, name="sum-hours", brief="Sum the 'Hours' column of hours.xlsx.")

    asked = handle.requests[0]["messages"][1]["content"]
    assert "sheet 'Week 1': Day, Hours" in asked and "Mon    7.5" in asked
    assert [r["code"] == SHEET_PREVIEW for r in sandbox.runs] == [True, False]  # once, first


def test_a_spreadsheet_the_sandbox_cant_read_is_described_as_before(make_ctx):
    from lectern.modules.sandbox.tools import SHEET_PREVIEW

    def behave(code, inputs, out):  # noqa: ANN001, ANN202
        if code == SHEET_PREVIEW:
            return RunResult(1, "", "ValueError: Excel file format cannot be determined", 0.4)
        return RunResult(0, "total 39.25\n", "", 0.2)

    ctx, handle = ctx_for(make_ctx, [GOOD], sandbox=FakeSandbox(behave=behave))
    add(ctx, "hours.xlsx", "not really a workbook")

    result = write(ctx, name="sum-hours", brief="Sum the 'Hours' column of hours.xlsx.")

    assert "test run passed" in result.text
    assert "an Excel workbook" in handle.requests[0]["messages"][1]["content"]


def test_without_a_spreadsheet_input_there_is_no_preview_run(make_ctx):
    sandbox = FakeSandbox(behave=lambda c, i, o: RunResult(0, "penalty 62.5\n", "", 0.2))
    ctx, _ = ctx_for(make_ctx, [GOOD], sandbox=sandbox)
    add(ctx, "rent.csv", "month,amount\nApril,250.00\n")

    write(ctx, inputs=["rent.csv"])

    assert len(sandbox.runs) == 1  # the test run only
