"""SSRF-hardened HTTP fetcher.

Defences:
* scheme allow-list (http/https) and outbound port allow-list;
* DNS resolution performed by us; every resolved address must be public
  (blocks loopback, RFC1918, link-local / cloud metadata, CGNAT, multicast,
  reserved, IPv4-mapped IPv6 tricks);
* the connection is pinned to the validated IP (Host header + TLS SNI keep
  the original hostname) so DNS rebinding between check and connect fails;
* redirects are followed manually and every hop is re-validated;
* response bodies are streamed and aborted beyond ``max_response_bytes``;
* only text-like content types are read.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.core.config import Settings, get_settings

log = logging.getLogger("agentready.crawler")

BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata", "instance-data"}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")
ALWAYS_BLOCKED_IPS = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}
TEXT_TYPES = ("text/html", "application/xhtml+xml", "application/xml", "text/xml", "text/plain")

Resolver = Callable[[str, int], Awaitable[list[str]]]

# Test hooks: the automated test-suite serves fixture websites from memory.
# They are only read when no transport/resolver is passed explicitly.
TRANSPORT_OVERRIDE: httpx.AsyncBaseTransport | None = None
RESOLVER_OVERRIDE: "Resolver | None" = None


class BlockedURL(Exception):
    """The URL targets a destination AgentReady refuses to contact."""


async def default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip in ALWAYS_BLOCKED_IPS:
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


@dataclass
class FetchResult:
    url: str
    status_code: int | None = None
    content_type: str | None = None
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None and 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        charset = "utf-8"
        ct = self.content_type or ""
        if "charset=" in ct:
            charset = ct.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


class SafeFetcher:
    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ):
        self.s = settings or get_settings()
        self.resolver = resolver or RESOLVER_OVERRIDE or default_resolver
        self._client = httpx.AsyncClient(
            transport=transport or TRANSPORT_OVERRIDE,
            timeout=httpx.Timeout(self.s.crawl_timeout_seconds, connect=min(10.0, self.s.crawl_timeout_seconds)),
            follow_redirects=False,
            headers={"User-Agent": self.s.crawler_user_agent, "Accept-Language": "en;q=0.9,*;q=0.5"},
            trust_env=False,  # never route through env-configured proxies
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SafeFetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    async def check_url(self, url: str) -> tuple[str, str]:
        """Validate ``url``; return (hostname, pinned_ip). Raises BlockedURL."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise BlockedURL(f"scheme '{parts.scheme}' is not allowed")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise BlockedURL("missing hostname")
        if parts.username or parts.password:
            raise BlockedURL("credentials in URL are not allowed")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in self.s.allowed_port_set:
            raise BlockedURL(f"port {port} is not allowed")
        if not self.s.allow_private_networks and (host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES)):
            raise BlockedURL(f"host '{host}' is not allowed")
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
            addrs = [str(literal)]
        except ValueError:
            try:
                addrs = await self.resolver(host, port)
            except (socket.gaierror, OSError) as e:
                raise BlockedURL(f"DNS resolution failed for '{host}'") from e
        if not addrs:
            raise BlockedURL(f"'{host}' did not resolve")
        for a in addrs:
            ip = ipaddress.ip_address(a.split("%")[0])
            if ip in ALWAYS_BLOCKED_IPS:
                raise BlockedURL("cloud metadata endpoints are blocked")
            if not self.s.allow_private_networks and not ip_is_public(ip):
                raise BlockedURL(f"'{host}' resolves to a non-public address")
        return host, addrs[0]

    def _pinned_request(self, url: str, host: str, ip: str) -> httpx.Request:
        parts = urlsplit(url)
        port = parts.port
        ip_host = f"[{ip}]" if ":" in ip else ip
        netloc = f"{ip_host}:{port}" if port else ip_host
        pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        host_header = f"{host}:{port}" if port else host
        extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
        return self._client.build_request("GET", pinned, headers={"Host": host_header}, extensions=extensions)

    async def _single(self, url: str, accept: tuple[str, ...]) -> tuple[FetchResult, str | None]:
        host, ip = await self.check_url(url)
        req = self._pinned_request(url, host, ip)
        resp = await self._client.send(req, stream=True)
        try:
            result = FetchResult(url=url, status_code=resp.status_code, headers=dict(resp.headers))
            result.content_type = resp.headers.get("content-type", "").lower() or None
            if resp.is_redirect:
                loc = resp.headers.get("location")
                return result, urljoin(url, loc) if loc else None
            if resp.status_code >= 400:
                return result, None
            ctype = (result.content_type or "").split(";")[0].strip()
            if accept and ctype and not any(ctype.startswith(t) for t in accept):
                result.error = f"unsupported content type '{ctype}'"
                return result, None
            declared = int(resp.headers.get("content-length") or 0)
            if declared > self.s.max_response_bytes:
                result.error = "response exceeds size limit"
                return result, None
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > self.s.max_response_bytes:
                    result.error = "response exceeds size limit"
                    return result, None
            result.content = bytes(buf)
            return result, None
        finally:
            await resp.aclose()
            # Stateless crawling: never keep cookies, so pages are not personalised by session state
            # (carts, "recently viewed" strips) and no site session data is retained.
            self._client.cookies.clear()

    async def fetch(self, url: str, accept: tuple[str, ...] = TEXT_TYPES) -> FetchResult:
        redirects: list[str] = []
        current = url
        for attempt in range(self.s.crawl_max_retries + 1):
            try:
                hops = 0
                current = url
                redirects = []
                while True:
                    result, nxt = await self._single(current, accept)
                    if nxt is None:
                        break
                    hops += 1
                    if hops > self.s.max_redirects:
                        return FetchResult(url=current, error="too many redirects", redirects=redirects)
                    redirects.append(nxt)
                    current = nxt
                result.redirects = redirects
                retryable = result.status_code in (429, 500, 502, 503, 504)
                if retryable and attempt < self.s.crawl_max_retries:
                    delay = 0.5 * (2**attempt)
                    if result.status_code == 429:
                        try:
                            delay = min(float(result.headers.get("retry-after", delay)), 10.0)
                        except ValueError:
                            pass
                    await asyncio.sleep(delay)
                    continue
                return result
            except BlockedURL as e:
                return FetchResult(url=current, error=f"blocked: {e}", redirects=redirects)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt < self.s.crawl_max_retries:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                return FetchResult(url=current, error=f"network error: {type(e).__name__}", redirects=redirects)
        return FetchResult(url=current, error="exhausted retries")  # pragma: no cover


async def render_with_playwright(url: str, fetcher: SafeFetcher, timeout_ms: int = 20000) -> str | None:
    """Render a permitted JavaScript page. Every sub-request is SSRF-checked.

    Returns rendered HTML, or None if Playwright is unavailable/disabled.
    """
    if not fetcher.s.playwright_enabled:
        return None
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        log.warning("PLAYWRIGHT_ENABLED is true but playwright is not installed")
        return None

    async def guard(route):
        req = route.request
        if req.resource_type in ("image", "media", "font"):
            return await route.abort()
        try:
            await fetcher.check_url(req.url)
        except BlockedURL:
            return await route.abort()
        return await route.continue_()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(
                user_agent=fetcher.s.crawler_user_agent, accept_downloads=False, service_workers="block"
            )
            page = await ctx.new_page()
            await page.route("**/*", guard)
            await page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            html = await page.content()
            return html[: fetcher.s.max_response_bytes]
        finally:
            await browser.close()
