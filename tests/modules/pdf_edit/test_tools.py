"""pdf.edit and pdf.fill as the chat calls them: a new file each time, the original kept,
asking first only when the page would look different, and the person's files only."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from lectern.agent.tools import ToolContext, ToolError
from lectern.modules import ToolEnv, load_modules
from lectern.modules.pdf_edit import embed
from lectern.modules.pdf_edit import fonts as font_index
from lectern.modules.pdf_edit.fonts import InstalledFonts
from lectern.projects import files
from lectern.projects.tables import ArtifactRow

from ..conftest import add
from .conftest import Run, form_pdf, make_pdf

LINES = [
    [Run("The total fee is EUR 1,250.00, payable in two")],
    [Run("Birth certificate"), Run("EUR 150.00", x=300)],
]


@pytest.fixture(autouse=True)
def installed_fonts(monkeypatch: pytest.MonkeyPatch, font_files: Path) -> None:
    """This computer's fonts, for the tools: the test PDFs' fonts, nothing else."""
    monkeypatch.setattr(font_index, "_default", InstalledFonts([font_files]))


def _tools() -> dict:
    (module,) = [m for m in load_modules() if m.id == "pdf-edit"]
    return {t.name: t for t in module.tools(ToolEnv(sandbox=False))}


def _call(tool_name: str, ctx: ToolContext, **arguments):  # noqa: ANN202
    tool = _tools()[tool_name]
    result = tool.run(ctx, tool.validate(arguments))
    return asyncio.run(result) if inspect.isawaitable(result) else result


def _import(ctx: ToolContext, path: Path, owner_id: int | None = None) -> ArtifactRow:
    with ctx.sessions.begin() as session:
        row = files.write_bytes(
            ctx.project.root, session, owner_id=owner_id or ctx.user.id, name=path.name,
            data=path.read_bytes(), origin=files.Origin("import"),
        )  # fmt: skip
        session.expunge(row)
        return row


def _row(ctx: ToolContext, artifact_id: int) -> ArtifactRow:
    with ctx.sessions() as session:
        row = session.get(ArtifactRow, artifact_id)
        session.expunge(row)
        return row


@pytest.fixture
def contract(tmp_path: Path, font_files: Path) -> Path:
    return make_pdf(tmp_path / "contract.pdf", font_files, [LINES])


def test_an_edit_writes_a_new_file_beside_the_original_and_opens_it(make_ctx, contract) -> None:  # noqa: ANN001
    ctx = make_ctx()
    source = _import(ctx, contract)
    original = (ctx.project.root / source.path).read_bytes()

    result = _call("pdf.edit", ctx, name="contract.pdf",
                   edits=[{"find": "1,250.00", "replace": "1,480.00", "page": 1}])  # fmt: skip

    assert result.text.startswith("Saved as contract (edited).pdf, ")
    assert "p1: “The total fee is EUR 1,480.00, payable in two”" in result.text
    row = _row(ctx, result.refs[0]["id"])
    assert row.path == "contract (edited).pdf" and row.mime == "application/pdf"
    assert row.from_kind == "artifact" and row.from_id == source.id
    assert result.action == {"type": "open_tab", "tab": {"kind": "artifact", "id": row.id}}
    assert (ctx.project.root / source.path).read_bytes() == original
    assert ctx.asked == []


def test_editing_an_edited_pdf_writes_another_new_file(make_ctx, contract) -> None:  # noqa: ANN001
    ctx = make_ctx()
    _import(ctx, contract)
    _call("pdf.edit", ctx, name="contract.pdf", edits=[{"find": "two", "replace": "three"}])

    again = _call("pdf.edit", ctx, name="contract (edited).pdf",
                  edits=[{"find": "three", "replace": "two"}])  # fmt: skip

    assert again.text.startswith("Saved as contract (edited) (")
    assert (ctx.project.root / "contract (edited).pdf").is_file()


def test_an_edit_that_changes_the_look_asks_first_and_no_saves_nothing(make_ctx, contract) -> None:  # noqa: ANN001
    ctx = make_ctx(answers=[False])
    _import(ctx, contract)
    longer = "Birth certificate with an apostille attached"

    with pytest.raises(ToolError, match="The person said no"):
        _call("pdf.edit", ctx, name="contract.pdf",
              edits=[{"find": "Birth certificate", "replace": longer}])  # fmt: skip

    [question] = ctx.asked
    assert question.startswith("Save the edited contract.pdf? It won't look quite like")
    assert "into the text beside it" in question
    assert not (ctx.project.root / "contract (edited).pdf").exists()


def test_yes_to_the_question_saves_it_and_says_what_changed(make_ctx, contract) -> None:  # noqa: ANN001
    ctx = make_ctx(answers=[True])
    _import(ctx, contract)

    result = _call("pdf.edit", ctx, name="contract.pdf",
                   edits=[{"find": "Birth certificate",
                           "replace": "Birth certificate with an apostille attached"}])  # fmt: skip

    assert "into the text beside it" in result.text
    assert (ctx.project.root / "contract (edited).pdf").is_file()


def test_an_edit_that_cant_be_made_tells_the_model_why(make_ctx, contract) -> None:  # noqa: ANN001
    ctx = make_ctx()
    _import(ctx, contract)

    with pytest.raises(ToolError, match="It isn't in the PDF. The nearest lines"):
        _call("pdf.edit", ctx, name="contract.pdf", edits=[{"find": "zzz", "replace": "y"}])

    assert not (ctx.project.root / "contract (edited).pdf").exists()


def test_a_file_that_isnt_a_pdf_is_refused(make_ctx) -> None:  # noqa: ANN001
    ctx = make_ctx()
    add(ctx, "notes.txt", "The total fee")

    with pytest.raises(ToolError, match="notes.txt isn't a PDF"):
        _call("pdf.edit", ctx, name="notes.txt", edits=[{"find": "total", "replace": "x"}])


def test_someone_elses_pdf_isnt_found(make_ctx, contract) -> None:  # noqa: ANN001
    ctx = make_ctx()
    _import(ctx, contract, owner_id=ctx.user.id + 1)

    with pytest.raises(ToolError) as failed:
        _call("pdf.edit", ctx, name="contract.pdf", edits=[{"find": "two", "replace": "x"}])

    assert "contract.pdf" in str(failed.value)
    assert not (ctx.project.root / "contract (edited).pdf").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "contract.pdf", "edits": []},
        {"name": "contract.pdf", "edits": [{"find": "", "replace": "x"}]},
        {"name": "contract.pdf", "edits": [{"find": "a", "replace": "b", "page": 0}]},
        {"name": "", "edits": [{"find": "a", "replace": "b"}]},
    ],
    ids=["no edits", "empty find", "page 0", "no name"],
)
def test_arguments_the_tool_cant_take_are_refused(make_ctx, arguments: dict) -> None:  # noqa: ANN001
    with pytest.raises(ToolError):
        _tools()["pdf.edit"].validate(arguments)


def test_fill_without_values_lists_the_fields(make_ctx, tmp_path: Path) -> None:  # noqa: ANN001
    ctx = make_ctx()
    _import(ctx, form_pdf(tmp_path / "form.pdf"))

    result = _call("pdf.fill", ctx, name="form.pdf")

    assert "“name” · text · p1 · “”" in result.text
    assert "“size” · radio · p1 · “” · options: “S”, “M”" in result.text
    assert "“id” · text · p1 · “A-17” · read-only" in result.text
    assert not (ctx.project.root / "form (edited).pdf").exists()


def test_fill_writes_a_new_file_with_the_values(make_ctx, tmp_path: Path) -> None:  # noqa: ANN001
    ctx = make_ctx()
    source = _import(ctx, form_pdf(tmp_path / "form.pdf"))

    result = _call("pdf.fill", ctx, name="form.pdf",
                   fields={"name": "Olena Shevchenko", "agree": "yes"})  # fmt: skip

    assert result.text.startswith("Saved as form (edited).pdf, ")
    assert "“name” · text · p1 · “Olena Shevchenko”" in result.text
    assert "“agree” · checkbox · p1 · “yes”" in result.text
    assert _row(ctx, result.refs[0]["id"]).from_id == source.id


def test_fill_says_when_its_text_is_in_a_font_that_isnt_embedded(make_ctx, tmp_path: Path) -> None:  # noqa: ANN001
    ctx = make_ctx()  # only the test fonts are installed: neither Arial nor a fallback
    _import(ctx, form_pdf(tmp_path / "form.pdf"))

    result = _call("pdf.fill", ctx, name="form.pdf", fields={"name": "Олена Шевченко"})

    assert "“name” · text · p1 · “Олена Шевченко”" in result.text
    assert "uses a font that isn't embedded" in result.text
    assert "it shows only where a viewer has that font" in result.text


def test_fill_with_its_fonts_embedded_says_nothing_of_them(
    make_ctx,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    monkeypatch.setattr(embed, "FALLBACK_SANS", ("TestSans",))
    ctx = make_ctx()
    _import(ctx, form_pdf(tmp_path / "form.pdf"))

    cyrillic = _call("pdf.fill", ctx, name="form.pdf", fields={"name": "Олена Шевченко"})
    latin = _call("pdf.fill", ctx, name="form.pdf", fields={"name": "Olena"})

    assert "isn't embedded" not in cyrillic.text and "isn't embedded" not in latin.text


def test_fill_on_a_pdf_without_a_form_points_to_pdf_edit(make_ctx, contract) -> None:  # noqa: ANN001
    ctx = make_ctx()
    _import(ctx, contract)

    with pytest.raises(ToolError, match="has no form fields. To change its text, use pdf.edit"):
        _call("pdf.fill", ctx, name="contract.pdf")


def test_fill_that_cant_be_made_tells_the_model_why(make_ctx, tmp_path: Path) -> None:  # noqa: ANN001
    ctx = make_ctx()
    _import(ctx, form_pdf(tmp_path / "form.pdf"))

    with pytest.raises(ToolError, match="“phone”: there is no such field"):
        _call("pdf.fill", ctx, name="form.pdf", fields={"phone": "1"})


def test_the_tools_say_what_a_call_writes_and_what_it_writes_over() -> None:
    tools = _tools()
    edit = tools["pdf.edit"].validate({
        "name": "a.pdf",
        "edits": [{"find": "EUR 1,250.00", "replace": "EUR 1,480.00"},
                  {"find": "two", "replace": "three"}],
    })  # fmt: skip
    fill = tools["pdf.fill"].validate({"name": "f.pdf", "fields": {"date": "08.11.2026"}})

    assert tools["pdf.edit"].writes(edit) == ("EUR 1,480.00 three", "EUR 1,250.00 two")
    assert tools["pdf.fill"].writes(fill) == ("08.11.2026", "")
