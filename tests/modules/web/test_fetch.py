from __future__ import annotations

import asyncio
import re
import socket

import httpx2
import pytest

from lectern.modules.web import fetch
from lectern.modules.web.fetch import FetchRefused, is_public

PUBLIC = "93.184.215.14"
NAMES = {
    "example.com": [PUBLIC],
    "docs.example.org": ["2606:4700::1111"],
    "localhost": ["127.0.0.1", "::1"],
    "intranet.example": ["10.0.0.1"],
    "half.example": [PUBLIC, "192.168.1.20"],
}


async def resolver(host: str, port: int) -> list[str]:
    await asyncio.sleep(0)
    if host in NAMES:
        return NAMES[host]
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
    except OSError:
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided") from None
    return [host]  # an address in the URL resolves to itself


class Site:
    """A fake internet: answers by the Host header, and records every request it got."""

    def __init__(self, pages: dict[str, httpx2.Response]) -> None:
        self.pages = pages
        self.seen: list[httpx2.Request] = []

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0)
        self.seen.append(request)
        key = f"{request.headers['host']}{request.url.raw_path.decode()}"
        return self.pages.get(key) or httpx2.Response(404)

    def get(self, url: str) -> fetch.Fetched:
        transport = httpx2.MockTransport(self.handle)
        return asyncio.run(fetch.fetch(url, transport=transport, resolver=resolver))


def html(body: str = "<p>hi</p>", **headers: str) -> httpx2.Response:
    return httpx2.Response(
        200, headers={"content-type": "text/html; charset=utf-8", **headers}, content=body
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "http://user:secret@example.com/",
        "http://example.com:22/",
        "http://127.0.0.1:8765/api/projects",
        "http://localhost/",
        "http://10.1.2.3/",
        "http://192.168.1.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8765/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",  # 127.0.0.1 written as one number
        "http://intranet.example/",  # a name resolving to a private address
        "http://half.example/",  # one private answer among public ones
        "http://no-such-host.invalid/",
    ],
)
def test_a_url_that_isnt_a_public_page_is_refused_before_any_request(url):
    site = Site({})
    NAMES["2130706433"] = ["127.0.0.1"]
    with pytest.raises(FetchRefused):
        site.get(url)
    assert site.seen == []


def test_a_redirect_to_the_metadata_address_is_refused_and_not_followed():
    site = Site({"example.com/go": httpx2.Response(
        302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
    )})  # fmt: skip
    with pytest.raises(FetchRefused, match="local network"):
        site.get("http://example.com/go")
    assert [r.headers["host"] for r in site.seen] == ["example.com"]


def test_a_redirect_to_a_file_url_is_refused():
    site = Site({"example.com/go": httpx2.Response(302, headers={"location": "file:///etc/x"})})
    with pytest.raises(FetchRefused, match="Only http and https"):
        site.get("http://example.com/go")


def test_redirects_are_followed_to_a_public_page_and_the_final_url_kept():
    moved = httpx2.Response(301, headers={"location": "https://docs.example.org/new"})
    latin = html("<p>new</p>", **{"content-type": "text/html; charset=latin-1"})
    site = Site({"example.com/old": moved, "docs.example.org/new": latin})
    page = site.get("http://example.com/old")
    assert page.final_url == "https://docs.example.org/new"
    assert (page.content_type, page.charset, page.data) == (
        "text/html",
        "latin-1",
        b"<p>new</p>",
    )


def test_the_request_goes_to_the_checked_address_with_the_hosts_name_kept():
    site = Site({"docs.example.org/a": html()})
    site.get("https://docs.example.org/a")
    (request,) = site.seen
    assert request.url.host == "2606:4700::1111"  # no second lookup by the transport
    assert request.headers["host"] == "docs.example.org"
    assert request.extensions["sni_hostname"] == "docs.example.org"  # TLS checks the name
    assert "cookie" not in request.headers


def test_more_than_five_redirects_are_refused():
    site = Site({"example.com/loop": httpx2.Response(302, headers={"location": "/loop"})})
    with pytest.raises(FetchRefused, match="more than 5"):
        site.get("http://example.com/loop")
    assert len(site.seen) == 6


def test_a_page_declared_larger_than_2_mb_is_refused_unread():
    big = html(**{"content-length": str(fetch.MAX_BYTES + 1)})
    with pytest.raises(FetchRefused, match="larger than 2 MB"):
        Site({"example.com/big": big}).get("http://example.com/big")


def test_a_page_that_grows_past_2_mb_is_cut_off():
    async def chunks():
        for _ in range(3):
            await asyncio.sleep(0)
            yield b"x" * (1024 * 1024)

    streamed = httpx2.Response(200, headers={"content-type": "text/plain"}, content=chunks())
    with pytest.raises(FetchRefused, match="larger than 2 MB"):
        Site({"example.com/big": streamed}).get("http://example.com/big")


def test_exactly_2_mb_is_read():
    page = Site({"example.com/f": httpx2.Response(
        200, headers={"content-type": "text/plain"}, content=b"x" * fetch.MAX_BYTES
    )}).get("http://example.com/f")  # fmt: skip
    assert len(page.data) == fetch.MAX_BYTES


def test_a_page_slower_than_the_limit_is_given_up(monkeypatch):
    monkeypatch.setattr(fetch, "TOTAL_SECONDS", 0.05)
    never = asyncio.Event()

    async def stalls(request: httpx2.Request) -> httpx2.Response:
        await never.wait()
        raise AssertionError("unreachable")

    async def go() -> None:
        await fetch.fetch(
            "http://example.com/", transport=httpx2.MockTransport(stalls), resolver=resolver
        )

    with pytest.raises(FetchRefused, match="took longer than"):
        asyncio.run(go())


@pytest.mark.parametrize("media", ["application/zip", "image/svg+xml", ""])
def test_a_type_the_project_cant_read_is_refused_by_name(media):
    response = httpx2.Response(200, headers={"content-type": media}, content=b"PK")
    with pytest.raises(FetchRefused, match=re.escape(media or "no stated type")):
        Site({"example.com/f": response}).get("http://example.com/f")


def test_an_error_answer_is_refused_with_its_status():
    with pytest.raises(FetchRefused, match="answered 404"):
        Site({}).get("http://example.com/missing")


def test_a_host_that_cant_be_reached_is_refused_with_the_reason():
    async def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    async def go() -> None:
        await fetch.fetch(
            "http://example.com/", transport=httpx2.MockTransport(down), resolver=resolver
        )

    with pytest.raises(FetchRefused, match="ConnectError"):
        asyncio.run(go())


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700::1111", True),
        ("100.64.0.1", False),  # shared (CGNAT)
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:10.0.0.1", False),
        ("64:ff9b::7f00:1", False),  # 127.0.0.1 through NAT64
        ("64:ff9b::808:808", True),  # 8.8.8.8 through NAT64
        ("0.0.0.0", False),  # noqa: S104 — an address under test, not a bind
        ("224.0.0.1", False),  # multicast, which is_global alone lets through
        ("ff02::1", False),
    ],
)
def test_which_addresses_count_as_public(address, public):
    assert is_public(address) is public
