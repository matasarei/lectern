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
from .fakes import gate_reply, images, klein

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
    def make(
        *, downloaded: bool = True, user: User = LOCAL_USER, replies: list[bytes] | None = None
    ) -> ToolContext:
        handle = FakeHandle(list(replies or []))
        runtime = make_runtime(handle, per_role=True)
        made_by, _ = images(tmp_path, runtime)
        made_by.downloads = Downloads(tmp_path / "models", downloaded=downloaded)
        project = store.create(LOCAL_USER, "Pictures")
        events: list[dict] = []
        ctx = ToolContext(
            project=project, sessions=store.db(project), user=user, session_id=1,
            emit=events.append, runtime=runtime, downloads=made_by.downloads, images=made_by,
        )  # fmt: skip
        ctx.events = events  # type: ignore[attr-defined]
        ctx.handle = handle  # type: ignore[attr-defined]
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
    def make(arguments: dict, then: str = "Done.", rewritten: str | None = None):  # noqa: ANN202
        # The model's call, the prompt gate's rewrite before the card, the reply after it
        rewritten = rewritten or f"{arguments['prompt']}, rewritten"
        handle = FakeHandle(
            [tool_reply("image_generate", arguments), gate_reply(rewritten), text_reply(then)]
        )
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
        assert card["fields"][0]["value"] == "a lighthouse, rewritten"  # the gate's, to approve
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


# --- reference pictures and the prompt gate ----------------------------------------------------


def _photo(width: int = 640, height: int = 480) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (120, 80, 40)).save(out, format="JPEG")
    return out.getvalue()


def _add_photo(ctx, name: str = "cat.jpg") -> int:  # noqa: ANN001
    with ctx.sessions.begin() as session:
        return files.write_bytes(
            ctx.project.root, session, owner_id=LOCAL_USER.id, name=name, data=_photo(),
            origin=files.Origin("import"), folder="photos",
        ).id  # fmt: skip


class _VisionMissing(Downloads):
    """The image model is on disk; the vision model that reads references isn't."""

    def is_downloaded(self, entry) -> bool:  # noqa: ANN001
        return "ocr.hand" not in entry.roles


def _prepare(ctx, **params):  # noqa: ANN001, ANN003, ANN202
    return asyncio.run(GENERATE.prepare(ctx, _params(**params)))


def _roles(ctx) -> list[str]:  # noqa: ANN001
    return [r["role"] for r in ctx.runtime.state()["residents"]]


def test_the_prompt_is_rewritten_before_the_card_by_the_chats_model(image_ctx):
    ctx = image_ctx(replies=[gate_reply("A red lighthouse on a cliff at dusk, oil painting.")])

    prepared = _prepare(ctx)

    assert prepared.prompt == "A red lighthouse on a cliff at dusk, oil painting."
    assert prepared.references == []
    assert _roles(ctx) == ["main"]
    guide = klein().image.prompt_guide
    assert guide in ctx.handle.requests[0]["messages"][0]["content"]
    assert GENERATE.confirm_text(ctx, prepared) == "Make a picture with FLUX.2 klein 4B?"


def test_a_failed_rewrite_keeps_the_prompt_and_the_card_says_why(image_ctx):
    ctx = image_ctx(replies=[gate_reply("")])

    prepared = _prepare(ctx)

    assert prepared.prompt == "a red lighthouse on a cliff at dusk"
    assert GENERATE.confirm_text(ctx, prepared).endswith(
        "The prompt wasn't rewritten for the image model: the model's rewrite was empty."
    )


def test_named_references_are_seen_by_the_vision_model_and_offered_on_the_card(image_ctx):
    ctx = image_ctx(replies=[gate_reply("The cat from image 1 as a watercolour.")])
    artifact_id = _add_photo(ctx)

    prepared = _prepare(ctx, references=["photos/CAT.jpg"])

    assert prepared.references == ["photos/cat.jpg"]  # as it is stored
    assert prepared.prompt == "The cat from image 1 as a watercolour."
    assert _roles(ctx) == ["ocr.hand"]
    parts = ctx.handle.requests[0]["messages"][1]["content"]
    assert [p["type"] for p in parts] == ["image_url", "text"]
    fields = GENERATE.confirm_fields(ctx, prepared)
    assert [f["name"] for f in fields] == ["prompt", "references", "shape"]
    assert fields[1] == {
        "name": "references",
        "kind": "images",
        "value": ["photos/cat.jpg"],
        "choices": [
            {
                "value": "photos/cat.jpg",
                "artifact_id": artifact_id,
                "url": f"/api/projects/{ctx.project.id}/artifacts/{artifact_id}/content",
            }
        ],
    }
    ctx.prepared = None  # as if prepare had found none
    without = GENERATE.confirm_fields(ctx, prepared.model_copy(update={"references": []}))
    plain = {c["value"]: c["seconds"] for c in without[-1]["choices"]}
    waits = {c["value"]: c["seconds"] for c in fields[-1]["choices"]}
    assert waits["square"] > plain["square"]  # references lengthen the wait


def test_a_reference_that_isnt_there_is_refused_before_any_card(image_ctx):
    ctx = image_ctx()
    with pytest.raises(ToolError, match="No picture called 'photos/dog.jpg'"):
        _prepare(ctx, references=["photos/dog.jpg"])
    assert ctx.handle.requests == []


def test_more_references_than_a_call_takes_are_refused_by_the_arguments():
    with pytest.raises(ToolError, match="Invalid arguments"):
        _params(references=[f"photos/{n}.jpg" for n in range(tools.MAX_REFERENCES + 1)])


def test_a_missing_vision_model_is_asked_for_and_without_it_the_pictures_still_go(image_ctx):
    ctx = image_ctx(replies=[gate_reply("The cat from image 1 in a garden.")])
    ctx.downloads = ctx.images.downloads = _VisionMissing(ctx.project.root)
    asked: list[str] = []

    async def say_no(question: str, kind: str = "allow") -> bool:
        asked.append(question)
        return False

    ctx.confirm = say_no
    _add_photo(ctx)

    prepared = _prepare(ctx, references=["photos/cat.jpg"])

    assert asked and asked[0].startswith("Reading the reference pictures needs Qwen3-VL")
    assert _roles(ctx) == ["main"]  # the chat's model wrote it, by the pictures' numbers
    assert "you can't see them" in ctx.handle.requests[0]["messages"][1]["content"]
    assert prepared.references == ["photos/cat.jpg"]
    assert "The pictures weren't looked at to write the prompt" in GENERATE.confirm_text(
        ctx, prepared
    )


def test_someone_who_cant_download_the_vision_model_isnt_asked(image_ctx):
    ctx = image_ctx(
        user=User(id=LOCAL_USER.id, name="m", role=MEMBER),
        replies=[gate_reply("The cat from image 1 in a garden.")],
    )
    ctx.downloads = ctx.images.downloads = _VisionMissing(ctx.project.root)
    _add_photo(ctx)

    prepared = _prepare(ctx, references=["photos/cat.jpg"])

    assert "only an admin can download models" in GENERATE.confirm_text(ctx, prepared)


def test_on_a_model_that_takes_no_references_they_are_described(image_ctx, monkeypatch):
    monkeypatch.setattr(tools, "_model", lambda ctx: klein("z-image-turbo"))
    ctx = image_ctx(replies=[gate_reply("A ginger cat with white paws in a garden.")])
    _add_photo(ctx)

    prepared = _prepare(ctx, references=["photos/cat.jpg"])

    text = ctx.handle.requests[0]["messages"][1]["content"][-1]["text"]
    assert "describe in the prompt everything" in text
    assert "described in the prompt, not copied" in GENERATE.confirm_text(ctx, prepared)


def test_on_a_model_that_takes_no_references_unseen_ones_are_refused(image_ctx, monkeypatch):
    monkeypatch.setattr(tools, "_model", lambda ctx: klein("z-image-turbo"))
    ctx = image_ctx()
    ctx.downloads = ctx.images.downloads = _VisionMissing(ctx.project.root)
    _add_photo(ctx)

    with pytest.raises(ToolError, match="can't be given reference pictures"):
        _prepare(ctx, references=["photos/cat.jpg"])


def test_a_picture_made_from_references_passes_them_to_sd_cli_and_says_so(image_ctx, tmp_path):
    ctx = image_ctx()
    _add_photo(ctx)

    result = asyncio.run(GENERATE.run(ctx, _params(references=["photos/cat.jpg"], seed=3)))

    assert result.text.startswith("Made images/")
    seen = json.loads((tmp_path / "bin" / "refs.json").read_text())
    width, height = seen[0]["size"]
    assert width * height <= 768 * 768 and abs(width / height - 4 / 3) < 0.05
    with ctx.sessions() as session:
        made = [r for r in files.live(session) if r.path.startswith("images/")][0]
    assert made.from_note == "seed 3, from photos/cat.jpg: a red lighthouse on a cliff at dusk"


def _send_with(manager, project, text, attachments, values=None, approve=True):  # noqa: ANN001, ANN202
    async def scenario():  # noqa: ANN202
        await manager.send(project, LOCAL_USER, 1, text, list(attachments))
        card = await _card(manager, project)
        manager.decide((project.id, 1), card["confirmation_id"], approve, values)
        await manager.wait((project.id, 1))
        return card

    return asyncio.run(scenario())


def _add_to(store, project, name: str, data: bytes) -> int:  # noqa: ANN001
    with store.db(project).begin() as session:
        return files.write_bytes(
            project.root, session, owner_id=LOCAL_USER.id, name=name, data=data,
            origin=files.Origin("import"), folder="photos",
        ).id  # fmt: skip


def test_the_pictures_attached_to_the_message_are_the_references(chat, store, tmp_path):
    manager, project, _ = chat({"prompt": "my cat as a watercolour"})
    photo = _add_to(store, project, "cat.jpg", _photo())
    notes = _add_to(store, project, "notes.txt", b"not a picture")

    card = _send_with(manager, project, "my cat as a watercolour", [photo, notes])

    refs = next(f for f in card["fields"] if f["name"] == "references")
    assert refs["value"] == ["photos/cat.jpg"]  # the text file isn't a picture
    argv = _argv(tmp_path)
    assert argv.count("-r") == 1


def test_a_reference_taken_off_the_card_isnt_used(chat, store, tmp_path):
    manager, project, _ = chat({"prompt": "my cat", "references": ["photos/cat.jpg"]})
    _add_to(store, project, "cat.jpg", _photo())

    _send_with(manager, project, "my cat", [], values={"references": []})

    assert "-r" not in _argv(tmp_path)
    assert _tool_result(store, project).startswith("Made images/")


def test_the_card_cant_add_a_picture_it_didnt_show(chat, store, tmp_path):
    manager, project, _ = chat({"prompt": "my cat", "references": ["photos/cat.jpg"]})
    _add_to(store, project, "cat.jpg", _photo())
    _add_to(store, project, "dog.jpg", _photo())

    _send_with(manager, project, "my cat", [], values={"references": ["photos/dog.jpg"]})

    assert _tool_result(store, project) == (
        "Error: references can only keep pictures the card showed."
    )
    assert not (tmp_path / "bin" / "argv.json").exists()


def test_a_text_field_cant_be_sent_back_as_a_list(chat, store, tmp_path):
    manager, project, _ = chat({"prompt": "a lighthouse"})

    _send_with(manager, project, "a lighthouse", [], values={"prompt": ["a", "b"]})

    assert _tool_result(store, project) == "Error: prompt takes a text."
    assert not (tmp_path / "bin" / "argv.json").exists()
