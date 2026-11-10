"""In-memory fixture websites served through httpx.MockTransport.

Requests arrive pinned to an IP (see SafeFetcher); the original hostname is
in the Host header, exactly like production traffic.
"""

from __future__ import annotations

from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC_IP = "93.184.216.34"  # documentation/public range; never contacted (mock transport)

SHOP_ROUTES = {
    "/": "index.html",
    "/robots.txt": "robots.txt",
    "/sitemap.xml": "sitemap.xml",
    "/products/dc-motor-12v": "dc-motor-12v.html",
    "/products/steel-bolt-m10": "steel-bolt-m10.html",
    "/products/gear-pump": "gear-pump.html",
    "/products/heavy-motor": "heavy-motor.html",
    "/products/broken-page": "broken-page.html",
    "/products/promo-item": "promo-item.html",
    "/price-list": "price-list.html",
    "/app": "app.html",
    "/about": "about.html",
    "/blog/new-arrivals": "blog.html",
    "/private/secret-product": "dc-motor-12v.html",
}


class FixtureSite:
    def __init__(self, host: str = "shop.example", routes: dict[str, str] | None = None, folder: str = "shop"):
        self.host = host
        self.routes = dict(routes or SHOP_ROUTES)
        self.folder = FIXTURES / folder
        self.overrides: dict[str, str | int] = {}
        self.requested: list[str] = []

    def body(self, path: str) -> str | int | None:
        if path in self.overrides:
            return self.overrides[path]
        name = self.routes.get(path)
        if name is None:
            return None
        return (self.folder / name).read_text(encoding="utf-8")

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "").split(":")[0]
        path = request.url.path
        self.requested.append(path)
        if host != self.host:
            return httpx.Response(404, text="unknown host")
        content = self.body(path)
        if content is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        if isinstance(content, int):
            return httpx.Response(content, text="error", headers={"content-type": "text/html"})
        ctype = "text/plain" if path.endswith(".txt") else ("application/xml" if path.endswith(".xml") else "text/html; charset=utf-8")
        return httpx.Response(200, text=content, headers={"content-type": ctype})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


async def public_resolver(host: str, port: int) -> list[str]:
    return [PUBLIC_IP]
