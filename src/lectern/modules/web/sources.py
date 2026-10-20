"""Where a web search goes. DuckDuckGo, through the ddgs library: DuckDuckGo has no official
API, so ddgs reads its HTML results page, and a change there or a rate limit can stop it
working. The search tries each source in order and moves to the next only when one fails;
Brave or a SearXNG server would be one class each here. Wikipedia isn't one of them: it
answers a different question, and has a tool of its own (wiki.py)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ddgs import DDGS
from ddgs.exceptions import DDGSException, TimeoutException

SNIPPET_CHARS = 300
SEARCH_SECONDS = 15.0  # one source's answer; ddgs's own request timeout is shorter
NO_RESULTS = "No results found."  # what ddgs 9.16 raises with, for no results or a rate limit


@dataclass(frozen=True)
class Hit:
    title: str
    url: str
    snippet: str


class SourceFailed(Exception):
    """A source couldn't answer. The message says why, in words the model can pass on."""


class SearchSource(Protocol):
    id: str
    host: str  # where the query goes, for the card

    def search(self, query: str, limit: int) -> list[Hit]:
        """Blocking: called in a worker thread."""
        ...


class DuckDuckGo:
    id = "duckduckgo"
    host = "duckduckgo.com"
    # Pinned: ddgs's default, "auto", sends the query to every engine it knows (Bing, Google,
    # Yandex and more). This one engine posts to html.duckduckgo.com and nowhere else.
    BACKEND = "duckduckgo"

    def __init__(self, client: Callable[..., Any] = DDGS) -> None:
        self._client = client

    def search(self, query: str, limit: int) -> list[Hit]:
        try:
            rows = self._client(timeout=10).text(query, backend=self.BACKEND, max_results=limit)
        except TimeoutException as e:
            raise SourceFailed("DuckDuckGo didn't answer in time") from e
        except DDGSException as e:
            # ddgs gives no results and a refused (rate-limited) query the same way.
            if str(e) == NO_RESULTS:
                return []
            raise SourceFailed(f"DuckDuckGo couldn't be read: {e}") from e
        hits = [
            Hit(
                title=" ".join(str(row.get("title", "")).split()),
                url=str(row["href"]),
                snippet=_cut(" ".join(str(row.get("body", "")).split()), SNIPPET_CHARS),
            )
            for row in rows
            if str(row.get("href", "")).startswith(("http://", "https://"))
        ]
        return hits[:limit]


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def search(
    query: str, sources: Sequence[SearchSource], limit: int
) -> tuple[SearchSource, list[Hit]]:
    """The hits from the first source that answers, and which one it was. SourceFailed, with
    every source's reason, when none does."""
    reasons: list[str] = []
    for source in sources:
        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(source.search, query, limit), SEARCH_SECONDS
            )
        except SourceFailed as e:
            reasons.append(str(e))
            continue
        except TimeoutError:
            reasons.append(f"{source.host} didn't answer within {SEARCH_SECONDS:.0f} s")
            continue
        return source, hits
    raise SourceFailed("; ".join(reasons) or "no search source is set up")
