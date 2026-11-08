"""The chat's translation tool: `translate` translates text on this computer, with the model of
a language an admin added (languages.py `choose`), or else with the chat model.

The translation is the turn's answer as it stands (`ToolResult.answer`): the chat model doesn't
copy it into a reply of its own, so it can't reword it, and on a small GPU it isn't loaded
again just for that.
"""

from __future__ import annotations

from functools import partial

from pydantic import BaseModel, Field

from lectern.agent import model_download
from lectern.agent.tools import AgentTool, ToolContext, ToolError, ToolResult
from lectern.catalog.catalog import CatalogEntry
from lectern.catalog.languages import name
from lectern.modules.translate.languages import Added, choose
from lectern.runners.base import RunnerError
from lectern.runtime.supervisor import NotAvailable, Priority

CHUNK_CHARS = 3000  # of the source per request: with its translation, within a 4k context
MAX_TOKENS = 2048  # of one part's translation
TEXT_CHARS = 20_000
CODE = r"^[a-z]{2,3}$"

# The chat model's own: a general model, told to translate and nothing else.
CHAT_SYSTEM = (
    "You are a translator. Reply with the translation only: no notes, no quotes around it, "
    "nothing before or after it. Keep the formatting: line breaks, Markdown, lists, numbers."
)
CHAT_PROMPT = "Translate the following text from {source} into {target}.\n\n{text}"


class TranslateParams(BaseModel):
    source: str = Field(
        pattern=CODE, description='The language the text is in, as an ISO 639-1 code: "uk".'
    )
    target: str = Field(
        pattern=CODE, description='The language to translate it into, as an ISO 639-1 code: "en".'
    )
    text: str = Field(
        min_length=1,
        max_length=TEXT_CHARS,
        description="The text to translate, copied exactly as the person gave it.",
    )


def chunks(text: str, limit: int | None = None) -> list[str]:
    """The text in parts of at most `limit` characters (CHUNK_CHARS unless given), cut between
    paragraphs, else between sentences or lines, else anywhere: a part is translated in one
    request."""
    limit = limit or CHUNK_CHARS
    parts: list[str] = []
    current = ""
    for paragraph in (p for p in text.strip().split("\n\n") if p.strip()):
        for piece in _pieces(paragraph, limit):
            if current and len(current) + 2 + len(piece) > limit:
                parts.append(current)
                current = ""
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        parts.append(current)
    return parts


def _pieces(paragraph: str, limit: int) -> list[str]:
    """One paragraph, cut where it has to be."""
    pieces: list[str] = []
    rest = paragraph
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(mark) for mark in (". ", "! ", "? ", "\n"))
        end = cut + 1 if cut > limit // 2 else limit
        pieces.append(rest[:end].strip())
        rest = rest[end:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def fill(prompt: str, source: str, target: str, text: str) -> str:
    """A model's prompt with the languages' names and the text. Not str.format: the text may
    have braces of its own, and is put in last so nothing in it is replaced."""
    filled = prompt.replace("{source}", name(source)).replace("{target}", name(target))
    return filled.replace("{text}", text)


async def _ask(client, messages: list[dict]) -> str:  # noqa: ANN001
    response = await client.post(
        "/v1/chat/completions",
        json={
            "messages": messages,
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    if response.status_code != 200:
        raise ToolError(f"The translation failed: {response.text[:300]}")
    choice = response.json()["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ToolError("A part of the text was too long to translate in one go.")
    return (choice["message"].get("content") or "").strip()


def _messages(entry: CatalogEntry | None, source: str, target: str, part: str) -> list[dict]:
    if entry is None:
        return [
            {"role": "system", "content": CHAT_SYSTEM},
            {"role": "user", "content": fill(CHAT_PROMPT, source, target, part)},
        ]
    return [{"role": "user", "content": fill(entry.translate.prompt, source, target, part)}]


async def translate_parts(
    ctx: ToolContext, entry: CatalogEntry | None, source: str, target: str, parts: list[str]
) -> str:
    """Each part translated in turn, with progress in the chat, by the language's model or,
    with `entry` None, the chat model."""
    key = f"translate@{entry.id}" if entry is not None else "main"
    done: list[str] = []
    async with ctx.runtime.lease(key, Priority.CHAT) as lease:
        for number, part in enumerate(parts, start=1):
            event = {"type": "progress", "label": f"Translating with {lease.model_id}"}
            if len(parts) > 1:
                event.update(done=number - 1, total=len(parts))
            ctx.emit(event)
            done.append(await _ask(lease.client, _messages(entry, source, target, part)))
    return "\n\n".join(done)


async def _translate(
    added: tuple[Added, ...], ctx: ToolContext, params: TranslateParams
) -> ToolResult:
    if params.source == params.target:
        raise ToolError("The text is already in that language: source and target are the same.")
    if ctx.runtime is None:
        raise ToolError("Translation isn't available here.")
    choice = choose(params.source, params.target, added, partial(model_download.on_disk, ctx))
    parts = chunks(params.text)
    if not parts:
        raise ToolError("There's no text to translate.")
    entry, why = choice.entry, choice.why
    if entry is not None:
        try:
            translation = await translate_parts(ctx, entry, params.source, params.target, parts)
        except (NotAvailable, RunnerError) as exc:
            # The language's model couldn't start (too big for the memory left, or its server
            # didn't): the chat model translates instead, and says why.
            reason = (str(exc).splitlines() or [""])[0] or type(exc).__name__
            entry, why = None, f"{choice.entry.title} couldn't start: {reason}"
    if entry is None:
        translation = await translate_parts(ctx, None, params.source, params.target, parts)
    by = entry.title if entry is not None else "the chat model"
    text = (
        f"Translated from {name(params.source)} into {name(params.target)} by {by}"
        + (f" ({why})" if why else "")
        + ". The person sees the translation as your answer."
    )
    return ToolResult(text, answer=translation)


def translate_tool(added: tuple[Added, ...]) -> AgentTool:
    """The tool for one turn, with the languages added when the turn began."""
    return AgentTool(
        name="translate",
        description=(
            "Translate text into another language, on this computer. Use it whenever the "
            "person asks for a translation; don't translate yourself. Its translation is shown "
            "to the person as your answer."
        ),
        params=TranslateParams,
        run=partial(_translate, added),
        max_tokens=200,
    )
