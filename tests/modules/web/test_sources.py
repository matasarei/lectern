from __future__ import annotations

import asyncio
import threading
from urllib.parse import unquote

import httpx2
import pytest
from ddgs.exceptions import DDGSException, TimeoutException

from lectern.modules.web import sources, wiki
from lectern.modules.web.sources import DuckDuckGo, Hit, SourceFailed


class FakeDDGS:
    """Stands in for ddgs.DDGS: records what it was asked, answers with `rows` or raises."""

    calls: list[dict] = []

    def __init__(self, rows=None, error: Exception | None = None) -> None:
        self.rows, self.error = rows or [], error

    def __call__(self, **kwargs):
        self.made_with = kwargs
        return self

    def text(self, query: str, **kwargs):
        self.calls.append({"query": query, **kwargs})
        if self.error:
            raise self.error
        return self.rows


def row(n: int, body: str = "A snippet.") -> dict:
    return {"title": f" Result\n {n} ", "href": f"https://example.com/{n}", "body": body}


def test_duckduckgo_is_asked_through_its_own_backend_only():
    fake = FakeDDGS([row(1)])
    DuckDuckGo(fake).search("python unhashable list", 8)
    assert fake.calls[-1] == {
        "query": "python unhashable list", "backend": "duckduckgo", "max_results": 8,
    }  # fmt: skip
    assert DuckDuckGo.BACKEND == "duckduckgo"  # "auto" would ask Bing, Google, Yandex…


def test_hits_have_a_title_url_and_snippet_cut_to_300_characters():
    hits = DuckDuckGo(FakeDDGS([row(1, "word " * 200), row(2)])).search("q", 8)
    assert hits[0].title == "Result 1"
    assert hits[0].url == "https://example.com/1"
    assert len(hits[0].snippet) == 300
    assert hits[0].snippet.endswith("…")
    assert hits[1] == Hit("Result 2", "https://example.com/2", "A snippet.")


def test_no_more_hits_than_asked_and_none_without_a_web_address():
    rows = [*(row(n) for n in range(12)), {"title": "x", "href": "javascript:alert(1)"}]
    assert len(DuckDuckGo(FakeDDGS(rows)).search("q", 8)) == 8
    assert DuckDuckGo(FakeDDGS([{"title": "x", "href": "/relative"}])).search("q", 8) == []


def test_no_results_is_an_empty_list_not_a_failure():
    fake = FakeDDGS(error=DDGSException("No results found."))
    assert DuckDuckGo(fake).search("q", 8) == []


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutException("timed out"), "didn't answer in time"),
        (DDGSException("ConnectError: dns failure"), "couldn't be read: ConnectError"),
    ],
)
def test_a_ddgs_error_becomes_a_reason_the_model_can_pass_on(error, reason):
    with pytest.raises(SourceFailed, match=reason):
        DuckDuckGo(FakeDDGS(error=error)).search("q", 8)


class Source:
    def __init__(self, id: str, hits=None, error: str | None = None) -> None:
        self.id, self.host, self.hits, self.error = id, f"{id}.example", hits or [], error
        self.asked = 0

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
    source, hits = asyncio.run(sources.search("q", [first, second, third], 8))
    assert source is second
    assert hits == [Hit("t", "u", "")]
    assert third.asked == 0


def test_a_source_that_answers_with_nothing_is_the_answer():
    empty, other = Source("a"), Source("b", [Hit("t", "u", "")])
    source, hits = asyncio.run(sources.search("q", [empty, other], 8))
    assert (source, hits, other.asked) == (empty, [], 0)


def test_every_reason_is_given_when_all_sources_fail():
    with pytest.raises(SourceFailed, match="a is down; b is down"):
        asyncio.run(
            sources.search("q", [Source("a", error="a is down"), Source("b", error="b is down")], 8)
        )


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
