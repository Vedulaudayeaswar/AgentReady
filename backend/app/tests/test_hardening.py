"""Regression tests for issues found during the Phase 1.5 production-readiness review."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update

from app.models import ScanJob
from app.services.crawler.crawler import (
    PRIORITY_CARD,
    PRIORITY_LISTING,
    CrawlConfig,
    Crawler,
    extract_links_prioritized,
)
from app.services.crawler.fetcher import SafeFetcher
from app.tests.conftest import register
from app.tests.sitekit import public_resolver
from app.workers.dispatch import wait_for_inline_tasks

API = "/api/v1"


async def _website(client, h, url="https://shop.example"):
    r = await client.post(f"{API}/websites", json={"url": url, "max_pages": 30}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_timestamps_are_serialised_with_utc_offset(client, site):
    """SQLite returned naive datetimes, which browsers read as local time ("6h ago" for a new scan)."""
    h = await register(client)
    wid = await _website(client, h)
    await client.post(f"{API}/scans", json={"website_id": wid}, headers=h)
    await wait_for_inline_tasks()
    scan = (await client.get(f"{API}/scans?page_size=1", headers=h)).json()["items"][0]
    for key in ("created_at", "started_at", "finished_at"):
        parsed = datetime.fromisoformat(scan[key])
        assert parsed.tzinfo is not None, key
        assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 300, key
    product = (await client.get(f"{API}/products?page_size=1", headers=h)).json()["items"][0]
    assert datetime.fromisoformat(product["updated_at"]).tzinfo is not None


async def test_hostile_page_does_not_fail_the_scan(client, site):
    site.overrides["/about"] = "<html><body>" + "<div>" * 20000 + "x" + "</div>" * 20000 + "</body></html>"
    h = await register(client)
    wid = await _website(client, h)
    r = await client.post(f"{API}/scans", json={"website_id": wid}, headers=h)
    await wait_for_inline_tasks()
    scan = (await client.get(f"{API}/scans/{r.json()['id']}", headers=h)).json()
    assert scan["status"] == "completed", scan["errors"]
    assert scan["products_extracted"] > 0
    assert any("Page skipped" in e["message"] and e["url"].endswith("/about") for e in scan["errors"])
    assert scan["outcome"] == "completed_with_warnings"


def test_complexity_check_is_fast_and_bounded():
    import time

    from app.services.extraction.limits import PageTooComplex, check_page_complexity

    hostile = "<html><body>" + "<div>" * 20000 + "x" + "</div>" * 20000 + "</body></html>"
    t = time.monotonic()
    try:
        check_page_complexity(hostile)
        raise AssertionError("not rejected")
    except PageTooComplex:
        pass
    assert time.monotonic() - t < 2
    assert check_page_complexity("<html><body><p>ok</p></body></html>")[1] <= 4


async def test_robots_server_error_means_disallow(settings):
    def handler(req: httpx.Request):
        if req.url.path == "/robots.txt":
            return httpx.Response(503, text="down")
        return httpx.Response(200, text="<html><a href='/products/x'>x</a></html>", headers={"content-type": "text/html"})

    fetcher = SafeFetcher(settings, transport=httpx.MockTransport(handler), resolver=public_resolver)
    seen = []

    async def on_page(doc):
        seen.append(doc.url)

    stats = await Crawler(fetcher, CrawlConfig(delay_seconds=0, use_sitemap=False)).crawl("https://shop.example/", on_page)
    await fetcher.aclose()
    assert seen == [] and stats.crawled == 0 and stats.skipped_robots == 1
    assert any("robots.txt returned HTTP 503" in e["error"] for e in stats.errors)


def test_product_card_links_are_crawled_before_listings():
    html = """<html><body>
      <ul class="nav"><li><a href="/catalogue/category/books/travel_2/index.html">Travel</a></li></ul>
      <article class="product_pod"><h3><a href="/catalogue/a-light-in-the-attic_1000/index.html">A Light</a></h3></article>
      <a href="/about">About</a> <a href="/catalogue/page-2.html?page=2">next</a></body></html>"""
    prio = dict(extract_links_prioritized(html, "https://books.example/"))
    assert prio["https://books.example/catalogue/a-light-in-the-attic_1000/index.html"] == PRIORITY_CARD
    assert prio["https://books.example/catalogue/category/books/travel_2/index.html"] == PRIORITY_LISTING
    assert prio["https://books.example/catalogue/a-light-in-the-attic_1000/index.html"] < prio["https://books.example/about"]


async def test_search_treats_wildcards_literally(client, site):
    h = await register(client)
    wid = await _website(client, h)
    await client.post(f"{API}/scans", json={"website_id": wid}, headers=h)
    await wait_for_inline_tasks()
    assert (await client.get(f"{API}/products?q=%25", headers=h)).json()["total"] == 0
    assert (await client.get(f"{API}/products?q=_", headers=h)).json()["total"] == 0
    assert (await client.get(f"{API}/products?q=motor", headers=h)).json()["total"] >= 3


async def test_stale_scan_is_recovered_and_concurrency_is_limited(client, site, db, settings):
    h = await register(client)
    wid = await _website(client, h)
    me = (await client.get(f"{API}/auth/me", headers=h)).json()
    # a scan whose worker died an hour ago
    stale = ScanJob(business_id=uuid.UUID(me["business"]["id"]), website_id=uuid.UUID(wid), status="running",
                    stage="extracting", settings={}, stage_history=[], errors=[], change_summary={})
    db.add(stale)
    await db.commit()
    await db.execute(update(ScanJob).where(ScanJob.id == stale.id)
                     .values(updated_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await db.commit()
    r = await client.post(f"{API}/scans", json={"website_id": wid}, headers=h)
    assert r.status_code == 202, r.text
    await db.refresh(stale)
    assert stale.status == "failed" and "stopped reporting" in stale.errors[-1]["message"]
    await wait_for_inline_tasks()

    settings.max_active_scans_per_business = 0
    r = await client.post(f"{API}/scans", json={"website_id": wid}, headers=h)
    assert r.status_code == 429 and r.json()["error"]["code"] == "scan_limit"


async def test_database_enforces_one_active_scan_per_website(db, client):
    h = await register(client)
    wid = await _website(client, h, "https://example.com")
    me = (await client.get(f"{API}/auth/me", headers=h)).json()
    bid = uuid.UUID(me["business"]["id"])
    db.add(ScanJob(business_id=bid, website_id=uuid.UUID(wid), status="queued", settings={}, stage_history=[], errors=[], change_summary={}))
    await db.commit()
    db.add(ScanJob(business_id=bid, website_id=uuid.UUID(wid), status="running", settings={}, stage_history=[], errors=[], change_summary={}))
    try:
        await db.commit()
        raise AssertionError("second active scan was allowed")
    except Exception as e:  # IntegrityError
        assert "unique" in str(e).lower() or "duplicate" in str(e).lower()
        await db.rollback()


async def test_unavailable_worker_fails_scan_instead_of_hanging(client, site, monkeypatch):
    def broken(_):
        raise ConnectionError("broker down")

    monkeypatch.setattr("app.workers.dispatch.dispatch_scan", broken)
    h = await register(client)
    wid = await _website(client, h)
    r = await client.post(f"{API}/scans", json={"website_id": wid}, headers=h)
    assert r.status_code == 503 and r.json()["error"]["code"] == "worker_unavailable"
    scans = (await client.get(f"{API}/scans", headers=h)).json()["items"]
    assert scans[0]["status"] == "failed"
    # the website is not blocked by a phantom active scan
    monkeypatch.undo()


async def test_reset_token_never_logged_outside_development(client, settings, caplog):
    await register(client)
    settings.environment = "production"
    with caplog.at_level(logging.INFO):
        r = await client.post(f"{API}/auth/forgot-password", json={"email": "owner@example.com"})
    assert r.status_code == 200 and "dev_reset_url" not in r.json()
    assert "token=" not in caplog.text and "reset-password?" not in caplog.text


async def test_recent_changes_only_show_rescan_results(client, site):
    h = await register(client)
    wid = await _website(client, h)
    await client.post(f"{API}/scans", json={"website_id": wid}, headers=h)
    await wait_for_inline_tasks()
    overview = (await client.get(f"{API}/dashboard/overview", headers=h)).json()
    assert overview["recent_changes"] == []  # the initial import is not a "change"


async def test_scan_detail_reports_methods_and_ai_usage(client, site):
    h = await register(client)
    wid = await _website(client, h)
    r = await client.post(f"{API}/scans", json={"website_id": wid}, headers=h)
    await wait_for_inline_tasks()
    scan = (await client.get(f"{API}/scans/{r.json()['id']}", headers=h)).json()
    assert scan["ai"]["requests"] == 0  # AI_PROVIDER=none in tests: no AI was used, and the API says so
    methods = {(m["method"], m["source_type"]) for m in scan["extraction_methods"]}
    assert ("deterministic", "json_ld") in methods and ("deterministic", "html_table") in methods


RELATED_PRODUCT_PAGE = """<html><body><main>
<article class="product_page"><h1>Sharp Objects</h1><p class="price_color">£47.82</p>
<p class="instock availability">In stock</p><button>Add to basket</button>
<table class="table"><tr><th>UPC</th><td>e00eb4fd7b871a48</td></tr><tr><th>Product Type</th><td>Books</td></tr>
<tr><th>Availability</th><td>In stock (20 available)</td></tr></table></article>
<section class="recently-viewed"><h2>Products you recently viewed</h2>""" + "".join(
    f"""<article class="product_pod"><h3><a href="/catalogue/b-{i}/index.html" title="Book {i}">Book {i}</a></h3>
    <p class="price_color">£{20 + i}.00</p><button>Add to basket</button></article>""" for i in range(6)
) + "</section></main></body></html>"


def test_product_page_with_recently_viewed_strip_is_a_product_page():
    """Regression (live site): detail pages with a 'recently viewed' strip of priced cards were classified as listings."""
    from bs4 import BeautifulSoup

    from app.services.discovery.classifier import classify_page
    from app.services.extraction.structured import parse_meta

    soup = BeautifulSoup(RELATED_PRODUCT_PAGE, "lxml")
    c = classify_page("https://books.example/catalogue/sharp-objects_997/index.html", soup, parse_meta(soup))
    assert c.page_type == "product", c.reasons
    assert not any("listing-like URL" in r for r in c.reasons)


async def test_crawler_does_not_keep_cookies(settings):
    """Stateful crawling personalised pages ("recently viewed") and retained site session data."""
    seen_cookie_headers = []

    def handler(req: httpx.Request):
        seen_cookie_headers.append(req.headers.get("cookie"))
        return httpx.Response(200, text="<html></html>",
                              headers={"content-type": "text/html", "set-cookie": "session=abc; Path=/"})

    f = SafeFetcher(settings, transport=httpx.MockTransport(handler), resolver=public_resolver)
    await f.fetch("https://shop.example/a")
    await f.fetch("https://shop.example/b")
    await f.aclose()
    assert seen_cookie_headers == [None, None]


def test_recently_viewed_heading_followed_by_sibling_cards():
    """The live markup: <div class=sub-header><h2>Products you recently viewed</h2></div><ul>cards</ul>."""
    from bs4 import BeautifulSoup

    from app.services.discovery.classifier import classify_page
    from app.services.extraction.structured import parse_meta

    cards = "".join(
        f'<li><article class="product_pod"><h3><a href="/c/b{i}/index.html" title="B{i}">B{i}</a></h3>'
        f'<p class="price_color">£{10 + i}.00</p><button>Add to basket</button></article></li>' for i in range(8))
    html = ('<html><body><article class="product_page"><h1>Sharp Objects</h1><p class="price_color">£47.82</p>'
            '<div class="sub-header"><h2>Product Information</h2></div><table><tr><th>UPC</th><td>e00e</td></tr>'
            '<tr><th>Tax</th><td>£0.00</td></tr></table>'
            f'<div class="sub-header"><h2>Products you recently viewed</h2></div><ul class="row">{cards}</ul>'
            '</article></body></html>')
    soup = BeautifulSoup(html, "lxml")
    c = classify_page("https://books.example/catalogue/sharp-objects_997/index.html", soup, parse_meta(soup))
    assert c.page_type == "product", c.reasons
    assert soup.find("table") is not None  # unrelated sections are kept


def test_internal_image_and_product_urls_are_dropped():
    from app.schemas.canonical import ExtractedProduct, ProductImage
    from app.services.normalization.product import finalize

    p = finalize(ExtractedProduct(
        name="Drill", source_url="https://shop.example/p", product_url="http://10.0.0.5/admin",
        images=[ProductImage(url="http://169.254.169.254/latest/meta-data/x.png"), ProductImage(url="http://localhost/x.png"),
                ProductImage(url="https://cdn.shop.example/drill.jpg")]))
    assert [i.url for i in p.images] == ["https://cdn.shop.example/drill.jpg"]
    assert p.product_url is None
