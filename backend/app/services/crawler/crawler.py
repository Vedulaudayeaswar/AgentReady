"""Polite, bounded website crawler.

Order of discovery: robots.txt -> sitemaps (incl. sitemap indexes) ->
homepage -> breadth-first internal links. Respects robots.txt rules and
Crawl-delay, crawl depth, page limits and a per-request delay.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from lxml import etree

from app.services.crawler.fetcher import FetchResult, SafeFetcher, render_with_playwright
from app.services.crawler.urls import is_crawlable_link, normalize_url, same_site

log = logging.getLogger("agentready.crawler")

PRODUCTISH_URL = re.compile(r"/(products?|item|items|p|shop|catalog(ue)?|store|sku|dp)/", re.I)
LISTING_URL = re.compile(r"(/(category|categories|collections?|tags?|page)/|[?&](page|p|sort|order|filter)=)", re.I)
CARD_CLASS = re.compile(r"(product|item|card|tile)", re.I)

# Crawl priorities (lower first): links from product cards, then product-looking URLs, then
# ordinary pages, then category/pagination pages. Small page budgets therefore reach real
# product detail pages instead of being spent on listings only.
PRIORITY_CARD, PRIORITY_PRODUCT_URL, PRIORITY_DEFAULT, PRIORITY_LISTING = 0, 1, 2, 3
MAX_SITEMAP_URLS = 5000
MAX_SITEMAP_FILES = 10


@dataclass
class CrawlConfig:
    max_pages: int = 50
    max_depth: int = 3
    delay_seconds: float = 1.0
    use_sitemap: bool = True
    render_js: bool = False


@dataclass
class CrawledDocument:
    url: str
    depth: int
    fetch: FetchResult
    html: str | None
    render_mode: str = "http"


@dataclass
class CrawlStats:
    discovered: int = 0
    crawled: int = 0
    failed: int = 0
    skipped_robots: int = 0
    sitemap_urls: int = 0
    errors: list[dict] = field(default_factory=list)


class Robots:
    def __init__(self, text: str | None, user_agent: str):
        self.user_agent = user_agent
        self.parser = RobotFileParser()
        self.parser.parse((text or "").splitlines())
        self.sitemaps = [l.split(":", 1)[1].strip() for l in (text or "").splitlines() if l.lower().startswith("sitemap:")]
        delay = None
        try:
            delay = self.parser.crawl_delay(user_agent)
        except Exception:  # pragma: no cover
            pass
        self.crawl_delay = float(delay) if delay else None

    def allowed(self, url: str) -> bool:
        return self.parser.can_fetch(self.user_agent, url)


def looks_js_rendered(html: str) -> bool:
    """Heuristic: very little visible text, a SPA root node and many scripts."""
    soup = BeautifulSoup(html, "lxml")
    scripts = len(soup.find_all("script"))
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    text = soup.get_text(" ", strip=True)
    root = soup.find(id=re.compile(r"^(root|app|__next|__nuxt)$"))
    return len(text) < 200 and (root is not None or scripts >= 3)


def parse_sitemap(xml: bytes) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemaps)."""
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=True)
        root = etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    tag = etree.QName(root).localname
    locs = [el.text.strip() for el in root.iter() if etree.QName(el).localname == "loc" and el.text]
    if tag == "sitemapindex":
        return [], locs[:MAX_SITEMAP_FILES]
    return locs[:MAX_SITEMAP_URLS], []


def link_priority(url: str, in_card: bool) -> int:
    if LISTING_URL.search(url):
        return PRIORITY_LISTING
    if in_card:
        return PRIORITY_CARD
    if PRODUCTISH_URL.search(url):
        return PRIORITY_PRODUCT_URL
    return PRIORITY_DEFAULT


def extract_links_prioritized(html: str, base_url: str) -> list[tuple[str, int]]:
    soup = BeautifulSoup(html, "lxml")
    base_tag = soup.find("base", href=True)
    base = normalize_url(base_tag["href"], base_url) if base_tag else base_url
    out: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if a.get("rel") and "nofollow" in a.get("rel"):
            continue
        if not is_crawlable_link(href):
            continue
        try:
            url = normalize_url(href, base)
        except ValueError:
            continue
        in_card = any(isinstance(cls, str) and CARD_CLASS.search(cls)
                      for parent in a.parents if parent.name in ("article", "li", "div")
                      for cls in (parent.get("class") or []))
        prio = link_priority(url, in_card)
        out[url] = min(prio, out.get(url, prio))
    return list(out.items())


def extract_links(html: str, base_url: str) -> list[str]:
    return [u for u, _ in extract_links_prioritized(html, base_url)]


class Crawler:
    def __init__(self, fetcher: SafeFetcher, config: CrawlConfig):
        self.fetcher = fetcher
        self.config = config
        self.stats = CrawlStats()
        self.robots: Robots | None = None

    async def _load_robots(self, root: str) -> Robots:
        p = urlsplit(root)
        robots_url = urlunsplit((p.scheme, p.netloc, "/robots.txt", "", ""))
        res = await self.fetcher.fetch(robots_url)
        text = res.text if res.ok else None
        if res.status_code in (401, 403) or (res.status_code is not None and res.status_code >= 500):
            # RFC 9309: a 5xx robots.txt means "assume complete disallow"; 401/403 are treated the same
            # way (conservative). A missing robots.txt (404) means everything is allowed.
            text = "User-agent: *\nDisallow: /"
            self.stats.errors.append({"url": robots_url, "error": f"robots.txt returned HTTP {res.status_code}; "
                                                                  "the site does not permit crawling right now"})
        return Robots(text, self.fetcher.s.crawler_user_agent)

    async def _sitemap_urls(self, root: str) -> list[str]:
        assert self.robots is not None
        p = urlsplit(root)
        queue = deque(self.robots.sitemaps or [urlunsplit((p.scheme, p.netloc, "/sitemap.xml", "", ""))])
        seen: set[str] = set()
        pages: list[str] = []
        while queue and len(seen) < MAX_SITEMAP_FILES:
            sm = queue.popleft()
            if sm in seen or not same_site(sm, root):
                continue
            seen.add(sm)
            res = await self.fetcher.fetch(sm)
            if not res.ok:
                continue
            urls, children = parse_sitemap(res.content)
            pages.extend(urls)
            queue.extend(children)
        return pages

    async def crawl(
        self,
        root_url: str,
        on_page: Callable[[CrawledDocument], Awaitable[None]],
        should_stop: Callable[[], Awaitable[bool]] | None = None,
        on_discovered: Callable[[int], Awaitable[None]] | None = None,
    ) -> CrawlStats:
        cfg = self.config
        root = normalize_url(root_url)
        self.robots = await self._load_robots(root)
        delay = max(cfg.delay_seconds, min(self.robots.crawl_delay or 0, 10.0))

        seen: set[str] = {root}
        counter = itertools.count()
        # heap of (priority, depth, insertion order, url)
        queue: list[tuple[int, int, int, str]] = [(-1, 0, next(counter), root)]
        if cfg.use_sitemap:
            sm_urls = await self._sitemap_urls(root)
            self.stats.sitemap_urls = len(sm_urls)
            for u in sm_urls:
                try:
                    nu = normalize_url(u)
                except ValueError:
                    continue
                if nu not in seen and same_site(nu, root) and is_crawlable_link(nu):
                    seen.add(nu)
                    heapq.heappush(queue, (link_priority(nu, False), 1, next(counter), nu))
        self.stats.discovered = len(seen)
        if on_discovered:
            await on_discovered(self.stats.discovered)

        first = True
        while queue and self.stats.crawled + self.stats.failed < cfg.max_pages:
            if should_stop and await should_stop():
                break
            _, depth, _, url = heapq.heappop(queue)
            if not self.robots.allowed(url):
                self.stats.skipped_robots += 1
                continue
            if not first:
                await asyncio.sleep(delay)
            first = False
            res = await self.fetcher.fetch(url)
            final_url = normalize_url(res.redirects[-1]) if res.redirects else url
            if res.redirects and not same_site(final_url, root):
                self.stats.failed += 1
                self.stats.errors.append({"url": url, "error": "redirected off-site"})
                continue
            if not res.ok:
                self.stats.failed += 1
                self.stats.errors.append({"url": url, "error": res.error or f"HTTP {res.status_code}"})
                await on_page(CrawledDocument(url=final_url, depth=depth, fetch=res, html=None))
                continue
            html = res.text
            mode = "http"
            if cfg.render_js and looks_js_rendered(html):
                try:
                    rendered = await render_with_playwright(final_url, self.fetcher)
                    if rendered:
                        html, mode = rendered, "playwright"
                except Exception as e:  # rendering is best-effort
                    self.stats.errors.append({"url": url, "error": f"render failed: {type(e).__name__}"})
            self.stats.crawled += 1
            await on_page(CrawledDocument(url=final_url, depth=depth, fetch=res, html=html, render_mode=mode))

            if depth < cfg.max_depth:
                new = 0
                for link, prio in extract_links_prioritized(html, final_url):
                    if link not in seen and same_site(link, root):
                        seen.add(link)
                        heapq.heappush(queue, (prio, depth + 1, next(counter), link))
                        new += 1
                if new:
                    self.stats.discovered = len(seen)
                    if on_discovered:
                        await on_discovered(self.stats.discovered)
        return self.stats
