from __future__ import annotations

import asyncio
import json
import sys

import pytest
from sqlalchemy import select

from lectern.agent.core_tools import CORE_TOOLS
from lectern.agent.tools import Registry, ToolContext, ToolError
from lectern.agent.turns import TurnManager
from lectern.core.auth import LOCAL_USER, MEMBER, User
from lectern.modules.image import tools
from lectern.modules.image.tools import GENERATE, shape_size, wait_text
from lectern.projects import files
from lectern.projects.tables import MessageRow

from ...agent.fake_model import FakeHandle, make_runtime, text_reply, tool_reply
from ..conftest import FakeDownloads as DownloadsWithEvents
from .fakes import images, klein

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake sd-cli is a script")


class Downloads(DownloadsWithEvents):
    """Downloads that can say where a model's files are, for sd-cli's arguments."""

    def __init__(self, root, **kwargs) -> None:  # noqa: ANN001
        super().__init__(**kwargs)
        self.root = root

    def file_path(self, entry, file):  # noqa: ANN001, ANN201
        return self.root / entry.id / file.path


@pytest.fixture
def image_ctx(store, tmp_path):
    def make(*, downloaded: bool = True, user: User = LOCAL_USER) -> ToolContext:
        runtime = make_runtime(FakeHandle([]))
        made_by, _ = images(tmp_path, runtime)
        made_by.downloads = Downloads(tmp_path / "models", downloaded=downloaded)
        project = store.create(LOCAL_USER, "Pictures")
        events: list[dict] = []
        ctx = ToolContext(
            project=project, sessions=store.db(project), user=user, session_id=1,
            emit=events.append, runtime=runtime, downloads=made_by.downloads, images=made_by,
        )  # fmt: skip
        ctx.events = events  # type: ignore[attr-defined]
        return ctx

    return make


def _params(**changes):  # noqa: ANN202
    return GENERATE.validate({"prompt": "a red lighthouse on a cliff at dusk", **changes})


# --- the card ----------------------------------------------------------------------------


def test_the_card_names_the_model(image_ctx):
    ctx = image_ctx()
    text = GENERATE.confirm_text(ctx, _params())
    # The size and wait are each shape's, beside it on the card (the shape can change)
    assert text == "Make a picture with FLUX.2 klein 4B?"


def test_the_card_says_what_downloads_first(image_ctx):
    ctx = image_ctx(downloaded=False)
    text = GENERATE.confirm_text(ctx, _params())
    assert text.endswith(f"First FLUX.2 klein 4B downloads ({klein().size / 1e9:.1f} GB).")


def test_someone_who_cant_download_is_told_before_any_card(image_ctx):
    ctx = image_ctx(downloaded=False, user=User(id=2, name="m", role=MEMBER))
    with pytest.raises(ToolError, match="only an admin can download models"):
        GENERATE.confirm_text(ctx, _params())


def test_the_card_lets_the_person_change_the_prompt_and_the_shape(image_ctx):
    ctx = image_ctx()
    prompt, shape = GENERATE.confirm_fields(ctx, _params(shape="portrait"))
    assert prompt == {
        "name": "prompt",
        "kind": "text",
        "value": "a red lighthouse on a cliff at dusk",
    }
    assert shape["name"] == "shape" and shape["value"] == "portrait"
    sizes = {c["value"]: (c["width"], c["height"]) for c in shape["choices"]}
    assert sizes == {
        "square": (768, 768),
        "portrait": (656, 880),
        "landscape": (880, 656),
        "large": (1024, 1024),
    }
    waits = {c["value"]: c["seconds"] for c in shape["choices"]}
    assert waits["large"] > waits["square"] > 0


@pytest.mark.parametrize(
    ("seconds", "said"),
    [(3, "about 5 s"), (42, "about 40 s"), (95, "about 2 min"), (600, "about 10 min")],
)
def test_the_wait_is_said_roughly(seconds, said):
    assert wait_text(seconds) == said


def test_no_image_service_is_a_tool_error(image_ctx):
    ctx = image_ctx()
    ctx.images = None
    with pytest.raises(ToolError, match="isn't available"):
        GENERATE.confirm_text(ctx, _params())


# --- running it --------------------------------------------------------------------------


def test_a_picture_is_saved_to_images_from_the_chat_and_opened(image_ctx):
    ctx = image_ctx()
    result = asyncio.run(GENERATE.run(ctx, _params(name="Lighthouse at dusk!", seed=5)))

    assert result.text.startswith("Made images/lighthouse-at-dusk.png (768×768, seed 5)")
    assert result.action == {
        "type": "open_tab",
        "tab": {"kind": "artifact", "id": result.refs[0]["id"]},
    }
    png = (ctx.project.root / "images" / "lighthouse-at-dusk.png").read_bytes()
    assert png.startswith(b"\x89PNG")
    with ctx.sessions() as session:
        row = files.live(session)[0]
    assert (row.path, row.from_kind, row.from_id) == ("images/lighthouse-at-dusk.png", "session", 1)
    assert row.from_note == "seed 5: a red lighthouse on a cliff at dusk"
    progress = [e for e in ctx.events if e["type"] == "progress"]
    assert all(e["stoppable"] for e in progress)
    assert [e["done"] for e in progress if "done" in e] == [1, 2, 3, 4]


def test_a_picture_without_a_name_is_named_from_its_prompt(image_ctx):
    ctx = image_ctx()
    asyncio.run(GENERATE.run(ctx, _params(prompt="A cat / on a ../ roof, at night")))
    assert (ctx.project.root / "images" / "a-cat-on-a-roof-at.png").is_file()


def test_a_failed_picture_is_a_tool_error_and_saves_nothing(image_ctx):
    ctx = image_ctx()
    with pytest.raises(ToolError, match="load from file failed"):
        asyncio.run(GENERATE.run(ctx, _params(prompt="FAIL please")))
    assert not (ctx.project.root / "images").exists()


def test_the_model_downloads_first_when_it_isnt_there(image_ctx):
    ctx = image_ctx(downloaded=False)
    asyncio.run(GENERATE.run(ctx, _params()))
    assert ctx.downloads.started == ["flux2-klein-4b"]
    assert any(e.get("label") == "Downloading FLUX.2 klein 4B" for e in ctx.events)


def test_a_download_that_fails_says_so_and_makes_nothing(image_ctx):
    ctx = image_ctx(downloaded=False)
    ctx.downloads.fail, ctx.downloads.end = True, "failed"
    with pytest.raises(ToolError, match="didn't download: disk full"):
        asyncio.run(GENERATE.run(ctx, _params()))


# --- through the chat: the card's values, declining, Stop -----------------------------------


@pytest.fixture
def chat(store, tmp_path):
    def make(arguments: dict, then: str = "Done."):  # noqa: ANN202
        handle = FakeHandle([tool_reply("image_generate", arguments), text_reply(then)])
        runtime = make_runtime(handle)
        made_by, _ = images(tmp_path, runtime)
        made_by.downloads = Downloads(tmp_path / "models")
        manager = TurnManager(
            runtime, store, Registry([*CORE_TOOLS, GENERATE]), images=made_by,
            downloads=made_by.downloads,
        )  # fmt: skip
        project = store.create(LOCAL_USER, "Pictures")
        return manager, project, runtime

    return make


async def _card(manager, project) -> dict:  # noqa: ANN001
    key = (project.id, 1)
    for _ in range(500):
        await asyncio.sleep(0.01)
        events = manager.channel(key).events
        card = next((e for e in events if e["type"] == "confirm"), None)
        if card is not None:
            return card
    raise AssertionError("no card")


def _argv(tmp_path) -> list[str]:  # noqa: ANN001
    return json.loads((tmp_path / "bin" / "argv.json").read_text())


def _tool_result(store, project) -> str:  # noqa: ANN001
    with store.db(project)() as session:
        return session.scalars(select(MessageRow.content).where(MessageRow.role == "tool")).one()


def test_the_picture_is_made_with_what_the_person_left_on_the_card(chat, store, tmp_path):
    manager, project, _ = chat({"prompt": "a lighthouse", "shape": "square"})

    async def scenario() -> str:
        await manager.send(project, LOCAL_USER, 1, "Draw a lighthouse", [])
        card = await _card(manager, project)
        assert [f["name"] for f in card["fields"]] == ["prompt", "shape"]
        values = {"prompt": "a lighthouse in fog, oil painting", "shape": "landscape", "seed": "9"}
        assert manager.decide((project.id, 1), card["confirmation_id"], True, values)
        return await manager.wait((project.id, 1))

    assert asyncio.run(scenario()) == "done"
    argv = _argv(tmp_path)
    assert argv[argv.index("-p") + 1] == "a lighthouse in fog, oil painting"
    assert (argv[argv.index("-W") + 1], argv[argv.index("-H") + 1]) == ("880", "656")
    assert argv[argv.index("-s") + 1] != "9"  # only the card's own fields can be changed
    assert _tool_result(store, project).startswith(
        "Made images/a-lighthouse-in-fog-oil-painting.png"
    )


def test_a_value_the_tool_cant_take_is_an_error_and_nothing_runs(chat, store, tmp_path):
    manager, project, _ = chat({"prompt": "a lighthouse"})

    async def scenario() -> str:
        await manager.send(project, LOCAL_USER, 1, "Draw a lighthouse", [])
        card = await _card(manager, project)
        manager.decide((project.id, 1), card["confirmation_id"], True, {"shape": "hexagon"})
        return await manager.wait((project.id, 1))

    asyncio.run(scenario())
    assert _tool_result(store, project).startswith("Error: Invalid arguments for image.generate")
    assert not (tmp_path / "bin" / "argv.json").exists()


def test_declining_the_card_makes_nothing(chat, store, tmp_path):
    manager, project, _ = chat({"prompt": "a lighthouse"})

    async def scenario() -> str:
        await manager.send(project, LOCAL_USER, 1, "Draw a lighthouse", [])
        card = await _card(manager, project)
        manager.decide((project.id, 1), card["confirmation_id"], False)
        return await manager.wait((project.id, 1))

    asyncio.run(scenario())
    assert _tool_result(store, project).startswith("The person declined: image.generate")
    assert not (tmp_path / "bin" / "argv.json").exists()


def test_stop_ends_the_picture_and_the_turn_goes_on(chat, store, tmp_path):
    manager, project, runtime = chat({"prompt": "HANG a lighthouse"}, then="Stopped it.")
    key = (project.id, 1)

    async def scenario() -> str:
        await manager.send(project, LOCAL_USER, 1, "Draw a lighthouse", [])
        card = await _card(manager, project)
        manager.decide(key, card["confirmation_id"], True)
        for _ in range(1000):
            await asyncio.sleep(0.01)
            if any(e.get("done") == 1 for e in manager.channel(key).events):
                break
        assert not manager.stop_tool(key, "another_call")
        assert manager.stop_tool(key, "call_1")
        return await manager.wait(key)

    assert asyncio.run(scenario()) == "done"
    assert _tool_result(store, project).startswith("The person stopped image.generate")
    with store.db(project)() as session:
        reply = session.scalars(
            select(MessageRow.content).where(MessageRow.role == "assistant")
        ).all()
    assert reply[-1] == "Stopped it."  # the turn went on and answered
    assert runtime.state()["reserved"] == []
    assert not (project.root / "images").exists()
    assert not manager.stop_tool(key, "call_1")  # nothing running any more


def test_the_image_tool_is_offered_by_its_module():
    from lectern.modules import load_modules

    module = next(m for m in load_modules() if m.id == "image")
    assert module.tools(None) == [tools.GENERATE]
    assert shape_size  # imported for the sizes test above
