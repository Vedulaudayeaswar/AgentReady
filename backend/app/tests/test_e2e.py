"""End-to-end merchant journey through the HTTP API with a fixture website."""

import csv
import io
import json

from app.tests.conftest import register
from app.workers.dispatch import wait_for_inline_tasks

API = "/api/v1"


async def connect_and_scan(client, headers, url="https://shop.example"):
    r = await client.post(f"{API}/websites", json={"url": url, "max_pages": 30, "max_depth": 2}, headers=headers)
    assert r.status_code == 201, r.text
    website = r.json()
    r = await client.post(f"{API}/scans", json={"website_id": website["id"]}, headers=headers)
    assert r.status_code == 202, r.text
    await wait_for_inline_tasks()
    scan = (await client.get(f"{API}/scans/{r.json()['id']}", headers=headers)).json()
    return website, scan


async def products_by_name(client, headers):
    r = await client.get(f"{API}/products?page_size=200", headers=headers)
    return {p["name"]: p for p in r.json()["items"]}


async def test_full_merchant_journey(client, site):
    h = await register(client)
    website, scan = await connect_and_scan(client, h)

    # ---- scan completed with real progress
    assert scan["status"] == "completed", scan["errors"]
    assert [s["stage"] for s in scan["stage_history"]] == [
        "connecting", "discovering_pages", "finding_products", "extracting", "normalizing", "validating", "generating_catalog"]
    assert all(s["finished_at"] for s in scan["stage_history"])
    progress = (await client.get(f"{API}/scans/{scan['id']}/progress", headers=h)).json()
    assert all(s["state"] == "done" for s in progress["stages"])

    # ---- robots.txt respected; non-HTML links not fetched
    assert "/private/secret-product" not in site.requested
    assert "/brochure.pdf" not in site.requested
    assert scan["pages_skipped_robots"] >= 1

    # ---- discovery: product & listing pages found, content pages not
    pages = (await client.get(f"{API}/scans/{scan['id']}/pages", headers=h)).json()["items"]
    types = {p["url"].replace("https://shop.example", ""): p["page_type"] for p in pages}
    assert types["/products/dc-motor-12v"] == "product"
    assert types["/price-list"] == "listing"
    assert types["/about"] == "other" and types["/blog/new-arrivals"] == "other" and types["/"] == "other"

    # ---- extraction results
    products = await products_by_name(client, h)
    assert {"12V DC Motor", "Steel Bolt M10", "Motor A", "Motor B", "Gear Pump GP-200", "Heavy Duty Motor",
            "Cable Ties (100 pcs)"} <= set(products)
    assert not any("Opening" in n or "Mon" in n for n in products)  # unrelated table ignored
    motor = products["12V DC Motor"]
    assert motor["price"] == {"amount": 450.0, "currency": "INR"} and motor["sku"] == "MTR001"
    assert motor["approval_status"] == "needs_review"
    # prompt-injection text on the page did not change the extracted price
    assert products["Cable Ties (100 pcs)"]["price"]["amount"] == 99.0

    # ---- quality classification
    assert products["Gear Pump GP-200"]["quality_status"] == "incomplete"  # no price
    assert products["Heavy Duty Motor"]["quality_status"] == "needs_review"  # conflicting prices
    dupes = [n for n, p in products.items() if p["quality_status"] == "duplicate_candidate"]
    assert dupes  # "Steel Bolt M10" appears on its own page (with SKU) and in the price list (without)

    # ---- product detail with evidence and issues
    detail = (await client.get(f"{API}/products/{motor['id']}", headers=h)).json()
    price_ev = [e for e in detail["evidence"] if e["field"] == "price"]
    assert price_ev and price_ev[0]["source_type"] == "json_ld" and price_ev[0]["source_url"].endswith("/dc-motor-12v")
    assert detail["source_page"]["url"].endswith("/products/dc-motor-12v")
    heavy = (await client.get(f"{API}/products/{products['Heavy Duty Motor']['id']}", headers=h)).json()
    assert any(i["code"] == "conflicting_values" and i["field"] == "price" for i in heavy["issues"])

    # ---- validation center
    issues = (await client.get(f"{API}/validation/issues?severity=warning", headers=h)).json()
    assert issues["total"] > 0
    codes = {i["code"] for i in issues["items"]}
    assert {"missing_price", "conflicting_values", "possible_duplicate"} <= codes

    # resolve the conflict by editing the price (merchant decision) -> issue auto-resolves
    r = await client.patch(f"{API}/products/{heavy['id']}", json={"price_amount": "1099", "currency": "inr"}, headers=h)
    assert r.status_code == 200 and r.json()["manual_overrides"] == ["currency", "price"]
    heavy = (await client.get(f"{API}/products/{heavy['id']}", headers=h)).json()
    assert not [i for i in heavy["issues"] if i["code"] == "conflicting_values" and i["status"] == "open"]
    assert any(e["extraction_method"] == "merchant" for e in heavy["evidence"])

    # dismiss a duplicate warning explicitly
    dup_issue = next(i for i in issues["items"] if i["code"] == "possible_duplicate")
    r = await client.post(f"{API}/validation/issues/{dup_issue['id']}/resolve", json={"action": "dismiss", "note": "different packs"}, headers=h)
    assert r.json()["status"] == "dismissed"

    # ---- approval
    for name in ("12V DC Motor", "Motor A", "Heavy Duty Motor"):
        r = await client.patch(f"{API}/products/{products[name]['id']}", json={"approval_status": "approved"}, headers=h)
        assert r.status_code == 200, r.text
    # cannot approve a product with a blocking error
    bad = products["Motor B"]
    # Phase 2 pricing rules reject a zero price up front instead of storing it
    r = await client.patch(f"{API}/products/{bad['id']}", json={"price_amount": "0"}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "pricing_rule"
    r = await client.patch(f"{API}/products/{bad['id']}", json={"gtin": "12345678"}, headers=h)  # bad check digit
    r = await client.patch(f"{API}/products/{bad['id']}", json={"approval_status": "approved"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "has_blocking_issues"

    # ---- dashboard is computed from real data
    dash = (await client.get(f"{API}/dashboard/overview", headers=h)).json()
    total = (await client.get(f"{API}/products", headers=h)).json()["total"]
    assert dash["total_products"] == total == len(products) + 1  # +1: the two "Steel Bolt M10" duplicate candidates
    assert dash["approval"]["approved"] == 3
    assert dash["latest_scan"]["id"] == scan["id"]

    # ---- exports (approved only by default)
    r = await client.post(f"{API}/exports", json={"format": "json"}, headers=h)
    assert r.status_code == 201 and r.json()["product_count"] == 3
    body = json.loads((await client.get(f"{API}/exports/{r.json()['id']}/download", headers=h)).text)
    assert {p["name"] for p in body["products"]} == {"12V DC Motor", "Motor A", "Heavy Duty Motor"}

    r = await client.post(f"{API}/exports", json={"format": "csv"}, headers=h)
    dl = await client.get(f"{API}/exports/{r.json()['id']}/download", headers=h)
    assert dl.headers["content-type"].startswith("text/csv") and "attachment" in dl.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(dl.text)))
    assert {r["name"] for r in rows} == {"12V DC Motor", "Motor A", "Heavy Duty Motor"}
    assert "attr:voltage" in rows[0]

    r = await client.post(f"{API}/exports", json={"format": "jsonld"}, headers=h)
    ld = json.loads((await client.get(f"{API}/exports/{r.json()['id']}/download", headers=h)).text)
    assert ld["@context"] == "https://schema.org" and ld["@type"] == "ItemList"
    item = next(i["item"] for i in ld["itemListElement"] if i["item"]["name"] == "12V DC Motor")
    assert item["offers"] == {"@type": "Offer", "price": "450.00", "priceCurrency": "INR",
                              "seller": {"@type": "Organization", "name": "Sharma Electricals"},
                              "availability": "https://schema.org/InStock", "url": "https://shop.example/products/dc-motor-12v"}
    assert item["gtin13"] == "4006381333931"

    r = await client.post(f"{API}/exports", json={"format": "feed", "include_unapproved": True}, headers=h)
    feed = json.loads((await client.get(f"{API}/exports/{r.json()['id']}/download", headers=h)).text)
    assert feed["feed_version"] == "agentready-feed/1.0" and feed["item_count"] >= 7
    assert len((await client.get(f"{API}/exports", headers=h)).json()["items"]) == 4

    # ---- published catalog API: approved only, via catalog key
    key = (await client.post(f"{API}/business/catalog-key", headers=h)).json()["catalog_api_key"]
    cat = (await client.get(f"{API}/catalog", headers={"X-Catalog-Key": key})).json()
    assert cat["total"] == 3
    assert (await client.get(f"{API}/catalog/{products['Gear Pump GP-200']['id']}", headers={"X-Catalog-Key": key})).status_code == 404
    assert (await client.get(f"{API}/catalog", headers={"X-Catalog-Key": "wrong"})).status_code == 401

    # ---- rescan: price change, description change, removal, new product
    await client.patch(f"{API}/products/{motor['id']}", json={"description": "Merchant-written description"}, headers=h)
    site.overrides["/products/dc-motor-12v"] = site.body("/products/dc-motor-12v").replace('"price":"450"', '"price":"475"').replace("Rs. 450", "Rs. 475")
    site.overrides["/products/gear-pump"] = 404
    site.overrides["/price-list"] = site.body("/price-list").replace(
        "<tr><td>Motor B</td>", "<tr><td>Motor C</td><td>36V</td><td>6000</td><td>990</td><td>DC Motors</td></tr><tr><td>Motor B</td>")
    r = await client.post(f"{API}/scans", json={"website_id": website["id"]}, headers=h)
    await wait_for_inline_tasks()
    rescan = (await client.get(f"{API}/scans/{r.json()['id']}", headers=h)).json()
    assert rescan["is_rescan"] and rescan["status"] == "awaiting_review", rescan["errors"]
    summary = rescan["change_summary"]
    assert summary["new"] >= 1 and summary["removed"] == 1 and summary["price_changes"] >= 1

    # catalog not modified before review
    assert (await client.get(f"{API}/products/{motor['id']}", headers=h)).json()["price"]["amount"] == 450.0

    changes = (await client.get(f"{API}/scans/{rescan['id']}/changes", headers=h)).json()["items"]
    motor_change = next(c for c in changes if c["product_id"] == motor["id"])
    price_change = next(c for c in motor_change["changes"] if c["field"] == "price_amount")
    assert (price_change["old"], price_change["new"]) == ("450.00", "475.00")
    # the merchant edited the description but the WEBSITE did not change it: nothing to review (Phase 2 semantics;
    # website changes to merchant-managed fields become sync conflicts, see test_commerce.py)
    assert not [c for c in motor_change["changes"] if c["field"] == "description"]
    assert any(c["change_type"] == "new" and c["name"] == "Motor C" for c in changes)
    assert any(c["change_type"] == "removed" for c in changes)

    r = await client.post(f"{API}/scans/{rescan['id']}/apply", json={}, headers=h)
    assert r.status_code == 200 and r.json()["applied"]["kept_merchant_edits"] >= 1
    motor_after = (await client.get(f"{API}/products/{motor['id']}", headers=h)).json()
    assert motor_after["price"]["amount"] == 475.0
    assert motor_after["description"] == "Merchant-written description"  # manual correction preserved
    assert motor_after["approval_status"] == "needs_review"  # facts changed -> re-approval required
    after = await products_by_name(client, h)
    assert "Motor C" in after and "Gear Pump GP-200" not in after


async def test_merchant_data_isolation(client, site):
    a = await register(client, "a@example.com", "Shop A")
    b = await register(client, "b@example.com", "Shop B")
    website, scan = await connect_and_scan(client, a)
    product = (await client.get(f"{API}/products", headers=a)).json()["items"][0]
    exp = (await client.post(f"{API}/exports", json={"format": "json", "include_unapproved": True}, headers=a)).json()
    issue = (await client.get(f"{API}/validation/issues", headers=a)).json()["items"][0]

    assert (await client.get(f"{API}/products", headers=b)).json()["total"] == 0
    assert (await client.get(f"{API}/websites", headers=b)).json()["items"] == []
    assert (await client.get(f"{API}/scans", headers=b)).json()["total"] == 0
    assert (await client.get(f"{API}/validation/issues", headers=b)).json()["total"] == 0
    for path in (f"/products/{product['id']}", f"/scans/{scan['id']}", f"/scans/{scan['id']}/changes",
                 f"/websites/{website['id']}", f"/exports/{exp['id']}/download"):
        assert (await client.get(API + path, headers=b)).status_code == 404, path
    assert (await client.patch(f"{API}/products/{product['id']}", json={"name": "hacked"}, headers=b)).status_code == 404
    assert (await client.delete(f"{API}/products/{product['id']}", headers=b)).status_code == 404
    assert (await client.post(f"{API}/validation/issues/{issue['id']}/resolve", json={}, headers=b)).status_code == 404
    assert (await client.post(f"{API}/scans", json={"website_id": website["id"]}, headers=b)).status_code == 404
    assert (await client.get(f"{API}/dashboard/overview", headers=b)).json()["total_products"] == 0
    # B's catalog key cannot read A's products
    key_b = (await client.post(f"{API}/business/catalog-key", headers=b)).json()["catalog_api_key"]
    await client.patch(f"{API}/products/{product['id']}", json={"approval_status": "approved"}, headers=a)
    assert (await client.get(f"{API}/catalog/{product['id']}", headers={"X-Catalog-Key": key_b})).status_code == 404


async def test_ssrf_urls_rejected_at_api(client, site):
    h = await register(client)
    for url in ("http://127.0.0.1", "http://localhost:8000", "http://169.254.169.254", "ftp://shop.example", "http://10.0.0.1"):
        r = await client.post(f"{API}/websites", json={"url": url}, headers=h)
        assert r.status_code == 422, url


async def test_cancel_and_duplicate_scan_guard(client, site):
    h = await register(client)
    w = (await client.post(f"{API}/websites", json={"url": "https://shop.example"}, headers=h)).json()
    r1 = await client.post(f"{API}/scans", json={"website_id": w["id"]}, headers=h)
    r2 = await client.post(f"{API}/scans", json={"website_id": w["id"]}, headers=h)
    assert r2.status_code == 409
    await client.post(f"{API}/scans/{r1.json()['id']}/cancel", headers=h)
    await wait_for_inline_tasks()
    s = (await client.get(f"{API}/scans/{r1.json()['id']}", headers=h)).json()
    assert s["status"] in ("cancelled", "completed")


async def test_mock_ai_is_labelled(client, site, settings):
    """With AI_PROVIDER=mock, AI evidence is clearly marked as mock output."""
    settings.ai_provider = "mock"
    h = await register(client)
    _, scan = await connect_and_scan(client, h)
    assert scan["status"] == "completed"
    products = await products_by_name(client, h)
    relay = products.get("Relay R1")  # only reachable through the AI path (headerless table)
    assert relay is not None
    detail = (await client.get(f"{API}/products/{relay['id']}", headers=h)).json()
    assert detail["evidence"] and all(e["extraction_method"] == "ai_mock" and e["model_id"] == "mock-local"
                                      for e in detail["evidence"])
    ai = (await client.get(f"{API}/settings/ai", headers=h)).json()
    assert ai["is_mock"] is True and ai["usage"][0]["model"] == "mock-local"


async def test_unchanged_rescan_clears_stale_extraction_warnings(client, site):
    # A product's extraction warnings were computed by an older extractor; a rescan where nothing visible
    # changed must still replace them (otherwise fixed false warnings stay open forever).
    from sqlalchemy import select

    from app.db.session import session_factory
    from app.models import Product, ValidationIssue

    h = await register(client)
    website, scan = await connect_and_scan(client, h)
    assert scan["status"] == "completed", scan["errors"]
    stale = {"code": "conflicting_values", "field": "name", "message": "microdata and html disagree on name"}
    async with session_factory()() as s:
        for p in (await s.execute(select(Product))).scalars():
            p.extra = {**(p.extra or {}), "flags": [stale]}
        await s.commit()

    r = await client.post(f"{API}/scans", json={"website_id": website["id"]}, headers=h)
    await wait_for_inline_tasks()
    rescan = (await client.get(f"{API}/scans/{r.json()['id']}", headers=h)).json()
    assert rescan["status"] == "completed", rescan  # nothing changed: applied automatically
    async with session_factory()() as s:
        flags = [f for p in (await s.execute(select(Product))).scalars() for f in (p.extra or {}).get("flags", [])]
        open_stale = (await s.execute(select(ValidationIssue).where(
            ValidationIssue.code == "conflicting_values", ValidationIssue.status == "open",
            ValidationIssue.message == stale["message"]))).scalars().all()
    assert stale not in flags and open_stale == []
