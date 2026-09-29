from __future__ import annotations

from dataclasses import replace

import pytest

from lectern.agent import core_tools
from lectern.agent.core_tools import CORE_TOOLS, core_registry
from lectern.agent.tools import ToolError, page
from lectern.projects import files

from .conftest import add_file

LEASE = """# Сторони
Орендар: Ковальчук Олена Петрівна.

# Оплата
Орендна плата 250,00 грн сплачується до 10 числа.

# Пеня
0,5% від суми за кожен день прострочення, включно з днем оплати.
"""


def run(tool, ctx, **arguments):
    return tool.run(ctx, tool.validate(arguments))


def test_every_tool_has_a_wire_name_and_a_schema():
    registry = core_registry()
    names = [s["function"]["name"] for s in registry.schemas()]
    assert names == [t.name.replace(".", "_") for t in CORE_TOOLS]
    assert all(set(n) <= set("abcdefghijklmnopqrstuvwxyz_") for n in names)
    find = registry.get("project_find").schema()["function"]["parameters"]
    assert find["required"] == ["query"] and "title" not in find
    assert [t.name for t in CORE_TOOLS if t.confirm] == ["artifact.trash"]


def test_bad_arguments_come_back_as_a_message_the_model_can_use():
    with pytest.raises(ToolError, match=r"Invalid arguments for project.find: query: .*"):
        core_tools.FIND.validate('{"query": ""}')
    with pytest.raises(ToolError, match="Invalid arguments"):
        core_tools.READ.validate("{not json")
    with pytest.raises(ToolError, match="kind"):
        core_tools.LIST.validate({"kind": "everything"})


def test_list_and_find(ctx):
    add_file(ctx, "lease-1998.md", LEASE)
    add_file(ctx, "reply-letter.md", "Dear tenant", files.Origin("session", id=1))

    listed = run(core_tools.LIST, ctx).text
    assert listed.splitlines()[0] == "New chat · chat session"
    assert "lease-1998.md · file ·" in listed and "imported" in listed
    assert "reply-letter.md" in listed and "from chat 'New chat'" in listed

    found = run(core_tools.FIND, ctx, query="LEASE 1998")
    assert found.text.startswith("lease-1998.md · file")
    assert found.refs[0]["name"] == "lease-1998.md"
    assert run(core_tools.FIND, ctx, query="nothing").text == (
        "Nothing in this project matches 'nothing'."
    )
    assert run(core_tools.LIST, ctx, kind="sessions").text == "New chat · chat session"


def test_search_returns_block_refs_not_whole_files(ctx):
    add_file(ctx, "lease-1998.md", LEASE)
    result = run(core_tools.SEARCH, ctx, query="прострочення")
    assert result.text.startswith("lease-1998.md #c3 (Пеня): ")
    assert "Орендар" not in result.text
    assert result.refs == [{"kind": "artifact", "id": 1, "name": "lease-1998.md", "ref": "#c3"}]


def test_outline_and_read_by_block_heading_and_whole(ctx):
    add_file(ctx, "lease-1998.md", LEASE)

    outline = run(core_tools.OUTLINE, ctx, name="lease-1998.md").text.splitlines()
    assert outline[0] == "lease-1998.md: 3 blocks"
    assert [line.rsplit(" (", 1)[0] for line in outline[1:]] == [
        "#c1 Сторони", "#c2 Оплата", "#c3 Пеня"
    ]  # fmt: skip

    block = run(core_tools.READ, ctx, name="lease", refs=["#c3"]).text
    assert block == (
        "[lease-1998.md #c3 — Пеня]\n"
        "0,5% від суми за кожен день прострочення, включно з днем оплати."
    )
    assert "Оплата" in run(core_tools.READ, ctx, name="lease-1998.md", heading="оплата").text
    assert run(core_tools.READ, ctx, name="lease-1998.md").text.count("[lease-1998.md #c") == 3


def test_read_errors_name_what_exists(ctx):
    add_file(ctx, "lease-1998.md", LEASE)
    add_file(ctx, "lease-2001.md", "other")
    with pytest.raises(ToolError, match="matches several files: lease-1998.md, lease-2001.md"):
        run(core_tools.READ, ctx, name="lease")
    with pytest.raises(ToolError, match="No file called 'x.md'"):
        run(core_tools.READ, ctx, name="x.md")
    with pytest.raises(ToolError, match=r"#c1–#c3; none of \['#c9'\]"):
        run(core_tools.READ, ctx, name="lease-1998.md", refs=["#c9"])
    with pytest.raises(ToolError, match="Headings: Оплата, Пеня, Сторони"):
        run(core_tools.READ, ctx, name="lease-1998.md", heading="Terms")


def test_a_long_read_comes_in_pages_with_a_cursor(ctx, monkeypatch):
    monkeypatch.setattr(core_tools, "READ", replace(core_tools.READ, max_tokens=20))
    add_file(ctx, "long.txt", "\n".join(f"line {i:03d} of the long file" for i in range(40)))

    first = run(core_tools.READ, ctx, name="long.txt")
    assert len(first.text) < 20 * 3 + 80
    assert first.cursor is not None and "more characters: call again with cursor" in first.text
    seen = first.text.split("\n[")[0]
    cursor = first.cursor
    while cursor:
        more = run(core_tools.READ, ctx, name="long.txt", cursor=cursor)
        seen += "\n" + more.text.split("\n[")[0] if more.cursor else "\n" + more.text
        cursor = more.cursor
    assert all(f"line {i:03d}" in seen for i in range(40))


def test_page_rejects_a_bad_cursor():
    with pytest.raises(ToolError, match="Unknown cursor"):
        page("abc", "nope", 10)
    with pytest.raises(ToolError, match="past the end"):
        page("abc", "99", 10)
    assert page("abc", None, 10) == ("abc", None)


def test_write_creates_a_new_file_from_this_chat_and_never_overwrites(ctx):
    add_file(ctx, "reply.md", "by hand")

    result = run(core_tools.WRITE, ctx, name="reply.md", text="# Reply\nFrom the model")

    assert result.text.startswith("Saved as reply (2).md")
    assert (ctx.project.root / "reply.md").read_text() == "by hand"
    assert (ctx.project.root / "reply (2).md").read_text() == "# Reply\nFrom the model"
    with ctx.sessions() as session:
        row = files.live(session)[-1]
    assert (row.kind, row.from_kind, row.from_id) == ("generated", "session", 1)


@pytest.mark.parametrize("name", ["app.exe", "noext", ".env", "../x.md/"])
def test_write_refuses_non_text_and_bad_names(ctx, name):
    with pytest.raises(ToolError):
        run(core_tools.WRITE, ctx, name=name, text="x")


def test_trash_moves_the_file(ctx):
    add_file(ctx, "old.md", "old")
    tool = core_tools.TRASH
    assert tool.confirm_text(tool.validate({"name": "old.md"})) == "Move old.md to the trash?"

    result = run(tool, ctx, name="old.md")

    assert result.text.startswith("Moved old.md to the trash")
    assert not (ctx.project.root / "old.md").exists()
    with pytest.raises(ToolError, match="No file called"):
        run(tool, ctx, name="old.md")


def test_session_status_and_tab_open(ctx):
    ctx.turn_state = lambda session_id: "running" if session_id == 1 else "idle"
    add_file(ctx, "notes.md", "n")

    assert run(core_tools.STATUS, ctx, title="new chat").text.startswith(
        "New chat · chat session · running · 0 messages"
    )
    opened = run(core_tools.OPEN, ctx, name="notes.md")
    assert opened.action == {"type": "open_tab", "tab": {"kind": "artifact", "id": 1}}
    chat = run(core_tools.OPEN, ctx, name="New chat")
    assert chat.action == {"type": "open_tab", "tab": {"kind": "session", "id": 1}}
    with pytest.raises(ToolError, match="No session called"):
        run(core_tools.OPEN, ctx, name="missing")


def test_write_names_a_cleaned_up_path_without_claiming_it_was_taken(ctx):
    result = run(core_tools.WRITE, ctx, name="drafts/summary.md", text="x")
    assert result.text == "Saved as summary.md, 1 B."
