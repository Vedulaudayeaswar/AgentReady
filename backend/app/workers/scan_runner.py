"""Scan orchestration: crawl -> discover -> extract -> normalize -> validate -> catalog.

Progress written to ``scan_jobs`` is real: stage transitions happen when the
work actually starts, and counters reflect pages/products processed so far.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import session_factory
from app.models import ACTIVE_SCAN_STATUSES, Business, CrawledPage, ScanJob, Website
from app.schemas.canonical import ExtractedProduct
from app.services.ai.gateway import AIGateway, build_provider
from app.services.catalog.service import apply_scan, build_snapshots
from app.services.commerce.audit import Actor, record
from app.services.commerce.sync import apply_conflict_policy, finalize_scan_sync
from app.services.crawler.crawler import CrawlConfig, CrawledDocument, Crawler
from app.services.crawler.fetcher import BlockedURL, SafeFetcher
from app.services.discovery.classifier import classify_page
from app.services.extraction.limits import PageTooComplex, check_page_complexity
from app.services.extraction.pipeline import PageExtractor
from app.services.extraction.structured import parse_meta
from app.services.normalization.product import dedupe_within_scan, finalize

log = logging.getLogger("agentready.scan")

STAGES = ["connecting", "discovering_pages", "finding_products", "extracting", "normalizing", "validating",
          "generating_catalog"]
MAX_HTML_KEEP = 2_000_000


class ScanCancelled(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ScanRunner:
    def __init__(self, session: AsyncSession, scan_id: uuid.UUID, settings: Settings | None = None,
                 fetcher: SafeFetcher | None = None, ai_transport=None):
        self.session = session
        self.scan_id = scan_id
        self.s = settings or get_settings()
        self._fetcher = fetcher
        self._ai_transport = ai_transport

    async def _stage(self, scan: ScanJob, stage: str) -> None:
        await self._check_cancel(scan)
        hist = list(scan.stage_history or [])
        if hist and not hist[-1].get("finished_at"):
            hist[-1] = {**hist[-1], "finished_at": _now().isoformat()}
        hist.append({"stage": stage, "started_at": _now().isoformat(), "finished_at": None})
        scan.stage_history = hist
        scan.stage = stage
        await self.session.commit()

    async def _check_cancel(self, scan: ScanJob) -> None:
        await self.session.refresh(scan, ["cancel_requested"])
        if scan.cancel_requested:
            raise ScanCancelled()

    def _error(self, scan: ScanJob, stage: str, message: str, url: str | None = None, level: str = "error") -> None:
        """Record a scan message. level: info (expected), warning (partial result), error (scan failed)."""
        errs = list(scan.errors or [])
        if len(errs) < 200:
            errs.append({"stage": stage, "level": level, "message": message[:500], "url": url, "at": _now().isoformat()})
        scan.errors = errs

    async def run(self) -> None:
        scan = await self.session.get(ScanJob, self.scan_id)
        if scan is None or scan.status not in ("queued",):
            return
        website = await self.session.get(Website, scan.website_id)
        business = await self.session.get(Business, scan.business_id)
        assert website is not None and business is not None
        scan.status = "running"
        scan.started_at = _now()
        await finalize_scan_sync(self.session, scan)
        await self.session.commit()

        fetcher = self._fetcher or SafeFetcher(self.s)
        provider = None
        try:
            provider = build_provider(self.s, transport=self._ai_transport)
            await self._run(scan, website, business, fetcher, provider)
        except ScanCancelled:
            scan.status = "cancelled"
            self._error(scan, scan.stage, "Scan cancelled by user", level="info")
        except Exception as e:  # pragma: no cover - safety net, recorded for the merchant
            log.exception("scan %s failed", self.scan_id)
            await self.session.rollback()
            scan = await self.session.get(ScanJob, self.scan_id)
            assert scan is not None
            scan.status = "failed"
            # details go to the server log only; merchants see a generic message
            self._error(scan, scan.stage, f"The scan stopped because of an internal error ({type(e).__name__}).")
        finally:
            hist = list(scan.stage_history or [])
            if hist and not hist[-1].get("finished_at"):
                hist[-1] = {**hist[-1], "finished_at": _now().isoformat()}
                scan.stage_history = hist
            scan.finished_at = _now()
            website.last_scan_at = _now()
            try:
                await finalize_scan_sync(self.session, scan)
                outcome = {"completed": "scan.completed", "awaiting_review": "scan.awaiting_review",
                           "failed": "scan.failed", "cancelled": "scan.cancelled"}.get(scan.status, "scan.finished")
                summary = scan.change_summary or {}
                record(self.session, scan.business_id, Actor.system("Website scan"), outcome, "website", scan.website_id,
                       f"Scan of {website.normalized_url} {scan.status.replace('_', ' ')}: {scan.products_extracted} products"
                       + (f", {summary.get('new', 0)} new, {summary.get('changed', 0)} changed, "
                          f"{summary.get('conflicts', 0)} conflicts" if scan.is_rescan else ""),
                       {"scan_id": scan.id, "status": scan.status})
            except Exception:  # history must never break scan bookkeeping
                log.exception("sync bookkeeping failed for scan %s", scan.id)
            await self.session.commit()
            if self._fetcher is None:
                await fetcher.aclose()
            if provider is not None:
                await provider.aclose()

    async def _run(self, scan, website, business, fetcher, provider) -> None:
        cfg = scan.settings or {}
        # ---------------------------------------------------------- connecting
        await self._stage(scan, "connecting")
        try:
            await fetcher.check_url(website.normalized_url)
        except BlockedURL as e:
            scan.status = "failed"
            self._error(scan, "connecting", f"Website URL rejected: {e}", website.normalized_url)
            return
        home = await fetcher.fetch(website.normalized_url)
        if not home.ok:
            scan.status = "failed"
            self._error(scan, "connecting", f"Could not load homepage: {home.error or f'HTTP {home.status_code}'}",
                        website.normalized_url)
            return

        # ---------------------------------------------------------- discovering
        await self._stage(scan, "discovering_pages")
        crawl_cfg = CrawlConfig(
            max_pages=min(int(cfg.get("max_pages") or website.max_pages), self.s.max_pages_per_scan),
            max_depth=min(int(cfg.get("max_depth") or website.max_depth), self.s.max_crawl_depth),
            delay_seconds=float(cfg.get("delay_seconds", self.s.crawl_delay_seconds)),
            use_sitemap=bool(cfg.get("use_sitemap", website.use_sitemap)),
            render_js=bool(cfg.get("render_js", website.render_js)) and self.s.playwright_enabled,
        )
        crawler = Crawler(fetcher, crawl_cfg)
        docs: list[tuple[CrawledDocument, CrawledPage]] = []

        async def on_page(doc: CrawledDocument) -> None:
            html = doc.html[:MAX_HTML_KEEP] if doc.html else None
            page = CrawledPage(
                scan_id=scan.id, business_id=scan.business_id, url=doc.url, depth=doc.depth,
                status_code=doc.fetch.status_code, content_type=doc.fetch.content_type,
                render_mode=doc.render_mode, byte_size=len(doc.fetch.content), error=doc.fetch.error,
                content_hash=hashlib.sha256(doc.fetch.content).hexdigest() if doc.fetch.content else None,
            )
            self.session.add(page)
            docs.append((CrawledDocument(doc.url, doc.depth, doc.fetch, html, doc.render_mode), page))
            scan.pages_crawled = crawler.stats.crawled
            scan.pages_failed = crawler.stats.failed
            scan.pages_skipped_robots = crawler.stats.skipped_robots
            await self.session.commit()

        async def on_discovered(n: int) -> None:
            scan.pages_discovered = n
            await self.session.commit()

        async def should_stop() -> bool:
            await self.session.refresh(scan, ["cancel_requested"])
            return scan.cancel_requested

        stats = await crawler.crawl(website.normalized_url, on_page, should_stop, on_discovered)
        await self._check_cancel(scan)
        scan.pages_crawled, scan.pages_failed = stats.crawled, stats.failed
        scan.pages_skipped_robots = stats.skipped_robots
        for err in stats.errors[:50]:
            self._error(scan, "discovering_pages", err["error"], err["url"], level="warning")
        if stats.crawled == 0:
            scan.status = "failed"
            self._error(scan, "discovering_pages", "No pages could be crawled (robots.txt or errors)")
            return

        # ---------------------------------------------------------- finding products
        await self._stage(scan, "finding_products")
        candidates: list[tuple[CrawledDocument, CrawledPage]] = []
        for doc, page in docs:
            if not doc.html:
                continue
            try:
                check_page_complexity(doc.html)
                soup = BeautifulSoup(doc.html, "lxml")
                c = classify_page(doc.url, soup, parse_meta(soup))
            except PageTooComplex as e:
                self._error(scan, "finding_products", f"Page skipped: {e}", doc.url, level="warning")
                page.error = f"skipped: {e}"
                continue
            except (RecursionError, ValueError, TypeError, AttributeError) as e:
                self._error(scan, "finding_products", f"Page could not be analysed: {type(e).__name__}", doc.url, level="warning")
                page.error = f"analysis failed: {type(e).__name__}"
                continue
            page.page_type, page.product_score = c.page_type, max(c.score, c.listing_score)
            page.signals = {"reasons": c.reasons, "ambiguous": c.ambiguous, "listing_score": c.listing_score}
            page.title = soup.title.get_text(strip=True)[:500] if soup.title else None
            if c.page_type != "other" or c.ambiguous:
                candidates.append((doc, page))
        scan.product_pages_found = sum(1 for _, p in candidates if p.page_type != "other")
        await self.session.commit()

        # ---------------------------------------------------------- extracting
        await self._stage(scan, "extracting")
        max_products = min(int(cfg.get("max_products") or self.s.max_products_per_scan), self.s.max_products_per_scan)
        gateway = AIGateway(provider, self.session, scan.business_id, scan.id, self.s) if provider else None
        if provider is None and self.s.ai_provider == "groq":
            self._error(scan, "extracting", "GROQ_API_KEY not configured: deterministic extraction only (no AI)", level="info")
        extractor = PageExtractor(gateway, self.s, methods=cfg.get("methods"))
        extracted: list[ExtractedProduct] = []
        for doc, page in candidates:
            await self._check_cancel(scan)
            try:
                outcome = await extractor.extract(doc.url, doc.html or "")
            except Exception as e:
                self._error(scan, "extracting", f"Extraction failed on this page ({type(e).__name__})", doc.url, level="warning")
                log.warning("extraction failed for %s", doc.url, exc_info=e)
                continue
            page.page_type = outcome.classification.page_type
            page.text_excerpt = outcome.text_excerpt
            page.signals = {**(page.signals or {}), "reasons": outcome.classification.reasons,
                            "used_ai": outcome.result.used_ai, "products": len(outcome.result.products)}
            extracted.extend(outcome.result.products)
            scan.products_extracted = len(extracted)
            scan.ai_calls = gateway.calls if gateway else 0
            await self.session.commit()
            if len(extracted) >= max_products * 2:
                self._error(scan, "extracting", "Stopped early: product limit reached", level="warning")
                break
        scan.product_pages_found = sum(1 for _, p in candidates if p.page_type != "other")
        if gateway and gateway.disabled_reason:
            self._error(scan, "extracting", f"AI disabled during scan: {gateway.disabled_reason}", level="warning")

        # ---------------------------------------------------------- normalizing
        await self._stage(scan, "normalizing")
        products = [finalize(p, business.default_currency) for p in extracted]
        products = dedupe_within_scan([p for p in products if p.name])
        if len(products) > max_products:
            self._error(scan, "normalizing", f"Product limit reached: kept first {max_products} of {len(products)}", level="warning")
            products = products[:max_products]
        scan.products_extracted = len(products)
        await self.session.commit()

        # ---------------------------------------------------------- validating + catalog
        await self._stage(scan, "validating")
        page_ids = {p.url: p.id for _, p in docs}
        crawled_ok = {d.url for d, _ in docs if d.html}
        gone = {d.url for d, _ in docs if d.fetch.status_code in (404, 410)}
        summary = await build_snapshots(self.session, scan, products, page_ids, crawled_ok, gone)
        scan.change_summary = summary
        await self.session.commit()
        auto_resolved = await apply_conflict_policy(self.session, scan)
        if auto_resolved:
            scan.change_summary = {**summary, "conflicts_auto_resolved": auto_resolved}
        await self.session.commit()

        await self._stage(scan, "generating_catalog")
        if scan.is_rescan and (summary["changed"] or summary["removed"] or summary["new"]) and not cfg.get("auto_apply"):
            scan.status = "awaiting_review"
        else:
            counts = await apply_scan(self.session, scan)
            scan.change_summary = {**summary, "applied": counts}
            scan.status = "completed"
        await self.session.commit()


async def run_scan(scan_id: uuid.UUID, **kwargs) -> None:
    async with session_factory()() as session:
        await ScanRunner(session, scan_id, **kwargs).run()


async def has_active_scan(session: AsyncSession, website_id: uuid.UUID) -> bool:
    q = select(ScanJob.id).where(ScanJob.website_id == website_id, ScanJob.status.in_(ACTIVE_SCAN_STATUSES))
    return (await session.execute(q)).first() is not None
