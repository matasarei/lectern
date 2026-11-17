"""The Image toggle: the person asks for a picture with their message. The tool runs first, the
model writing only the prompt; the prompt gate rewrites it and the card asks, as for any
picture, so the person approves the prompt that will run."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from lectern.agent.core_tools import CORE_TOOLS
from lectern.agent.tools import Registry
from lectern.agent.turns import TurnManager
from lectern.core.auth import LOCAL_USER
from lectern.hardware.tiers import Tier
from lectern.modules.image.tools import GENERATE
from lectern.projects.tables import MessageRow
from lectern.runtime.supervisor import ModelChoice, ModelRuntime

from ...agent.fake_model import SHAPE, FakeHandle, FakeRunner, make_runtime, text_reply
from .fakes import gate_reply, images
from .test_tools import Downloads

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake sd-cli is a script")

IMAGE = {"name": "image.generate", "arguments": {"shape": "landscape"}}


def json_reply(content: dict) -> bytes:
    return json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode()


@pytest.fixture
def chat(store, tmp_path):
    def make(replies, *, downloaded: bool = True, runtime=None):  # noqa: ANN001, ANN202
        handle = FakeHandle(replies)
        runtime = runtime(handle) if runtime else make_runtime(handle)
        made_by, _ = images(tmp_path, runtime)
        made_by.downloads = Downloads(tmp_path / "models", downloaded=downloaded)
        manager = TurnManager(
            runtime, store, Registry([*CORE_TOOLS, GENERATE]), images=made_by,
            downloads=made_by.downloads,
        )  # fmt: skip
        project = store.create(LOCAL_USER, "Pictures")
        return manager, project, handle

    return make


def _transcript(store, project) -> list[tuple[str, str, str | None]]:  # noqa: ANN001
    with store.db(project)() as session:
        rows = session.scalars(select(MessageRow).order_by(MessageRow.id)).all()
    return [(r.role, r.content, r.tool_calls) for r in rows]


def _argv(tmp_path) -> list[str]:  # noqa: ANN001
    return json.loads((tmp_path / "bin" / "argv.json").read_text())


async def _send(manager, project, text, force, approve=True, attachments=()):  # noqa: ANN001, ANN202
    """Send, approve the picture's card when one comes, and wait for the turn."""
    key = (project.id, 1)
    await manager.send(project, LOCAL_USER, 1, text, list(attachments), force)
    for _ in range(500):
        card = next((e for e in manager.channel(key).events if e["type"] == "confirm"), None)
        if card is not None:
            manager.decide(key, card["confirmation_id"], approve)
            break
        if manager.state(key) == "idle":
            break
        await asyncio.sleep(0.01)
    return await manager.wait(key)


def _card(manager, project) -> dict | None:  # noqa: ANN001
    return next(
        (e for e in manager.channel((project.id, 1)).events if e["type"] == "confirm"), None
    )


def test_the_toggle_makes_one_picture_after_its_card_from_a_prompt_held_to_a_schema(
    chat, store, tmp_path
):
    manager, project, handle = chat(
        [
            json_reply({"prompt": "a lighthouse in fog, oil painting"}),
            gate_reply("A white lighthouse in thick fog, oil painting, grey dawn light."),
            text_reply("Here it is."),
        ]
    )

    status = asyncio.run(_send(manager, project, "a lighthouse in the fog", IMAGE))

    assert status == "done"
    card = _card(manager, project)
    assert card is not None and card["text"] == "Make a picture with FLUX.2 klein 4B?"
    assert card["fields"][0]["value"] == (
        "A white lighthouse in thick fog, oil painting, grey dawn light."
    )
    argv = _argv(tmp_path)
    assert argv[argv.index("-p") + 1] == (
        "A white lighthouse in thick fog, oil painting, grey dawn light."
    )
    assert (argv[argv.index("-W") + 1], argv[argv.index("-H") + 1]) == ("880", "656")
    asked = handle.requests[0]
    assert asked["response_format"]["json_schema"]["schema"] == {
        "type": "object",
        "properties": {"prompt": {k: v for k, v in GENERATE.params.model_json_schema()[
            "properties"]["prompt"].items() if k != "title"}},
        "required": ["prompt"],
    }  # fmt: skip
    assert (
        "tools" not in asked
        and "Write the arguments for image.generate" in (asked["messages"][-1]["content"])
    )
    roles = [(role, content) for role, content, _ in _transcript(store, project)]
    assert [r for r, _ in roles] == ["user", "assistant", "tool", "assistant"]
    call = json.loads(_transcript(store, project)[1][2])[0]
    assert call["function"]["name"] == "image_generate"
    assert json.loads(call["function"]["arguments"]) == {
        "prompt": "a lighthouse in fog, oil painting",
        "shape": "landscape",
    }
    assert roles[2][1].startswith("Made images/")
    assert roles[3][1] == "Here it is."
    assert (project_images := project.root / "images").is_dir() and any(project_images.iterdir())


def test_the_card_says_what_downloads_first_and_declining_makes_nothing(chat, store, tmp_path):
    manager, project, _ = chat(
        [json_reply({"prompt": "a lighthouse"}), gate_reply("A lighthouse."), text_reply("No.")],
        downloaded=False,
    )

    status = asyncio.run(_send(manager, project, "a lighthouse", IMAGE, approve=False))

    assert status == "done"
    card = _card(manager, project)
    assert "First FLUX.2 klein 4B downloads (" in card["text"]
    assert [f["name"] for f in card["fields"]] == ["prompt", "shape"]
    assert _transcript(store, project)[2][1].startswith("The person declined: image.generate")
    assert not (tmp_path / "bin" / "argv.json").exists()


def test_only_a_tool_that_allows_it_can_be_asked_for_directly(chat, store):
    manager, project, handle = chat([])

    status = asyncio.run(
        _send(manager, project, "delete it", {"name": "artifact.trash", "arguments": {}})
    )

    assert status == "error"
    errors = [e for e in manager.channel((project.id, 1)).events if e["type"] == "error"]
    assert errors[0]["detail"] == "artifact.trash can't be asked for directly here."
    assert handle.requests == []  # nothing was asked of the model, nothing ran
    assert [r for r, _, _ in _transcript(store, project)] == ["user"]


def test_an_answer_that_isnt_the_arguments_ends_the_turn_with_a_reason(chat, store, tmp_path):
    bad = json.dumps({"choices": [{"message": {"content": "a lighthouse"}}]}).encode()
    manager, project, _ = chat([bad])

    assert asyncio.run(_send(manager, project, "a lighthouse", IMAGE)) == "error"
    errors = [e for e in manager.channel((project.id, 1)).events if e["type"] == "error"]
    assert errors[0]["detail"] == "The model didn't write the arguments for image.generate."
    assert not (tmp_path / "bin" / "argv.json").exists()


def test_on_mlx_the_typed_text_is_the_prompt(chat, store, tmp_path):
    def mlx_runtime(handle):  # noqa: ANN001, ANN202
        handle.model_id = "qwen3.5-4b-mlx"
        return ModelRuntime(
            budget=8 * 1024**3, unified=True, tier=Tier.MINIMUM,
            resolve=lambda role: ModelChoice(
                role, "qwen3.5-4b-mlx", Path("/m/q"), 2 * 1024**3, SHAPE, 8192, format="mlx"
            ),
            runner_for=lambda choice: FakeRunner(handle),
        )  # fmt: skip

    manager, project, handle = chat(
        [gate_reply("A red barn in deep snow, winter morning."), text_reply("Here it is.")],
        runtime=mlx_runtime,
    )

    assert asyncio.run(_send(manager, project, "a red barn in snow", IMAGE)) == "done"
    argv = _argv(tmp_path)
    assert argv[argv.index("-p") + 1] == "A red barn in deep snow, winter morning."
    gate = handle.requests[0]  # no schema call; the gate's plain one, which MLX can answer
    assert "response_format" not in gate
    assert gate["messages"][1]["content"].startswith("The request: a red barn in snow\n")
    assert len(handle.requests) == 2
