from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path

import httpx2
import pytest
from PIL import Image

from lectern.modules.ocr.recognise import RecogniseFailed, recognise


def _page(tmp_path: Path, size=(2480, 3508)) -> Path:
    path = tmp_path / "p001.png"
    Image.new("L", size, 255).save(path)
    return path


def _reply(text="# Договір\n\nОрендар сплачує", *, finish="stop", logprobs=True) -> dict:
    choice = {"message": {"content": text}, "finish_reason": finish}
    if logprobs:
        tokens = [{"token": "#", "logprob": -0.01}, {"token": " Д", "logprob": -1.5}]
        choice["logprobs"] = {"content": tokens}
    return {"choices": [choice], "usage": {"prompt_tokens": 1800, "completion_tokens": 40}}


def _client(
    *responses: httpx2.Response | Exception, seen: list | None = None
) -> httpx2.AsyncClient:
    queue = list(responses)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0)
        if seen is not None:
            seen.append(json.loads(request.content))
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx2.AsyncClient(base_url="http://ocr", transport=httpx2.MockTransport(handler))


def _read(client, page, **kw):  # noqa: ANN001, ANN202
    return asyncio.run(
        recognise(client, page, prompt=kw.get("prompt", "OCR:"), max_side=kw.get("max_side", 1280))
    )


def test_a_page_is_sent_scaled_down_with_the_models_prompt(tmp_path: Path):
    seen: list[dict] = []
    client = _client(httpx2.Response(200, json=_reply()), seen=seen)

    page = _read(client, _page(tmp_path), prompt="OCR:", max_side=1280)

    (body,) = seen
    image_part, text_part = body["messages"][0]["content"]
    assert text_part == {"type": "text", "text": "OCR:"}
    assert body["temperature"] == 0 and body["logprobs"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    png = base64.b64decode(image_part["image_url"]["url"].removeprefix("data:image/png;base64,"))
    with Image.open(io.BytesIO(png)) as sent:
        assert max(sent.size) == 1280 and sent.size == (905, 1280)  # A4's shape kept
    assert page.text == "# Договір\n\nОрендар сплачує"
    assert page.tokens == [("#", -0.01), (" Д", -1.5)]
    assert (page.prompt_tokens, page.completion_tokens, page.cut_off) == (1800, 40, False)


def test_a_small_image_is_not_enlarged(tmp_path: Path):
    seen: list[dict] = []
    _read(_client(httpx2.Response(200, json=_reply()), seen=seen), _page(tmp_path, (600, 800)))

    url = seen[0]["messages"][0]["content"][0]["image_url"]["url"]
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as sent:
        assert sent.size == (600, 800)


def test_no_logprobs_and_a_cut_off_page_are_reported(tmp_path: Path):
    reply = _reply("partial", finish="length", logprobs=False)

    page = _read(_client(httpx2.Response(200, json=reply)), _page(tmp_path))

    assert page.tokens is None and page.cut_off is True and page.text == "partial"


def test_a_reply_wrapped_in_a_code_fence_is_unwrapped(tmp_path: Path):
    reply = _reply("```markdown\n# Title\n\n| a |\n```\n")

    assert (
        _read(_client(httpx2.Response(200, json=reply)), _page(tmp_path)).text == "# Title\n\n| a |"
    )


def test_a_blank_page_is_read_as_empty_not_failed(tmp_path: Path):
    assert _read(_client(httpx2.Response(200, json=_reply(""))), _page(tmp_path)).text == ""


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (httpx2.Response(500, json={"error": {"message": "too big"}}), "answered 500: too big"),
        (httpx2.Response(200, content=b"not json"), "reply wasn't readable"),
        (httpx2.Response(200, json={"choices": []}), "reply wasn't readable"),
        (httpx2.ConnectError("refused"), "couldn't be reached"),
    ],
)  # fmt: skip
def test_a_failed_read_says_why(tmp_path: Path, answer, message):  # noqa: ANN001
    with pytest.raises(RecogniseFailed, match=message):
        _read(_client(answer), _page(tmp_path))
