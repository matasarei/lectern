"""Looking a subject up on Wikipedia, through its own API: the search finds the articles,
and each one's summary is the first paragraphs of the article, as Wikipedia gives them."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx2

from lectern import __version__
from lectern.modules.web.sources import SourceFailed

LANGUAGE = re.compile(r"[a-z]{2,3}")  # a Wikipedia's code: "en", "uk", "ast"
LOOKUP_SECONDS = 15.0
SUMMARY_CHARS = 1200
# Wikimedia asks every client for a User-Agent that names it and says how to reach its makers.
USER_AGENT = f"Lectern/{__version__} (https://github.com/matasarei/lectern)"


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    summary: str


def host(language: str) -> str:
    """The Wikipedia for a language code; ValueError for anything that isn't one, so the
    model can't steer the request to another host."""
    if not LANGUAGE.fullmatch(language):
        raise ValueError(f"{language!r} isn't a Wikipedia language code (like en or uk).")
    return f"{language}.wikipedia.org"


async def _summary(client: httpx2.AsyncClient, site: str, title: str) -> Article | None:
    path = quote(title.replace(" ", "_"), safe="")
    response = await client.get(f"https://{site}/api/rest_v1/page/summary/{path}")
    if response.status_code == 404:  # the search found a page that's gone since
        return None
    response.raise_for_status()
    page = response.json()
    url = page.get("content_urls", {}).get("desktop", {}).get("page")
    extract = " ".join(str(page.get("extract", "")).split())
    if not url or not extract:
        return None
    if len(extract) > SUMMARY_CHARS:
        extract = extract[: SUMMARY_CHARS - 1].rstrip() + "…"
    return Article(title=str(page.get("title", title)), url=url, summary=extract)


async def _lookup(client: httpx2.AsyncClient, query: str, site: str, limit: int) -> list[Article]:
    response = await client.get(
        f"https://{site}/w/api.php",
        params={"action": "query", "list": "search", "srsearch": query, "srlimit": limit,
                "srprop": "", "format": "json"},
    )  # fmt: skip
    response.raise_for_status()
    titles = [row["title"] for row in response.json().get("query", {}).get("search", [])]
    found = await asyncio.gather(*(_summary(client, site, t) for t in titles[:limit]))
    return [a for a in found if a is not None]


async def lookup(
    query: str,
    language: str = "en",
    *,
    limit: int = 3,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> list[Article]:
    """Up to `limit` articles for `query` from that language's Wikipedia. ValueError for a
    bad language code; SourceFailed when Wikipedia can't be reached or read."""
    site = host(language)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        async with (
            asyncio.timeout(LOOKUP_SECONDS),
            httpx2.AsyncClient(transport=transport, headers=headers, timeout=10.0) as client,
        ):
            return await _lookup(client, query, site, limit)
    except TimeoutError as e:
        raise SourceFailed(f"{site} didn't answer within {LOOKUP_SECONDS:.0f} s") from e
    except httpx2.HTTPStatusError as e:
        raise SourceFailed(f"{site} answered {e.response.status_code}") from e
    except httpx2.HTTPError as e:
        raise SourceFailed(f"{site} couldn't be reached ({type(e).__name__})") from e
    except ValueError as e:  # an answer that isn't the JSON the API sends
        raise SourceFailed(f"{site} sent an answer that couldn't be read") from e
