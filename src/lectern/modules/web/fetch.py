"""Opening a page the model chose, without letting it reach this computer or its network.

The model writes the URL, so it could name anything Lectern's own host can reach: the app
itself on 127.0.0.1, a router's page, a cloud machine's metadata service. Before anything is
sent, the host is resolved and every address it resolves to must be a public one; the request
then goes to the address that was checked (with the host name kept for the Host header and
for TLS), so a second DNS answer can't swap a private one in. Redirects are followed by hand,
each new URL checked the same way. The size and the time are bounded, and only the types the
project can read are taken.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx2

from lectern import __version__

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
CONNECT_SECONDS = 5.0
TOTAL_SECONDS = 20.0
# What the project can read once it's saved: a page becomes Markdown, a PDF stays a PDF.
TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain", "application/pdf"})
REDIRECTS = frozenset({301, 302, 303, 307, 308})
USER_AGENT = f"Lectern/{__version__}"
NAT64 = ipaddress.IPv6Network("64:ff9b::/96")  # an IPv4 address carried in IPv6

# A host and port → the addresses they resolve to.
Resolver = Callable[[str, int], Awaitable[list[str]]]


class FetchRefused(Exception):
    """The page wasn't fetched. The message says why, in words the model can pass on."""


@dataclass(frozen=True)
class Fetched:
    final_url: str  # after the redirects
    content_type: str  # the media type alone, lower-case: "text/html"
    charset: str | None  # from the Content-Type header, if it named one
    data: bytes


async def resolve(host: str, port: int) -> list[str]:
    """The addresses `host` resolves to (the lookup runs in a worker thread)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def is_public(address: str) -> bool:
    """Whether an address is on the public internet: not loopback, private, link-local (the
    cloud metadata address among them), shared (CGNAT), multicast or reserved. An IPv4
    address carried in IPv6 is judged as the IPv4 address."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in NAT64:
            ip = ipaddress.IPv4Address(ip.packed[-4:])
    return ip.is_global and not ip.is_multicast  # is_global lets 224.0.0.0/4 through


def check_url(url: str) -> httpx2.URL:
    """The URL, parsed, if it's one a page may be fetched from; FetchRefused if not."""
    try:
        parsed = httpx2.URL(url)
    except httpx2.InvalidURL as e:
        raise FetchRefused(f"{url!r} isn't a URL ({e}).") from e
    if parsed.scheme not in ("http", "https"):
        raise FetchRefused(f"Only http and https pages can be opened, not {parsed.scheme!r}.")
    if parsed.userinfo:
        raise FetchRefused("A URL with a user name or password in it isn't opened.")
    if not parsed.host:
        raise FetchRefused(f"{url!r} has no host.")
    if parsed.port is not None and parsed.port not in (80, 443) and parsed.port < 1024:
        raise FetchRefused(f"Port {parsed.port} isn't one a web page is opened on.")
    # Refused by the name alone, before any lookup; any other name is checked once resolved.
    host = parsed.host.rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or not _public_literal(host):
        raise FetchRefused(
            f"{parsed.host} is this computer or its local network: only public pages are opened."
        )
    return parsed


def _public_literal(host: str) -> bool:
    """False for a host written as an address that isn't public; True for any name."""
    try:
        return is_public(host)
    except ValueError:  # a name, not an address
        return True


async def _public_address(url: httpx2.URL, resolver: Resolver) -> str:
    """The address to connect to for `url`: the first it resolves to, once all of them are
    public. One private answer among public ones is refused too."""
    port = url.port or (443 if url.scheme == "https" else 80)
    try:
        addresses = await resolver(url.host, port)
    except OSError as e:
        raise FetchRefused(f"{url.host} couldn't be found ({e}).") from e
    if not addresses:
        raise FetchRefused(f"{url.host} couldn't be found.")
    for address in addresses:
        if not is_public(address):
            raise FetchRefused(
                f"{url.host} is on this computer or its local network ({address}): "
                "only public pages are opened."
            )
    return addresses[0]


def _request(url: httpx2.URL, address: str) -> httpx2.Request:
    """A GET for `url`, sent to `address`: the Host header and TLS keep the host's name, so
    the certificate is checked against it. No cookies are sent."""
    pinned = url.copy_with(host=address)
    host = url.netloc.decode("ascii")  # with the port, if the URL named one
    extensions = {"timeout": {"connect": CONNECT_SECONDS, "read": TOTAL_SECONDS,
                              "write": TOTAL_SECONDS, "pool": TOTAL_SECONDS}}  # fmt: skip
    if url.scheme == "https":
        extensions["sni_hostname"] = url.host
    headers = {
        "Host": host,
        "User-Agent": USER_AGENT,
        "Accept": ", ".join(sorted(TYPES)),
        # Compressed answers could unpack to far more than they weigh; ask for none.
        "Accept-Encoding": "identity",
    }
    return httpx2.Request("GET", pinned, headers=headers, extensions=extensions)


def _media_type(response: httpx2.Response) -> tuple[str, str | None]:
    header = response.headers.get("content-type", "")
    media, _, params = header.partition(";")
    charset = None
    for param in params.split(";"):
        name, _, value = param.strip().partition("=")
        if name.lower() == "charset" and value:
            charset = value.strip('"').strip()
    return media.strip().lower(), charset


async def _read(response: httpx2.Response, host: str) -> bytes:
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BYTES:
        raise FetchRefused(f"The page from {host} is larger than {MAX_BYTES // 2**20} MB.")
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data += chunk
        if len(data) > MAX_BYTES:
            raise FetchRefused(f"The page from {host} is larger than {MAX_BYTES // 2**20} MB.")
    return bytes(data)


async def _fetch(url: str, transport: httpx2.AsyncBaseTransport, resolver: Resolver) -> Fetched:
    current = check_url(url)
    async with httpx2.AsyncClient(transport=transport, trust_env=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            address = await _public_address(current, resolver)
            response = await client.send(_request(current, address), stream=True)
            try:
                if response.status_code in REDIRECTS and "location" in response.headers:
                    current = check_url(str(current.join(response.headers["location"])))
                    continue
                if response.status_code >= 400:
                    raise FetchRefused(f"{current.host} answered {response.status_code}.")
                media, charset = _media_type(response)
                if media not in TYPES:
                    raise FetchRefused(
                        f"{current.host} sent {media or 'a file of no stated type'}, which "
                        "isn't opened: only web pages, plain text and PDFs are."
                    )
                data = await _read(response, current.host)
                return Fetched(str(current), media, charset, data)
            finally:
                await response.aclose()
    raise FetchRefused(f"{url} redirected more than {MAX_REDIRECTS} times.")


async def fetch(
    url: str,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    resolver: Resolver = resolve,
) -> Fetched:
    """Fetch a public page: FetchRefused, with the reason, for anything that isn't one, is
    too large or too slow, or can't be reached."""
    transport = transport or httpx2.AsyncHTTPTransport(retries=0)
    try:
        async with asyncio.timeout(TOTAL_SECONDS):
            return await _fetch(url, transport, resolver)
    except TimeoutError as e:
        raise FetchRefused(f"Opening {url} took longer than {TOTAL_SECONDS:.0f} s.") from e
    except httpx2.ConnectTimeout as e:
        raise FetchRefused(
            f"Couldn't connect to the page's host within {CONNECT_SECONDS:.0f} s."
        ) from e
    except httpx2.HTTPError as e:
        raise FetchRefused(f"Couldn't open {url}: {type(e).__name__} ({e}).") from e
