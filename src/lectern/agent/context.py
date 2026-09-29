"""Builds what one model call sees, within the model's context size.

In order, most stable first so the runner can reuse its cache: the system prompt and tools,
the pinned part (in M1a: the files attached in this chat), then the turns, newest last.
Source data is never summarised: an attached file goes in whole, or as a one-line stub
telling the model to read it with document_read.

When it doesn't fit, in this order:
1. tool results in earlier turns are released (replaced by a stub; calling the tool again
   brings the exact text back);
2. the oldest turns are left out (the transcript keeps them; the chat shows a marker);
3. tool results earlier in the current turn are released.
M1b replaces step 2 with summaries.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

ANSWER_RESERVE = 1536
PINNED_SHARE = 0.5  # of what's left after the prompt, tools and answer
TOOL_OVERHEAD_TOKENS = 4  # per message, for the chat template's markers


class ContextTooSmall(Exception):
    """Even the system prompt, tools and the current message don't fit."""


@dataclass(frozen=True)
class Attachment:
    artifact_id: int
    name: str
    text: str  # the whole file text ("" when it has none)
    note: str | None = None  # why there's no text


@dataclass
class Built:
    messages: list[dict]
    left_out: int = 0  # older messages not sent this time
    released: int = 0  # tool results replaced by stubs
    stubbed_files: list[str] = field(default_factory=list)
    parts: dict[str, int] = field(default_factory=dict)  # tokens per part
    budget: int = 0


class Counter:
    """Counts tokens with the runner's own tokenizer when there is one, remembering answers
    by text, and estimates (a character count divided by 3) when there isn't."""

    def __init__(self, tokenize: Callable[[str], Awaitable[int]] | None = None) -> None:
        self._tokenize = tokenize
        self._cache: dict[str, int] = {}

    async def count(self, text: str) -> int:
        if not text:
            return 0
        if self._tokenize is None:
            return len(text) // 3 + 1
        key = hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()
        if key not in self._cache:
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[key] = await self._tokenize(text)
        return self._cache[key]

    async def message(self, message: dict) -> int:
        body = message.get("content") or ""
        if message.get("tool_calls"):
            body += json.dumps(message["tool_calls"], ensure_ascii=False)
        return await self.count(body) + TOOL_OVERHEAD_TOKENS


def answer_reserve(ctx: int) -> int:
    return min(ANSWER_RESERVE, ctx // 4)


def released_stub(message: dict) -> dict:
    name = message.get("name") or "the tool"
    return {
        **message,
        "content": f"[This {name} result was released to save space. Call it again to see it.]",
    }


def split_turns(history: list[dict]) -> list[list[dict]]:
    """Group messages into turns, each starting at a user message, so an assistant's tool
    calls are never separated from their results."""
    turns: list[list[dict]] = []
    for message in history:
        if message["role"] == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


async def build(
    *,
    system: str,
    tools: list[dict],
    attachments: list[Attachment],
    history: list[dict],
    ctx: int,
    counter: Counter,
    pins: list[tuple[str, str]] = (),  # (kind, text), in the order they were pinned
    summary: str = "",
) -> Built:
    """`history` is the transcript in OpenAI message form, oldest first, ending with the
    current turn (its user message, and any tool calls made so far in it). When the chat has
    a summary, `history` holds only the turns after it."""
    budget = ctx - answer_reserve(ctx)
    tools_tokens = await counter.count(json.dumps(tools, ensure_ascii=False)) if tools else 0
    system_tokens = await counter.count(system) + TOOL_OVERHEAD_TOKENS
    # Pinned items and the summary always go in whole: pins are short by rule, and the
    # summary is written to a fixed size (see compaction.py).
    pinned = _pins_section(pins)
    summarised = (
        f"\n\n# Earlier in this chat (summary)\n{summary.strip()}" if summary.strip() else ""
    )
    pins_tokens = await counter.count(pinned)
    summary_tokens = await counter.count(summarised)
    room = budget - tools_tokens - system_tokens - pins_tokens - summary_tokens
    if room <= 0:
        raise ContextTooSmall("The system prompt and tools don't fit this model's context.")

    files, stubbed, files_tokens = await _pinned(attachments, int(room * PINNED_SHARE), counter)
    # Most stable first, so the runner can reuse its cache: rules, pins, files, summary.
    system_message = {"role": "system", "content": system + pinned + files + summarised}
    room -= files_tokens

    turns = split_turns(history)
    sizes = [[await counter.message(m) for m in turn] for turn in turns]
    released = 0

    def total() -> int:
        return sum(sum(s) for s in sizes)

    # 1. Release tool results in earlier turns, oldest first.
    for t in range(len(turns) - 1):
        for i, message in enumerate(turns[t]):
            if total() <= room:
                break
            if message["role"] == "tool":
                turns[t][i] = released_stub(message)
                sizes[t][i] = await counter.message(turns[t][i])
                released += 1
    # 2. Leave out the oldest turns.
    left_out = 0
    while total() > room and len(turns) > 1:
        left_out += len(turns.pop(0))
        sizes.pop(0)
    # 3. Release earlier tool results in the current turn (never the newest one).
    current = turns[-1]
    tool_indexes = [i for i, m in enumerate(current) if m["role"] == "tool"][:-1]
    for i in tool_indexes:
        if total() <= room:
            break
        current[i] = released_stub(current[i])
        sizes[-1][i] = await counter.message(current[i])
        released += 1
    if total() > room:
        raise ContextTooSmall(
            "The current message and its tool results don't fit this model's context."
        )

    # "name" (which tool a result came from) and "id" (the stored message) are Lectern's
    # own notes; the model gets the plain OpenAI fields.
    messages = [system_message] + [
        {k: v for k, v in m.items() if k not in ("name", "id")} for turn in turns for m in turn
    ]
    return Built(
        messages=messages,
        left_out=left_out,
        released=released,
        stubbed_files=stubbed,
        parts={
            "system": system_tokens,
            "tools": tools_tokens,
            "pinned": pins_tokens,
            "files": files_tokens,
            "summary": summary_tokens,
            "turns": total(),
        },
        budget=budget,
    )


def _pins_section(pins: list[tuple[str, str]]) -> str:
    if not pins:
        return ""
    lines = "\n".join(f"- ({kind}) {text}" for kind, text in pins)
    return f"\n\n# Pinned by the person (these hold; follow them)\n{lines}"


async def _pinned(
    attachments: list[Attachment], share: int, counter: Counter
) -> tuple[str, list[str], int]:
    """The attached files' text, whole, while it fits the pinned share; a stub for the rest."""
    if not attachments:
        return "", [], 0
    parts = ["\n\n# Files attached in this chat"]
    stubbed: list[str] = []
    used = await counter.count(parts[0])
    for attachment in attachments:
        if not attachment.text:
            section = (
                f"\n\n## {attachment.name}\n"
                f"[No text to read: {attachment.note or 'this kind of file has none'}.]"
            )
        else:
            section = f"\n\n## {attachment.name}\n{attachment.text}"
            if used + await counter.count(section) > share:
                stubbed.append(attachment.name)
                section = (
                    f"\n\n## {attachment.name}\n[Too long to include here. Read it with "
                    f"document_outline and document_read.]"
                )
        used += await counter.count(section)
        parts.append(section)
    return "".join(parts), stubbed, used
