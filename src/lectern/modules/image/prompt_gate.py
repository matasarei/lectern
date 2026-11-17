"""The prompt gate: the chat's prompt for a picture, rewritten the way the image model wants
it, before the card shows it to the person (`image.generate`'s `prepare`).

The chat's model writes its prompt with the whole conversation and every tool in view; one
call that sees only the request and the image model's own guide (`ImageSettings.prompt_guide`
in the catalog) writes a better one. With reference pictures the call goes to the vision model
(the `ocr.hand` role), which sees them and can say what to take from each; without, to the
chat's model. The reply is plain text, not held to a schema, so it works on MLX too.

The gate never stands in the way of a picture: when it fails (no model, an error, a timeout,
an empty reply) the draft stays as it was and the card says why. The person can change the
prompt on the card either way.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

import httpx2
from PIL import Image

from lectern.modules.image.generate import Reference
from lectern.runtime.supervisor import ModelRuntime, NotAvailable, Priority

log = logging.getLogger(__name__)

TIMEOUT = 60.0  # seconds for the rewrite, the model's loading not included
MAX_TOKENS = 400
MAX_PROMPT = 1500  # GenerateParams.prompt's own limit
VISION_SIDE = 768  # the longest side of a reference shown to the vision model

# A model's guide says how it wants its prompt written; what to do with reference pictures is
# said here, and only when there are some: told about them with none given, the 4B wrote "take
# the style from image 1" into half its prompts (docs/acceptance/image.md, Part 3).
GENERAL_GUIDE = (
    "Write one paragraph of plain English prose, 30 to 80 words: the main subject first, then "
    "what it is doing, the style or medium, the setting, the light and the colours. Say what "
    "should be there, never what shouldn't. When the request asks for words in the picture, "
    "put them in quotes."
)

INSTRUCTIONS = (
    "You write the prompt an image model is given, from a request for a picture. {guide}\n\n"
    "Keep everything the request asks for: its subject, its style, any words in quotes. Where "
    "it leaves something open, choose it, so the picture is concrete. Add no words, letters "
    "or signs to the picture that the request doesn't ask for. Write in English whatever "
    "language the request is in. Reply with the prompt only: no heading, no quotes around it, "
    "no explanation."
)
NO_REFERENCES = "No reference pictures are given: mention none."

_FENCE = re.compile(r"^```[a-z]*\n?(.*?)\n?```$", re.S)
_THINKING = re.compile(r"<think>.*?</think>", re.S)
_LABEL = re.compile(r"^(?:\*\*)?(?:image prompt|prompt)(?:\*\*)?\s*:\s*(?:\*\*)?\s*", re.I)


@dataclass(frozen=True)
class Gated:
    """The prompt the card shows, and why it is still the draft when it is (None when the gate
    rewrote it)."""

    prompt: str
    kept_because: str | None = None


@dataclass(frozen=True)
class Shown:
    """A reference picture as the gate is told about it: its path, and its pixels when the
    model writing the prompt can see them."""

    path: str
    picture: Reference | None = None


async def rewrite(
    runtime: ModelRuntime | None,
    draft: str,
    *,
    guide: str | None,
    references: Sequence[Shown] = (),
    can_take_references: bool = True,
) -> Gated:
    """The draft rewritten by the image model's guide, or the draft and why it was kept."""
    if runtime is None:
        return Gated(draft, "no model could rewrite it here")
    see = any(r.picture is not None for r in references)
    messages = [
        {"role": "system", "content": INSTRUCTIONS.format(guide=guide or GENERAL_GUIDE)},
        {"role": "user", "content": _request(draft, references, see, can_take_references)},
    ]
    body = {
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    role = "ocr.hand" if see else "main"
    try:
        async with runtime.lease(role, Priority.CHAT) as lease:
            async with asyncio.timeout(TIMEOUT):
                response = await lease.client.post("/v1/chat/completions", json=body)
    except NotAvailable as exc:
        return Gated(draft, f"no model could rewrite it ({exc})")
    except TimeoutError:
        return Gated(draft, f"rewriting it took over {TIMEOUT:.0f} s")
    except httpx2.HTTPError as exc:
        log.warning("the prompt gate's model couldn't be reached: %s", exc)
        return Gated(draft, "the model rewriting it couldn't be reached")
    if response.status_code != 200:
        log.warning("the prompt gate's model answered %s", response.status_code)
        return Gated(draft, f"the model rewriting it answered {response.status_code}")
    try:
        text = response.json()["choices"][0]["message"].get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return Gated(draft, "the model's rewrite wasn't readable")
    prompt = clean(text)
    if len(prompt) < 3:
        return Gated(draft, "the model's rewrite was empty")
    return Gated(prompt)


def _request(
    draft: str, references: Sequence[Shown], see: bool, can_take_references: bool
) -> str | list[dict]:
    """The user message: the request, then the references by number, with their pixels when
    the model can see them."""
    lines = [f"The request: {draft}"]
    for number, reference in enumerate(references, start=1):
        lines.append(f"Image {number} is {reference.path}.")
    if not references:
        lines.append(NO_REFERENCES)
    elif can_take_references:
        lines.append(
            "Call them image 1, image 2 and so on in the prompt, and say what to take from each "
            "(the subject, the style, the layout or the background) and what should change."
        )
    if references and not can_take_references:
        lines.append(
            "This image model can't be given the reference pictures: describe in the prompt "
            "everything that must be taken from them."
        )
    elif references and not see:
        lines.append(
            "The image model is given these pictures; you can't see them. Refer to them by "
            "number, as the request does."
        )
    text = "\n".join(lines)
    if not see:
        return text
    parts: list[dict] = []
    for reference in references:
        if reference.picture is not None:
            parts.append({"type": "image_url", "image_url": {"url": _data_url(reference.picture)}})
    parts.append({"type": "text", "text": text})
    return parts


def _data_url(picture: Reference) -> str:
    """The reference as a PNG data URL, its longer side at most VISION_SIDE."""
    with Image.open(io.BytesIO(picture.png)) as image:
        image.load()
        small = image.copy()
    small.thumbnail((VISION_SIDE, VISION_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    small.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def clean(text: str) -> str:
    """The prompt from a reply: without a reasoning block, a code fence, a "Prompt:" label or
    the quotes around it, on one line, cut at a sentence's end to fit GenerateParams."""
    text = _THINKING.sub("", text).strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    text = _LABEL.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    if text[:1] == "“" and text[-1:] == "”":
        text = text[1:-1].strip()
    text = " ".join(text.split())
    if len(text) > MAX_PROMPT:
        cut = text[:MAX_PROMPT]
        end = cut.rfind(". ")
        text = cut[: end + 1] if end > MAX_PROMPT // 2 else cut.rstrip()
    return text
