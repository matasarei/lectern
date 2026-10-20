"""Where a web search goes. DuckDuckGo, through the ddgs library: DuckDuckGo has no official
API, so ddgs reads its HTML results page, and a change there or a rate limit can stop it
working. The search tries each source in order and moves to the next only when one fails;
Brave or a SearXNG server would be one class each here. Wikipedia isn't one of them: it
answers a different question, and has a tool of its own (wiki.py)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ddgs.engines.duckduckgo import Duckduckgo
from ddgs.exceptions import DDGSException, TimeoutException

SNIPPET_CHARS = 300
SEARCH_SECONDS = 15.0  # one source's answer; ddgs's own request timeout is shorter
# DuckDuckGo's answer when it limits a computer's searches: an "anomaly" page, status 202.
RATE_LIMITED = 202
LIMITED = "DuckDuckGo is limiting searches from this computer for now; it lifts within minutes"


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
    # How long to stop asking after DuckDuckGo said it is limiting this computer: asking again
    # sooner keeps the limit on (2026-10-20: 202 to every query for over a minute of retries,
    # lifted after half a minute without one).
    PAUSE_SECONDS = 120.0

    def __init__(
        self,
        engine: Callable[[], Any] = lambda: Duckduckgo(timeout=10),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._paused_until = 0.0

    def search(self, query: str, limit: int) -> list[Hit]:
        if self._clock() < self._paused_until:
            raise SourceFailed(LIMITED)
        # ddgs's DuckDuckGo engine, not DDGS().text: that one reports a refused query as "No
        # results found." (ddgs 9.16), and a limit has to be told from a search that found
        # nothing. The engine posts to html.duckduckgo.com and nowhere else.
        engine = self._engine()
        payload = engine.build_payload(
            query=query, region="us-en", safesearch="moderate", timelimit=None
        )
        try:
            response = engine.http_client.request(
                engine.search_method, engine.search_url, data=payload
            )
        except TimeoutException as e:
            raise SourceFailed("DuckDuckGo didn't answer in time") from e
        except DDGSException as e:
            raise SourceFailed(f"DuckDuckGo couldn't be reached: {e}") from e
        if response.status_code == RATE_LIMITED:
            self._paused_until = self._clock() + self.PAUSE_SECONDS
            raise SourceFailed(LIMITED)
        if response.status_code != 200:
            raise SourceFailed(f"DuckDuckGo answered {response.status_code}")
        rows = engine.post_extract_results(engine.extract_results(response.text))
        hits = [
            Hit(
                title=" ".join(row.title.split()),
                url=row.href,
                snippet=_cut(" ".join(row.body.split()), SNIPPET_CHARS),
            )
            for row in rows
            if row.href.startswith(("http://", "https://"))
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
