"""SSRF-safe HTTP client for commerce APIs.

Re-uses the crawler's destination checks (public IPs only, pinned connection, allowed ports),
requires HTTPS (credentials are sent), never follows redirects (a redirect could forward
credentials to another host), caps response size and retries 429/5xx with backoff.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from app.core.config import get_settings
from app.services.crawler import fetcher as fetcher_mod
from app.services.crawler.fetcher import BlockedURL, SafeFetcher

# test hook: the automated tests serve mocked provider APIs from memory
TRANSPORT_OVERRIDE: httpx.AsyncBaseTransport | None = None
MAX_BYTES = 10_000_000


class IntegrationHTTPError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status, self.retryable = status, retryable


@dataclass
class APIResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self):
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            raise IntegrationHTTPError("The store returned a response that is not JSON", self.status)


class SafeAPIClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, resolver=None, timeout: float = 30.0,
                 retries: int | None = None, backoff: float | None = None):
        s = get_settings()
        self._guard = SafeFetcher(s, transport=transport or TRANSPORT_OVERRIDE, resolver=resolver or fetcher_mod.RESOLVER_OVERRIDE)
        self._client = httpx.AsyncClient(transport=transport or TRANSPORT_OVERRIDE, timeout=timeout, follow_redirects=False,
                                         trust_env=False, headers={"User-Agent": "AgentReady/0.2 (+commerce-sync)",
                                                                   "Accept": "application/json"})
        self.retries = s.integration_max_retries if retries is None else retries
        self.backoff = s.integration_retry_base_seconds if backoff is None else backoff
        self.allow_http = s.allow_private_networks  # local development stores only

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._guard.aclose()

    async def request(self, method: str, url: str, *, headers: dict | None = None, params: dict | None = None,
                      json_body=None, auth: tuple[str, str] | None = None, content: bytes | None = None) -> APIResponse:
        parts = urlsplit(url)
        if parts.scheme != "https" and not self.allow_http:
            raise IntegrationHTTPError("Store connections must use https:// (credentials are sent with each request)")
        if params:
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))
        last: IntegrationHTTPError | None = None
        for attempt in range(self.retries + 1):
            try:
                host, ip = await self._guard.check_url(url)
            except BlockedURL as e:
                raise IntegrationHTTPError(f"Store address rejected: {e}")
            p = urlsplit(url)
            ip_host = f"[{ip}]" if ":" in ip else ip
            pinned = urlunsplit((p.scheme, f"{ip_host}:{p.port}" if p.port else ip_host, p.path or "/", p.query, ""))
            hdrs = {"Host": f"{host}:{p.port}" if p.port else host, **(headers or {})}
            if auth is not None:
                import base64

                hdrs["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            req = self._client.build_request(method, pinned, headers=hdrs, json=json_body if content is None else None,
                                             content=content,
                                             extensions={"sni_hostname": host} if p.scheme == "https" else {})
            try:
                resp = await self._client.send(req, stream=True)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = IntegrationHTTPError(f"Network error contacting the store ({type(e).__name__})", None, True)
            else:
                try:
                    if resp.is_redirect:
                        raise IntegrationHTTPError(
                            f"The store redirected to {resp.headers.get('location', 'another address')}; use that URL", resp.status_code)
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_BYTES:
                            raise IntegrationHTTPError("The store response was too large")
                    result = APIResponse(resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, bytes(buf))
                finally:
                    await resp.aclose()
                if result.status == 429 or result.status >= 500:
                    last = IntegrationHTTPError(f"The store API returned HTTP {result.status}", result.status, True)
                    if attempt < self.retries:
                        delay = self.backoff * (2 ** attempt)
                        if result.status == 429:
                            try:
                                delay = min(float(result.headers.get("retry-after", delay)), 30.0)
                            except ValueError:
                                pass
                        await asyncio.sleep(delay)
                        continue
                    raise last
                return result
            if attempt < self.retries:
                await asyncio.sleep(self.backoff * (2 ** attempt))
        raise last or IntegrationHTTPError("Request failed")
