from __future__ import annotations

import asyncio
import threading
from urllib.parse import unquote

import httpx2
import pytest
from ddgs.engines.duckduckgo import Duckduckgo
from ddgs.engines.yahoo import Yahoo as YahooEngine
from ddgs.exceptions import DDGSException, TimeoutException

from lectern.modules.web import sources, wiki
from lectern.modules.web.sources import DuckDuckGo, Hit, NoAnswer, SourceFailed, Yahoo

RESULTS = """<html><body>
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title"><a class="result__a" href="https://example.com/1"> Result
      1 </a></h2>
    <a class="result__snippet" href="https://example.com/1">A <b>snippet</b>.</a>
  </div>
</div>
<div class="result"><div class="links_main result__body">
  <h2 class="result__title"><a class="result__a" href="https://example.com/2">Result 2</a></h2>
  <a class="result__snippet" href="https://example.com/2">{long}</a>
</div></div>
<div class="result"><div class="links_main result__body">
  <h2 class="result__title"><a href="https://duckduckgo.com/y.js?ad=1">An ad</a></h2>
  <a class="result__snippet" href="https://duckduckgo.com/y.js?ad=1">Buy now</a>
</div></div>
</body></html>"""

# Yahoo's desktop page, as ddgs reads it: a result's link goes through Yahoo's redirect.
YAHOO = """<html><body>
<div class="dd algo algo-sr relsrch Sr"><div class="compTitle">
  <a href="https://r.search.yahoo.com/_ylt=x/RU=https%3a%2f%2fexample.org%2fy/RK=2/RS=z">
    <h3>Yahoo result</h3></a></div>
  <div class="compText"><p>Its snippet.</p></div></div>
</body></html>"""


class Answer:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code, self.text = status, text


class FakeHttp:
    """Stands in for ddgs's HTTP client: answers in turn, records each request."""

    def __init__(self, answers: list[Answer | Exception]) -> None:
        self.answers, self.requests = answers, []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append((method, url, kwargs.get("data") or kwargs.get("params")))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def scraped(kind, engine_class, *answers: Answer | Exception, clock: Clock | None = None):
    """The real ddgs engine (its request and its parsing of the page), over a fake connection."""
    http = FakeHttp(list(answers))

    def engine():
        made = engine_class(timeout=10)
        made.http_client = http
        return made

    return kind(engine, clock or Clock()), http


def duckduckgo(*answers: Answer | Exception, clock: Clock | None = None):
    return scraped(DuckDuckGo, Duckduckgo, *answers, clock=clock)


def yahoo(*answers: Answer | Exception, clock: Clock | None = None):
    return scraped(Yahoo, YahooEngine, *answers, clock=clock)


def test_a_search_posts_the_query_to_duckduckgo_only():
    source, http = duckduckgo(Answer(200, RESULTS.format(long="x")))
    source.search("python unhashable list", 8)
    ((method, url, data),) = http.requests
    assert (method, url) == ("POST", "https://html.duckduckgo.com/html/")
    assert data["q"] == "python unhashable list"


def test_a_yahoo_search_gets_yahoo_only_and_follows_its_redirect_links():
    source, http = yahoo(Answer(200, YAHOO))
    assert source.search("odfdo python", 8) == [
        Hit("Yahoo result", "https://example.org/y", "Its snippet.")
    ]
    ((method, url, params),) = http.requests
    assert method == "GET" and url.startswith("https://search.yahoo.com/search;")
    assert params["p"] == "odfdo python"


def test_hits_have_a_title_url_and_snippet_cut_to_300_characters():
    source, _ = duckduckgo(Answer(200, RESULTS.format(long="word " * 200)))
    hits = source.search("q", 8)
    assert hits[0] == Hit("Result 1", "https://example.com/1", "A snippet.")
    assert hits[1].url == "https://example.com/2"
    assert len(hits[1].snippet) == 300
    assert hits[1].snippet.endswith("…")
    assert [h.title for h in hits] == ["Result 1", "Result 2"]  # no ad


def test_no_more_hits_than_asked():
    source, _ = duckduckgo(Answer(200, RESULTS.format(long="x")))
    assert len(source.search("q", 1)) == 1


def test_a_page_with_no_results_is_an_empty_list_not_a_failure():
    source, _ = duckduckgo(Answer(200, "<html><body>No results.</body></html>"))
    assert source.search("zzzz", 8) == []


def test_an_engine_rests_30_seconds_after_every_request():
    clock = Clock()
    page = Answer(200, RESULTS.format(long="x"))
    source, http = duckduckgo(page, page, clock=clock)

    assert len(source.search("q", 8)) == 2
    assert source.ready_in() == sources.REST_SECONDS
    clock.now += sources.REST_SECONDS - 1
    with pytest.raises(SourceFailed, match="DuckDuckGo is resting between searches"):
        source.search("q", 8)
    assert len(http.requests) == 1  # the second wasn't sent
    assert source.ready_in() == 1

    clock.now += 1
    assert source.ready_in() == 0
    assert len(source.search("q", 8)) == 2


def test_a_refused_request_starts_the_rest_too():
    clock = Clock()
    source, http = duckduckgo(Answer(202, "<html>anomaly</html>"), clock=clock)
    with pytest.raises(SourceFailed, match="limiting searches from this computer"):
        source.search("q", 8)
    with pytest.raises(SourceFailed, match="resting"):
        source.search("q", 8)
    assert len(http.requests) == 1


def test_two_chats_at_once_get_one_request_through():
    clock = Clock()
    page = Answer(200, RESULTS.format(long="x"))
    source, http = duckduckgo(page, page, clock=clock)
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def chat() -> None:
        start.wait()
        try:
            source.search("q", 8)
            outcomes.append("hits")
        except SourceFailed:
            outcomes.append("resting")

    threads = [threading.Thread(target=chat) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(outcomes) == ["hits", "resting"]
    assert len(http.requests) == 1


def test_the_engines_rest_apart():
    clock = Clock()
    ddg, _ = duckduckgo(Answer(200, RESULTS.format(long="x")), clock=clock)
    yah, _ = yahoo(Answer(200, YAHOO), clock=clock)
    ddg.search("q", 8)
    assert yah.ready_in() == 0
    assert len(yah.search("q", 8)) == 1


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (TimeoutException("timed out"), "didn't answer in time"),
        (DDGSException("ConnectError: dns failure"), "couldn't be reached: ConnectError"),
        (Answer(500), "DuckDuckGo answered 500"),
    ],
)
def test_a_failure_becomes_a_reason_the_model_can_pass_on(answer, reason):
    source, _ = duckduckgo(answer)
    with pytest.raises(SourceFailed, match=reason):
        source.search("q", 8)


def test_a_real_engine_gets_a_desktop_browsers_fingerprint():
    # A phone's gets Yahoo's mobile page, which ddgs reads as no results.
    engine = Yahoo()._desktop_engine()
    assert engine.http_client.client.impersonate_os in sources.DESKTOP


class Source:
    def __init__(self, id: str, hits=None, error: str | None = None, rest: float = 0) -> None:
        self.id, self.host, self.hits, self.error = id, f"{id}.example", hits or [], error
        self.asked, self.rest = 0, rest

    def ready_in(self) -> float:
        return self.rest

    def search(self, query: str, limit: int) -> list[Hit]:
        self.asked += 1
        if self.error:
            raise SourceFailed(self.error)
        return self.hits


def test_the_next_source_is_asked_only_when_one_fails():
    first, second, third = (
        Source("a", error="a is down"),
        Source("b", [Hit("t", "u", "")]),
        Source("c"),
    )
    source, hits, passed_over = asyncio.run(sources.search("q", [first, second, third], 8))
    assert source is second
    assert hits == [Hit("t", "u", "")]
    assert third.asked == 0


def test_why_a_source_was_passed_over_comes_back_but_not_a_rest():
    refused = Source("brave", error="The Brave Search API refused the key; an admin can change it")
    resting = Source("ddg")
    resting.search = lambda query, limit: (_ for _ in ()).throw(
        sources.Resting("DuckDuckGo is resting between searches")
    )
    answering = Source("yahoo", [Hit("t", "u", "")])
    source, _, passed_over = asyncio.run(sources.search("q", [refused, resting, answering], 8))
    assert source is answering
    assert passed_over == ["The Brave Search API refused the key; an admin can change it"]


def test_a_source_that_answers_with_nothing_is_the_answer():
    empty, other = Source("a"), Source("b", [Hit("t", "u", "")])
    source, hits, _ = asyncio.run(sources.search("q", [empty, other], 8))
    assert (source, hits, other.asked) == (empty, [], 0)


def test_every_reason_is_given_when_all_sources_fail():
    failing = [Source("a", error="a is down"), Source("b", error="b is down")]
    with pytest.raises(NoAnswer, match="a is down; b is down") as no:
        asyncio.run(sources.search("q", failing, 8))
    assert no.value.wait == sources.REST_SECONDS  # nobody resting: wait a whole rest


def test_no_answer_says_how_long_until_the_first_engine_is_back():
    resting = [
        Source("a", error="a is resting", rest=12),
        Source("b", error="b is resting", rest=4),
    ]
    with pytest.raises(NoAnswer) as no:
        asyncio.run(sources.search("q", resting, 8))
    assert no.value.wait == 4


def test_a_source_slower_than_the_limit_is_given_up(monkeypatch):
    monkeypatch.setattr(sources, "SEARCH_SECONDS", 0.05)
    release = threading.Event()

    class Stuck(Source):
        def search(self, query: str, limit: int) -> list[Hit]:
            release.wait(5)
            return []

    async def go() -> None:
        try:
            await sources.search("q", [Stuck("stuck")], 8)
        finally:
            release.set()  # before asyncio.run waits for the worker thread

    with pytest.raises(SourceFailed, match="stuck.example didn't answer within"):
        asyncio.run(go())


# Wikipedia


def wikipedia(seen: list[httpx2.Request], *, summaries: dict[str, dict] | None = None):
    summaries = summaries if summaries is not None else {
        "Hryhorii Skovoroda": {"extract": "A  philosopher\nand poet.", "type": "standard"},
        "Skovoroda (crater)": {"extract": "A crater.", "type": "standard"},
    }  # fmt: skip

    async def handle(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0)
        seen.append(request)
        if request.url.path == "/w/api.php":
            titles = [*summaries, "Gone page"]
            return httpx2.Response(200, json={"query": {"search": [{"title": t} for t in titles]}})
        title = unquote(request.url.raw_path.decode().rsplit("/", 1)[1]).replace("_", " ")
        if title not in summaries:
            return httpx2.Response(404)
        page = summaries[title]
        url = f"https://{request.url.host}/wiki/{title.replace(' ', '_')}"
        return httpx2.Response(
            200, json={"title": title, **page, "content_urls": {"desktop": {"page": url}}}
        )

    return httpx2.MockTransport(handle)


def test_a_lookup_gives_title_url_and_summary_from_the_languages_wikipedia():
    seen: list[httpx2.Request] = []
    articles = asyncio.run(wiki.lookup("Skovoroda", "uk", transport=wikipedia(seen)))

    assert [a.title for a in articles] == ["Hryhorii Skovoroda", "Skovoroda (crater)"]
    assert articles[0].url == "https://uk.wikipedia.org/wiki/Hryhorii_Skovoroda"
    assert articles[0].summary == "A philosopher and poet."
    assert {r.url.host for r in seen} == {"uk.wikipedia.org"}
    search = seen[0]
    assert search.url.params["srsearch"] == "Skovoroda"
    assert search.url.params["srlimit"] == "3"
    assert search.headers["user-agent"].startswith("Lectern/")
    assert "github.com/matasarei/lectern" in search.headers["user-agent"]  # Wikimedia's policy


def test_a_long_summary_is_cut():
    seen: list[httpx2.Request] = []
    transport = wikipedia(seen, summaries={"Long": {"extract": "word " * 1000}})
    (article,) = asyncio.run(wiki.lookup("long", transport=transport))
    assert len(article.summary) == wiki.SUMMARY_CHARS


def test_no_articles_is_an_empty_list():
    seen: list[httpx2.Request] = []
    assert asyncio.run(wiki.lookup("zzzz", transport=wikipedia(seen, summaries={}))) == []


@pytest.mark.parametrize("language", ["", "e", "english", "EN", "en.evil.example/x", "u k", "1a"])
def test_a_language_that_isnt_a_wikipedia_code_is_refused_before_any_request(language):
    seen: list[httpx2.Request] = []
    with pytest.raises(ValueError, match="language code"):
        asyncio.run(wiki.lookup("x", language, transport=wikipedia(seen)))
    assert seen == []


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (httpx2.Response(503), "uk.wikipedia.org answered 503"),
        (httpx2.Response(200, content=b"<html>not json"), "couldn't be read"),
    ],
)
def test_a_wikipedia_that_fails_gives_a_reason(answer, reason):
    async def handle(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0)
        return answer

    with pytest.raises(SourceFailed, match=reason):
        asyncio.run(wiki.lookup("x", "uk", transport=httpx2.MockTransport(handle)))


def test_a_wikipedia_that_cant_be_reached_gives_a_reason():
    async def handle(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no such host", request=request)

    with pytest.raises(SourceFailed, match="xx.wikipedia.org couldn't be reached"):
        asyncio.run(wiki.lookup("x", "xx", transport=httpx2.MockTransport(handle)))
