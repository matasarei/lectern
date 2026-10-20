"""Where a web search goes. With an admin's key, the Brave Search API first (brave.py); then
two free engines, DuckDuckGo and Yahoo, whose results pages are read through the ddgs library.
Neither has an API for this, so a change to a page or a limit on this computer can stop one
working. Each free engine rests REST_SECONDS after every request it gets, shared by every chat,
and a search skips one that's resting. Wikipedia isn't one of these: it answers a different
question, and has a tool of its own (wiki.py)."""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

import primp
from ddgs.engines.duckduckgo import Duckduckgo
from ddgs.engines.yahoo import Yahoo as YahooEngine
from ddgs.exceptions import DDGSException, TimeoutException

SNIPPET_CHARS = 300
SEARCH_SECONDS = 15.0  # one source's answer; the engines' own request timeout is shorter
# After every request a free engine gets, answered or refused, the next waits this long: more
# often and they refuse (2026-10-20: DuckDuckGo refused a query sent 5 s after another).
REST_SECONDS = 30.0
# DuckDuckGo's answer when it limits a computer's searches: an "anomaly" page, status 202.
RATE_LIMITED = 202
# A desktop browser, always: given a phone's, Yahoo sends a page ddgs can't read (no results).
DESKTOP = ("windows", "macos")


@dataclass(frozen=True)
class Hit:
    title: str
    url: str
    snippet: str


class SourceFailed(Exception):
    """A source couldn't answer. The message says why, in words the model can pass on."""


class Resting(SourceFailed):
    """A free engine wasn't asked: it's resting between searches. Not worth telling anyone."""


class NoAnswer(SourceFailed):
    """No source answered this try. `wait` is how long until one may be asked again."""

    def __init__(self, reason: str, wait: float) -> None:
        super().__init__(reason)
        self.wait = wait


class SearchSource(Protocol):
    id: str
    host: str  # where the query goes, for the card

    def search(self, query: str, limit: int) -> list[Hit]:
        """Blocking: called in a worker thread."""
        ...

    def ready_in(self) -> float:
        """Seconds until it may be asked again: 0 when it may be asked now."""
        ...


def _hit(title: str, url: str, snippet: str) -> Hit:
    return Hit(" ".join(title.split()), url, _cut(" ".join(snippet.split()), SNIPPET_CHARS))


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class ScrapedEngine:
    """A search engine's results page, fetched and read through ddgs's engine for it (its
    request and its parser; not DDGS().text, which reports a refused query as "No results
    found." in ddgs 9.16, and a refusal has to be told from a search that found nothing)."""

    id: ClassVar[str]
    host: ClassVar[str]
    name: ClassVar[str]  # for the model: "DuckDuckGo"
    engine_class: ClassVar[type]

    def __init__(
        self,
        engine: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine or self._desktop_engine
        self._clock = clock
        self._lock = threading.Lock()
        self._next = 0.0  # when it may be asked again, on the clock

    def _desktop_engine(self) -> Any:  # noqa: ANN401 — a ddgs engine
        engine = self.engine_class(timeout=10)
        engine.http_client.client = primp.Client(
            impersonate="random",
            impersonate_os=random.choice(DESKTOP),  # noqa: S311 — not for secrets
            timeout=10,
        )
        return engine

    def ready_in(self) -> float:
        with self._lock:
            return max(0.0, self._next - self._clock())

    def _take_turn(self) -> bool:
        """Whether a request may go now; if so, the rest starts, so no other chat's can."""
        with self._lock:
            now = self._clock()
            if now < self._next:
                return False
            self._next = now + REST_SECONDS
            return True

    def search(self, query: str, limit: int) -> list[Hit]:
        if not self._take_turn():
            raise Resting(f"{self.name} is resting between searches")
        engine = self._engine()
        payload = engine.build_payload(
            query=query, region="us-en", safesearch="moderate", timelimit=None
        )  # sets the URL too (Yahoo's has a fresh token in it each time)
        sent = {"data": payload} if engine.search_method == "POST" else {"params": payload}
        try:
            response = engine.http_client.request(engine.search_method, engine.search_url, **sent)
        except TimeoutException as e:
            raise SourceFailed(f"{self.name} didn't answer in time") from e
        except DDGSException as e:
            raise SourceFailed(f"{self.name} couldn't be reached: {e}") from e
        if response.status_code == RATE_LIMITED:
            raise SourceFailed(f"{self.name} is limiting searches from this computer for now")
        if response.status_code != 200:
            raise SourceFailed(f"{self.name} answered {response.status_code}")
        rows = engine.post_extract_results(engine.extract_results(response.text))
        hits = [
            _hit(row.title, row.href, row.body)
            for row in rows
            if row.href.startswith(("http://", "https://"))
        ]
        return hits[:limit]


class DuckDuckGo(ScrapedEngine):
    id = "duckduckgo"
    host = "duckduckgo.com"
    name = "DuckDuckGo"
    engine_class = Duckduckgo  # posts to html.duckduckgo.com and nowhere else


class Yahoo(ScrapedEngine):
    id = "yahoo"
    host = "search.yahoo.com"
    name = "Yahoo"
    engine_class = YahooEngine  # gets search.yahoo.com and nowhere else


async def search(
    query: str, sources: Sequence[SearchSource], limit: int
) -> tuple[SearchSource, list[Hit], list[str]]:
    """One try down the order: the hits from the first source that answers (an answer with
    no hits is an answer), which one it was, and why the ones before it didn't answer (a rest
    left out), so a refused API key is said rather than covered by the next engine. NoAnswer,
    with every source's reason and how long until one may be asked again, when none does."""
    reasons: list[str] = []
    passed_over: list[str] = []
    for source in sources:
        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(source.search, query, limit), SEARCH_SECONDS
            )
        except SourceFailed as e:
            reasons.append(str(e))
            if not isinstance(e, Resting):
                passed_over.append(str(e))
            continue
        except TimeoutError:
            reason = f"{source.host} didn't answer within {SEARCH_SECONDS:.0f} s"
            reasons.append(reason)
            passed_over.append(reason)
            continue
        return source, hits, passed_over
    resting = [wait for s in sources if (wait := s.ready_in()) > 0]
    raise NoAnswer(
        "; ".join(reasons) or "no search source is set up",
        min(resting) if resting else REST_SECONDS,
    )
