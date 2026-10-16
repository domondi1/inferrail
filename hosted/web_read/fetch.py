"""Fetching a public web page safely on a buyer's behalf.

- Public http(s) only, ports 80/443. Every hop (including redirects) must resolve to public
  addresses only, and the connected peer is re-checked before any body is read, so a DNS
  answer that changes between the check and the connection can't reach a private network.
- robots.txt is honored for our user agent (RFC 9309: unreachable 4xx means allowed, 5xx or a
  network failure means disallowed).
- Bounded: 15 s per request, 2 MB of body, 3 redirects, text content types only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
TIMEOUT_S = 15.0
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown")
ROBOTS_TTL_S = 3600


class Refused(Exception):
    """The page can't be read. `code` is stable and machine-readable; nothing is charged."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    encoding: str


def _public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _check_host(url: str, resolve: Any = _resolve) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Refused("unsupported_url", "only http(s) URLs with a host name")
    if parts.username or parts.password:
        raise Refused("unsupported_url", "credentials in URLs are not accepted")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in (80, 443):
        raise Refused("unsupported_url", "only ports 80 and 443")
    try:  # an IP literal is checked as written, never through DNS
        ipaddress.ip_address(parts.hostname)
        ips = [parts.hostname]
    except ValueError:
        ips = None
    try:
        ips = ips or await resolve(parts.hostname, port)
    except OSError as exc:
        raise Refused("dns_failed", parts.hostname) from exc
    if not ips or not all(_public(ip) for ip in ips):
        raise Refused("non_public_address", parts.hostname)


def _peer_is_public(response: httpx.Response) -> bool:
    stream = response.extensions.get("network_stream")
    addr = stream.get_extra_info("server_addr") if stream is not None else None
    return bool(addr) and _public(str(addr[0]))


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve: Any = _resolve,
    ) -> None:
        self.user_agent = user_agent
        self._resolve = resolve
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=TIMEOUT_S,
            follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept": ", ".join(TEXT_TYPES) + ";q=0.9"},
        )
        self._robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}
        # Tests inject a transport without real sockets; peer checks then rely on DNS checks.
        self._check_peer = transport is None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, limit: int) -> tuple[httpx.Response, bytes]:
        await _check_host(url, self._resolve)
        request = self._client.build_request("GET", url)
        response = await self._client.send(request, stream=True)
        try:
            if self._check_peer and not _peer_is_public(response):
                raise Refused("non_public_address", urlsplit(url).hostname or "")
            body = bytearray()
            async for chunk in response.aiter_bytes():  # decoded; caps the decoded size
                body.extend(chunk)
                if len(body) > limit:
                    raise Refused("too_large", f"over {limit} bytes")
        finally:
            await response.aclose()
        return response, bytes(body)

    async def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached is None or time.monotonic() - cached[0] > ROBOTS_TTL_S:
            parser: urllib.robotparser.RobotFileParser | None = None
            try:
                response, body = await self._get(origin + "/robots.txt", 500_000)
                if 200 <= response.status_code < 300:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(body.decode("utf-8", "replace").splitlines())
                elif response.status_code >= 500:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.disallow_all = True
            except Refused:
                raise
            except httpx.HTTPError:
                parser = urllib.robotparser.RobotFileParser()
                parser.disallow_all = True
            cached = (time.monotonic(), parser)
            self._robots[origin] = cached
        parser = cached[1]
        return parser is None or parser.can_fetch(self.user_agent, url)

    async def fetch(self, url: str) -> Page:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if not await self._allowed(current):
                raise Refused("disallowed_by_robots_txt", current)
            try:
                response, body = await self._get(current, MAX_BYTES)
            except httpx.TimeoutException as exc:
                raise Refused("timeout", current) from exc
            except httpx.HTTPError as exc:
                raise Refused("fetch_failed", type(exc).__name__) from exc
            if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                current = urljoin(current, response.headers["location"])
                continue
            if response.status_code >= 400:
                raise Refused("upstream_status", str(response.status_code))
            ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in TEXT_TYPES:
                raise Refused("unsupported_content_type", ctype or "none")
            return Page(url, current, response.status_code, ctype, body,
                        response.charset_encoding or "utf-8")  # fmt: skip
        raise Refused("too_many_redirects", url)
