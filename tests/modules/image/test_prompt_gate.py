from __future__ import annotations

import asyncio
import base64
import io
import json

import httpx2
import pytest
from PIL import Image

from lectern.modules.image import prompt_gate
from lectern.modules.image.generate import Reference
from lectern.modules.image.prompt_gate import Shown, clean, rewrite

from ...agent.fake_model import FakeHandle, make_runtime

GUIDE = "Subject first, 30 to 80 words."


def reply(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def _picture(width: int = 1600, height: int = 1200) -> Reference:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (1, 2, 3)).save(out, format="PNG")
    return Reference(out.getvalue(), width, height)


def _gate(handle: FakeHandle, draft: str = "a cat on a roof", **kwargs):  # noqa: ANN003, ANN202
    runtime = make_runtime(handle, per_role=True)
    gated = asyncio.run(rewrite(runtime, draft, guide=GUIDE, **kwargs))
    roles = [r["role"] for r in runtime.state()["residents"]]
    return gated, roles


def test_without_references_the_chats_model_rewrites_the_draft_by_the_guide():
    handle = FakeHandle([reply("A grey tabby cat sitting on a red tile roof at sunset.")])

    gated, roles = _gate(handle)

    assert gated.prompt == "A grey tabby cat sitting on a red tile roof at sunset."
    assert gated.kept_because is None
    assert roles == ["main"]
    body = handle.requests[0]
    assert GUIDE in body["messages"][0]["content"]
    assert body["messages"][1]["content"] == (
        "The request: a cat on a roof\nNo reference pictures are given: mention none."
    )
    assert body["temperature"] == 0 and body["stream"] is False
    assert "response_format" not in body  # plain text, so MLX can answer it too


def test_with_references_it_sees_follows_the_vision_model_and_gets_the_pictures():
    handle = FakeHandle([reply("The cat from image 1, painted in the style of image 2.")])
    shown = [Shown("photos/cat.jpg", _picture()), Shown("art/style.png", _picture(300, 300))]

    gated, roles = _gate(handle, "my cat as this painting", references=shown)

    assert gated.prompt.startswith("The cat from image 1")
    assert roles == ["ocr.hand"]
    parts = handle.requests[0]["messages"][1]["content"]
    assert [p["type"] for p in parts] == ["image_url", "image_url", "text"]
    first = parts[0]["image_url"]["url"]
    assert first.startswith("data:image/png;base64,")
    sent = Image.open(io.BytesIO(base64.b64decode(first.split(",", 1)[1])))
    assert max(sent.size) == prompt_gate.VISION_SIDE  # scaled down for the vision model
    assert "Image 1 is photos/cat.jpg.\nImage 2 is art/style.png." in parts[2]["text"]
    assert "say what to take from each" in parts[2]["text"]
    assert "mention none" not in parts[2]["text"]


def test_references_it_cant_see_are_named_by_number_for_the_chats_model():
    handle = FakeHandle([reply("The person from image 1 on a beach.")])

    gated, roles = _gate(handle, "her on a beach", references=[Shown("photos/me.jpg")])

    assert roles == ["main"]
    text = handle.requests[0]["messages"][1]["content"]
    assert "Image 1 is photos/me.jpg." in text and "you can't see them" in text
    assert gated.prompt == "The person from image 1 on a beach."


def test_a_model_that_cant_take_references_is_told_to_describe_them():
    handle = FakeHandle([reply("A ginger cat with white paws and green eyes, in a garden.")])
    shown = [Shown("photos/cat.jpg", _picture())]

    _gate(handle, "my cat in a garden", references=shown, can_take_references=False)

    text = handle.requests[0]["messages"][1]["content"][-1]["text"]
    assert "can't be given the reference pictures: describe" in text


class _Failing(FakeHandle):
    def __init__(self, *, status: int = 200, content: bytes = b"", error: bool = False) -> None:
        super().__init__([])
        self.status, self.content, self.error = status, content, error

    def client(self) -> httpx2.AsyncClient:
        def handler(request: httpx2.Request) -> httpx2.Response:
            if self.error:
                raise httpx2.ConnectError("refused")
            return httpx2.Response(self.status, content=self.content)

        return httpx2.AsyncClient(base_url=self.base_url, transport=httpx2.MockTransport(handler))


@pytest.mark.parametrize(
    ("handle", "reason"),
    [
        (_Failing(error=True), "couldn't be reached"),
        (_Failing(status=500, content=b"{}"), "answered 500"),
        (_Failing(content=b"not json"), "wasn't readable"),
        (_Failing(content=b'{"choices": []}'), "wasn't readable"),
        (FakeHandle([reply("  ")]), "was empty"),
        (FakeHandle([reply('""')]), "was empty"),
    ],
)
def test_a_failed_rewrite_keeps_the_draft_and_says_why(handle, reason):
    gated, _ = _gate(handle, "a lighthouse")
    assert gated.prompt == "a lighthouse"
    assert reason in gated.kept_because


def test_a_rewrite_that_takes_too_long_keeps_the_draft(monkeypatch):
    monkeypatch.setattr(prompt_gate, "TIMEOUT", 0.05)
    handle = FakeHandle([reply("never")], hold=asyncio.Event())
    gated, _ = _gate(handle, "a lighthouse")
    assert gated.prompt == "a lighthouse"
    assert "took over" in gated.kept_because


def test_no_runtime_keeps_the_draft():
    gated = asyncio.run(rewrite(None, "a lighthouse", guide=None))
    assert gated.prompt == "a lighthouse" and gated.kept_because


@pytest.mark.parametrize(
    ("text", "prompt"),
    [
        ('"A red barn in snow."', "A red barn in snow."),
        ("“A red barn in snow.”", "A red barn in snow."),
        ("Prompt: A red barn in snow.", "A red barn in snow."),
        ("**Prompt:** A red barn\nin snow.", "A red barn in snow."),
        ("```\nA red barn in snow.\n```", "A red barn in snow."),
        ("<think>hmm</think>\nA red barn in snow.", "A red barn in snow."),
        ('A sign that says "OPEN" above a door.', 'A sign that says "OPEN" above a door.'),
        # two quoted words at its ends are lettering, not quotes around the prompt
        ('"OPEN" in red above a door, and "DAY"', '"OPEN" in red above a door, and "DAY"'),
        ("'OPEN' above and 'DAY'", "'OPEN' above and 'DAY'"),
        ("“OPEN” above and “DAY”", "“OPEN” above and “DAY”"),
    ],
)
def test_the_prompt_is_taken_from_the_reply_without_its_wrapping(text, prompt):
    assert clean(text) == prompt


def test_a_long_reply_is_cut_at_a_sentence_to_fit():
    sentence = "A tall lighthouse stands on a rocky cliff above a grey sea. "
    cut = clean(sentence * 40)
    assert len(cut) <= prompt_gate.MAX_PROMPT
    assert cut.endswith("grey sea.")
