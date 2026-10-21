from __future__ import annotations

import httpx2
import pytest

from lectern.modules.web.brave import BraveApi
from lectern.modules.web.sources import Hit, SourceFailed

KEY = "test-key-not-real"  # noqa: S105 (made up for the tests)


def brave(*answers: httpx2.Response | Exception) -> tuple[BraveApi, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []
    queue = list(answers)

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return BraveApi(KEY, transport=httpx2.MockTransport(handle)), seen


def results(*rows: dict) -> httpx2.Response:
    return httpx2.Response(200, json={"type": "search", "web": {"results": list(rows)}})


def test_a_search_asks_the_api_with_the_key_in_a_header():
    source, seen = brave(results())
    source.search("httpx streaming", 8)
    (request,) = seen
    assert str(request.url).startswith("https://api.search.brave.com/res/v1/web/search?")
    assert request.url.params["q"] == "httpx streaming"
    assert request.url.params["count"] == "8"
    assert request.headers["x-subscription-token"] == KEY
    assert KEY not in str(request.url)


def test_hits_have_a_title_url_and_snippet_without_its_markup():
    source, _ = brave(results(
        {"title": "HTTPX  Streaming", "url": "https://www.python-httpx.org/s",
         "description": "Use <strong>stream()</strong> for big bodies &amp; files."},
        {"title": "Not a page", "url": "javascript:alert(1)", "description": "x"},
        {"title": "Long", "url": "https://example.com/l", "description": "word " * 200},
    ))  # fmt: skip
    hits = source.search("q", 8)
    assert hits[0] == Hit("HTTPX Streaming", "https://www.python-httpx.org/s",
                          "Use stream() for big bodies & files.")  # fmt: skip
    assert [h.url for h in hits] == ["https://www.python-httpx.org/s", "https://example.com/l"]
    assert len(hits[1].snippet) == 300


def test_no_more_than_asked_and_never_more_than_the_api_gives():
    rows = [
        {"title": str(n), "url": f"https://example.com/{n}", "description": ""} for n in range(9)
    ]
    source, seen = brave(results(*rows), results(*rows))
    assert len(source.search("q", 3)) == 3
    source.search("q", 50)
    assert seen[1].url.params["count"] == "20"


def test_no_results_is_an_empty_list():
    source, _ = brave(httpx2.Response(200, json={"type": "search"}))
    assert source.search("zzzz", 8) == []


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (httpx2.Response(401), "refused the key"),
        (httpx2.Response(403), "refused the key"),
        (httpx2.Response(429), "limit for this key is used up"),
        (httpx2.Response(503), "answered 503"),
        (httpx2.Response(200, content=b"<html>"), "couldn't be read"),
        (httpx2.ReadTimeout("slow"), "didn't answer in time"),
        (httpx2.ConnectError("no route"), "couldn't be reached \\(ConnectError\\)"),
    ],
)
def test_a_failure_says_why_without_the_key(answer, reason):
    if isinstance(answer, Exception):
        answer.request = httpx2.Request("GET", "https://api.search.brave.com/")
    source, _ = brave(answer)
    with pytest.raises(SourceFailed, match=reason) as failed:
        source.search("q", 8)
    assert KEY not in str(failed.value)


def test_the_key_never_shows_in_its_repr():
    assert KEY not in repr(BraveApi(KEY))
    assert BraveApi(KEY).ready_in() == 0
