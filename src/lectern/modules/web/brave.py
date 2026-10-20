"""The Brave Search API: an index of its own, asked with the key an admin set. It comes first
when there is a key, and has no rest of its own (its limits are the key's, per second and per
month). The key goes in a header, never in a URL, a message or a log line."""

from __future__ import annotations

import html
import re

import httpx2

from lectern import __version__
from lectern.modules.web.sources import Hit, SourceFailed, _hit

API = "https://api.search.brave.com/res/v1/web/search"
MAX_COUNT = 20  # the most results the API gives in one answer
TAGS = re.compile(r"<[^>]+>")  # its descriptions mark the words that matched: <strong>…</strong>


class BraveApi:
    id = "brave"
    host = "api.search.brave.com"
    name = "The Brave Search API"

    def __init__(self, key: str, transport: httpx2.BaseTransport | None = None) -> None:
        self._key = key
        self._transport = transport

    def __repr__(self) -> str:  # the key never shows, whoever prints this
        return "BraveApi(key=…)"

    def ready_in(self) -> float:
        return 0.0

    def search(self, query: str, limit: int) -> list[Hit]:
        headers = {
            "X-Subscription-Token": self._key,
            "Accept": "application/json",
            "User-Agent": f"Lectern/{__version__}",
        }
        params = {"q": query, "count": min(limit, MAX_COUNT)}
        try:
            with httpx2.Client(transport=self._transport, headers=headers, timeout=10.0) as client:
                response = client.get(API, params=params)
        except httpx2.TimeoutException as e:
            raise SourceFailed(f"{self.name} didn't answer in time") from e
        except httpx2.HTTPError as e:
            raise SourceFailed(f"{self.name} couldn't be reached ({type(e).__name__})") from e
        if response.status_code in (401, 403):
            raise SourceFailed(f"{self.name} refused the key; an admin can change it")
        if response.status_code == 429:
            raise SourceFailed(f"{self.name}'s limit for this key is used up for now")
        if response.status_code != 200:
            raise SourceFailed(f"{self.name} answered {response.status_code}")
        try:
            results = response.json().get("web", {}).get("results", [])
        except ValueError as e:  # not the JSON the API sends
            raise SourceFailed(f"{self.name} sent an answer that couldn't be read") from e
        hits = [
            _hit(
                str(r.get("title", "")),
                str(r["url"]),
                html.unescape(TAGS.sub("", str(r.get("description", "")))),
            )
            for r in results
            if str(r.get("url", "")).startswith(("http://", "https://"))
        ]
        return hits[:limit]
