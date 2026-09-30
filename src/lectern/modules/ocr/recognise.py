"""Recognise one page: the second phase of OCR (blueprint: "Recognise").

The page's 300 dpi image is scaled down to what the model can take (`max_side`, from the
catalog), sent with the model's own prompt, and read back as Markdown with HTML tables. Token
log-probabilities are kept when the runner gives them: M3 turns the unsure ones into doubts.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx2

RECOGNISE_TIMEOUT = 600.0  # a slow CPU-only machine can take minutes on a dense page
# The whole reply wrapped in one code fence (```markdown … ```), which some models add.
FENCED = re.compile(r"\A```[a-zA-Z]*\n(.*)\n```\s*\Z", re.S)


@dataclass(frozen=True)
class PageText:
    text: str
    tokens: list[tuple[str, float]] | None  # (token, log-probability), if the runner gave them
    prompt_tokens: int | None
    completion_tokens: int | None
    seconds: float
    cut_off: bool  # the model ran out of context before it finished the page


class RecogniseFailed(Exception):
    """The page wasn't read. The message says why, for the person."""


def model_input(image: Path, max_side: int) -> bytes:
    """The page as a PNG whose longer side is at most `max_side`."""
    from PIL import Image

    with Image.open(image) as page:
        page.load()
        scaled = page.copy()
    scaled.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    scaled.save(out, format="PNG")
    return out.getvalue()


async def recognise(
    client: httpx2.AsyncClient, image: Path, *, prompt: str, max_side: int
) -> PageText:
    png = await asyncio.to_thread(model_input, image, max_side)
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "temperature": 0,
        "logprobs": True,
        "stream": False,
        # A model that reasons first (Chandra does) would otherwise have its whole reply,
        # page text included, filed as reasoning and leave the answer empty.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    started = time.monotonic()
    try:
        response = await client.post("/v1/chat/completions", json=body, timeout=RECOGNISE_TIMEOUT)
    except httpx2.HTTPError as exc:
        raise RecogniseFailed(f"The OCR model couldn't be reached ({exc}).") from None
    seconds = round(time.monotonic() - started, 2)
    if response.status_code != 200:
        raise RecogniseFailed(f"The OCR model answered {response.status_code}: {_detail(response)}")
    try:
        choice = response.json()["choices"][0]
        text = choice["message"].get("content") or ""
        usage = response.json().get("usage") or {}
    except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise RecogniseFailed("The OCR model's reply wasn't readable.") from None
    return PageText(
        text=_unwrap(text),
        tokens=_tokens(choice.get("logprobs")),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        seconds=seconds,
        cut_off=choice.get("finish_reason") == "length",
    )


def _unwrap(text: str) -> str:
    text = text.strip()
    fenced = FENCED.match(text)
    return fenced.group(1).strip() if fenced else text


def _tokens(logprobs: object) -> list[tuple[str, float]] | None:
    if not isinstance(logprobs, dict) or not isinstance(logprobs.get("content"), list):
        return None
    try:
        return [(t["token"], float(t["logprob"])) for t in logprobs["content"]]
    except (KeyError, TypeError, ValueError):
        return None


def _detail(response: httpx2.Response) -> str:
    try:
        error = response.json().get("error")
        return error.get("message", str(error)) if isinstance(error, dict) else str(error)
    except (ValueError, AttributeError):
        return response.text[:200] or "no details"
