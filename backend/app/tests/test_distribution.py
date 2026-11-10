"""Phase 3: agent-ready distribution (public serializer, agent API, search, compare, feeds, tokens, webhooks, MCP,
checkout foundation, analytics, merchant isolation). External platforms are never contacted; webhook destinations
are served by an in-memory transport (labelled SIMULATED where relevant)."""

import csv
import io
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from app.models import FeedConfiguration, FeedSnapshot, OutboxEvent, ScheduledPriceChange, WebhookDelivery
from app.services.distribution import cache, feeds
from app.tests.conftest import register

API = "/api/v1"
V3 = "/api/v3/agents"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


async def make(client, h, approve=True, **kw):
    body = {"name": "Cotton T-shirt", "regular_price": "499.00", "currency": "INR", "lifecycle_status": "active", **kw}
    r = await client.post(f"{API}/products", json=body, headers=h)
    assert r.status_code == 201, r.text
    p = r.json()
    if approve:
        r = await client.patch(f"{API}/products/{p['id']}", json={"approval_status": "approved"}, headers=h)
        assert r.status_code == 200, r.text
        p = r.json()
    return p


async def enable(client, h, **extra):
    body = {"enabled": True, "public_name": "Sharma Electricals", "public_url": "https://sharma.example", **extra}
    r = await client.put(f"{API}/distribution/settings", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def agent_key(client, h, scopes=("agent:read",), rate=120):
    r = await client.post(f"{API}/api-keys", json={"name": "Shopping agent", "scopes": list(scopes),
                                                   "rate_limit_per_minute": rate}, headers=h)
    assert r.status_code == 201, r.text
    return {"X-Catalog-Key": r.json()["key"]}


async def seed(client, h):
    laptop = await make(client, h, name="UltraBook 14 Laptop", brand="Zenon", sku="LAP-14", gtin="4006381333931",
                        regular_price="65000", description="Light laptop", images=[{"url": "https://cdn.example/l.jpg"}],
                        product_url="https://sharma.example/p/ultrabook-14",
                        attributes=[{"label": "Storage", "value": "512 GB"}, {"label": "Weight", "value": "1.2 kg"}],
                        cost_price="51000", track_inventory=True, stock_quantity=7)
    laptop2 = await make(client, h, name="ProBook 16 Laptop", brand="Zenon", sku="LAP-16", regular_price="75000",
                         availability="InStock",
                         attributes=[{"label": "Storage", "value": "1 TB"}, {"label": "Weight", "value": "2100 g"}])
    motor = await make(client, h, name="Industrial Motor 2HP", brand="Havells", sku="MOT-230", regular_price="12000",
                       attributes=[{"label": "Voltage", "value": "230 V"}, {"label": "Power", "value": "1.5 kW"}],
                       availability="InStock")
    motor12 = await make(client, h, name="DC Motor 12V", brand="Bosch", sku="MOT-12", regular_price="3000",
                         attributes=[{"label": "Voltage", "value": "12 V"}, {"label": "Power", "value": "100 W"}],
                         track_inventory=True, stock_quantity=0)
    tee = await make(client, h, name="Basic T-shirt", brand="Acme", sku="TEE", regular_price="450", availability="InStock")
    for size in ("M", "L"):
        for colour in ("Black", "White"):
            r = await client.post(f"{API}/products/{tee['id']}/variants", json={"options": {"Size": size, "Color": colour},
                                                                                "sku": f"TEE-{size}-{colour[0]}"}, headers=h)
            assert r.status_code == 201, r.text
    draft = await make(client, h, approve=False, name="Secret prototype laptop", sku="SECRET")
    return {"laptop": laptop, "laptop2": laptop2, "motor": motor, "motor12": motor12, "tee": tee, "draft": draft}


# ---------------------------------------------------------------- public data layer
async def test_distribution_off_by_default_and_public_serializer(client):
    h = await register(client)
    s = await seed(client, h)
    k = await agent_key(client, h)
    r = await client.get(f"{V3}/products", headers=k)
    assert r.status_code == 403 and r.json()["error"]["code"] == "distribution_disabled"
    await enable(client, h)
    r = await client.get(f"{V3}/products/{s['laptop']['id']}", headers=k)
    assert r.status_code == 200, r.text
    p = r.json()["product"]
    text = r.text
    assert "51000" not in text and "cost" not in text and "stock_on_hand" not in text and "reserved" not in text
    assert "evidence" not in text and "approval" not in text and "manual_overrides" not in text
    assert p["price"]["current"] == "65000.00" and p["price"]["currency"] == "INR"
    assert p["availability"]["status"] == "available" and p["availability"]["source"] == "inventory"
    assert "available_quantity" not in text  # quantities never leave AgentReady through the agent API
    assert p["merchant"]["name"] == "Sharma Electricals" and p["merchant"]["id"] == "sharma-electricals"
    assert {a["key"] for a in p["attributes"]} == {"storage", "weight"}
    storage = next(a for a in p["attributes"] if a["key"] == "storage")
    assert storage["normalized"] == {"value": 512.0, "unit": "GB", "dimension": "data"}
    assert "mpn" in p["unknown_fields"] and "brand" not in p["unknown_fields"] and p["withheld_fields"] == []
    # unapproved products do not exist for agents (same 404 as a missing id)
    r = await client.get(f"{V3}/products/{s['draft']['id']}", headers=k)
    assert r.status_code == 404
    assert (await client.get(f"{V3}/products/not-a-uuid", headers=k)).status_code == 404
    # out-of-stock is a status, never a quantity; unknown availability differs from out of stock
    oos = (await client.get(f"{V3}/products/{s['motor12']['id']}/availability", headers=k)).json()
    assert oos["availability"]["status"] == "out_of_stock"
    unknown = await make(client, h, name="Mystery Item", sku="MYST", regular_price="10")
    assert (await client.get(f"{V3}/products/{unknown['id']}", headers=k)).json()["product"]["availability"]["status"] == "unknown"
    # a public description override never replaces the internal description or evidence
    r = await client.patch(f"{API}/distribution/products/{s['laptop']['id']}",
                           json={"public_description": "Our lightest laptop, made for travel."}, headers=h)
    assert r.status_code == 200, r.text
    pub = (await client.get(f"{V3}/products/{s['laptop']['id']}", headers=k)).json()["product"]
    assert pub["description"] == "Our lightest laptop, made for travel."
    internal = (await client.get(f"{API}/products/{s['laptop']['id']}", headers=h)).json()
    assert internal["description"] == "Light laptop"
    assert any(e["field"] == "description" and e["value"] == "Light laptop" for e in internal["evidence"])
    # the preview IS the public serializer output
    prev = (await client.get(f"{API}/distribution/products/{s['laptop']['id']}/preview", headers=h)).json()
    agent_view = (await client.get(f"{V3}/products/{s['laptop']['id']}", headers=k)).json()["product"]
    strip = lambda d: {k_: v for k_, v in d.items() if k_ not in ("availability",)}  # noqa: E731
    assert strip(prev["representation"]) == strip(agent_view) and prev["visible_to_agents"] is True


async def test_distribution_controls(client):
    h = await register(client)
    s = await seed(client, h)
    k = await agent_key(client, h)
    await enable(client, h)
    ids = lambda r: {i["product"]["id"] for i in r.json()["items"]}  # noqa: E731
    everything = ids(await client.get(f"{V3}/products", headers=k))
    assert s["laptop"]["id"] in everything and s["draft"]["id"] not in everything and len(everything) == 5
    # product-level opt-out
    await client.patch(f"{API}/distribution/products/{s['motor']['id']}", json={"distribution_enabled": False}, headers=h)
    assert s["motor"]["id"] not in ids(await client.get(f"{V3}/products", headers=k))
    # channel restriction: only in feeds, not the agent API
    await client.patch(f"{API}/distribution/products/{s['laptop2']['id']}", json={"distribution_channels": ["feeds"]}, headers=h)
    assert s["laptop2"]["id"] not in ids(await client.get(f"{V3}/products", headers=k))
    # selected-only scope
    await client.put(f"{API}/distribution/settings", json={"product_scope": "selected"}, headers=h)
    assert ids(await client.get(f"{V3}/products", headers=k)) == set()
    await client.post(f"{API}/distribution/products/bulk", json={"product_ids": [s["tee"]["id"], s["laptop"]["id"]],
                                                                 "distribution_enabled": True}, headers=h)
    assert ids(await client.get(f"{V3}/products", headers=k)) == {s["tee"]["id"], s["laptop"]["id"]}
    await client.put(f"{API}/distribution/settings", json={"product_scope": "all_published"}, headers=h)
    # category exclusion (with subcategories)
    cat = (await client.post(f"{API}/categories", json={"name": "Computers"}, headers=h)).json()
    sub = (await client.post(f"{API}/categories", json={"name": "Laptops", "parent_id": cat["id"]}, headers=h)).json()
    await client.patch(f"{API}/products/{s['laptop']['id']}", json={"category_id": sub["id"]}, headers=h)
    assert (await client.put(f"{API}/distribution/categories/{cat['id']}", json={"distributed": False}, headers=h)).status_code == 200
    assert s["laptop"]["id"] not in ids(await client.get(f"{V3}/products", headers=k))
    lst = (await client.get(f"{API}/distribution/products?q=UltraBook", headers=h)).json()
    assert "Its category is excluded from distribution" in lst["items"][0]["status"]["reasons"]
    await client.put(f"{API}/distribution/categories/{cat['id']}", json={"distributed": True}, headers=h)
    # prices/availability withheld by the merchant are not leaked through filters either
    await client.put(f"{API}/distribution/settings", json={"show_prices": False, "show_availability": False}, headers=h)
    p = (await client.get(f"{V3}/products/{s['laptop']['id']}", headers=k)).json()["product"]
    assert p["price"] is None and p["availability"] is None and set(p["withheld_fields"]) == {"price", "availability"}
    assert (await client.get(f"{V3}/search?max_price=100000&currency=INR", headers=k)).json()["total"] == 0
    assert (await client.get(f"{V3}/search?availability=available", headers=k)).json()["total"] == 0
    await client.put(f"{API}/distribution/settings", json={"show_prices": True, "show_availability": True,
                                                           "hidden_attribute_keys": ["voltage"]}, headers=h)
    # hidden attributes are neither shown nor searchable
    m = (await client.get(f"{V3}/products/{s['motor12']['id']}", headers=k)).json()["product"]
    assert "voltage" not in {a["key"] for a in m["attributes"]}
    assert (await client.get(f"{V3}/search", params={"attr": "voltage:12 V"}, headers=k)).json()["total"] == 0
    # channel switch and expiry
    await client.put(f"{API}/distribution/settings", json={"agent_api_enabled": False}, headers=h)
    assert (await client.get(f"{V3}/products", headers=k)).status_code == 403
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    await client.put(f"{API}/distribution/settings", json={"agent_api_enabled": True, "expires_at": past}, headers=h)
    r = await client.get(f"{V3}/products", headers=k)
    assert r.status_code == 403 and "expired" in r.json()["error"]["message"]


# ---------------------------------------------------------------- search & ranking
async def test_agent_search_filters_and_ranking(client):
    h = await register(client)
    s = await seed(client, h)
    k = await agent_key(client, h)
    await enable(client, h)
    q = lambda **p: client.get(f"{V3}/search", params=p, headers=k)  # noqa: E731
    names = lambda r: [i["product"]["name"] for i in r.json()["items"]]  # noqa: E731
    # "laptops under ₹70,000" (plural stemmed, price in one currency)
    r = await q(q="laptops", max_price="70000", currency="INR")
    assert r.status_code == 200, r.text
    assert names(r) == ["UltraBook 14 Laptop"]
    # attribute filters with explicit unit conversion
    assert names(await q(attr="voltage:230 V")) == ["Industrial Motor 2HP"]
    assert names(await q(attr="voltage>=100 V")) == ["Industrial Motor 2HP"]
    assert names(await q(attr="voltage:0.23 kV")) == ["Industrial Motor 2HP"]  # 0.23 kV == 230 V
    assert names(await q(attr="power>=1 kW")) == ["Industrial Motor 2HP"]
    assert set(names(await q(attr="storage>=512 GB"))) == {"UltraBook 14 Laptop", "ProBook 16 Laptop"}
    assert names(await q(attr="storage>512 GB")) == ["ProBook 16 Laptop"]  # 1 TB = 1000 GB
    assert names(await q(attr="voltage>=100 W")) == []  # incompatible units are never compared
    # variant options: black T-shirts in medium; "medium" means size M
    r = await q(q="t-shirt", variant=["color:black", "size:medium"])
    assert names(r) == ["Basic T-shirt"] and len(r.json()["items"][0]["matched_variant_ids"]) == 1
    # availability, brand, exact identifiers
    assert "DC Motor 12V" not in names(await q(q="motor", availability="available"))
    assert set(names(await q(brand="zenon"))) == {"UltraBook 14 Laptop", "ProBook 16 Laptop"}
    assert names(await q(gtin="4006381333931")) == ["UltraBook 14 Laptop"]
    r = await q(q="MOT-12")
    assert names(r)[0] == "DC Motor 12V" and r.json()["items"][0]["ranking"]["signals"]["exact_identifier"] == 1.0
    # transparent ranking: name match outranks a description-only match; signals and policy published
    r = await q(q="laptop")
    body = r.json()
    assert body["ranking_policy"]["paid_placement"] is False and body["items"][0]["ranking"]["signals"]["text"] > 0
    assert all(i["ranking"]["score"] >= j["ranking"]["score"] for i, j in zip(body["items"], body["items"][1:]))
    # never the unapproved product, even when it matches best
    assert "Secret prototype laptop" not in names(r)
    # price sorting
    r = await q(sort="price_asc", currency="INR")
    prices = [float(i["product"]["price"]["current"]) for i in r.json()["items"]]
    assert prices == sorted(prices)
    # validation
    assert (await q(availability="sold")).status_code == 422
    assert (await q(attr="no-operator")).status_code == 422
    # typo tolerance in ranking (difflib on SQLite, pg_trgm on PostgreSQL)
    r = await q(q="laptp ultrabook")
    assert "UltraBook 14 Laptop" in names(r)


async def test_multi_currency_price_filters_require_currency(client):
    h = await register(client)
    await make(client, h, name="Widget A", sku="WA", regular_price="10", currency="USD")
    await make(client, h, name="Widget B", sku="WB", regular_price="500", currency="INR")
    k = await agent_key(client, h)
    await enable(client, h)
    r = await client.get(f"{V3}/search", params={"q": "widget", "max_price": "100"}, headers=k)
    assert r.status_code == 422 and "never converted" in r.json()["error"]["message"]
    r = await client.get(f"{V3}/search", params={"q": "widget", "max_price": "100", "currency": "USD"}, headers=k)
    assert [i["product"]["name"] for i in r.json()["items"]] == ["Widget A"]


async def test_compare_products(client):
    h = await register(client)
    s = await seed(client, h)
    k = await agent_key(client, h)
    await enable(client, h)
    r = await client.post(f"{V3}/compare", json={"product_ids": [s["laptop"]["id"], s["laptop2"]["id"]]}, headers=k)
    assert r.status_code == 200, r.text
    rows = {row["field"]: row for row in r.json()["comparison"]["rows"]}
    assert rows["price"]["comparable"] and rows["price"]["lowest"] == [s["laptop"]["id"]]
    st = rows["attribute:storage"]
    assert st["comparable"] and st["unit"] == "GB" and st["highest"] == [s["laptop2"]["id"]]
    wt = rows["attribute:weight"]
    assert wt["comparable"] and wt["normalized"][s["laptop"]["id"]] == "1200 g" and wt["lowest"] == [s["laptop"]["id"]]
    # missing attributes are unknown, incompatible units are not compared
    r = await client.post(f"{V3}/compare", json={"product_ids": [s["motor"]["id"], s["laptop"]["id"]]}, headers=k)
    rows = {row["field"]: row for row in r.json()["comparison"]["rows"]}
    assert rows["attribute:voltage"]["values"][s["laptop"]["id"]] == "unknown"
    other = await make(client, h, name="Odd motor", sku="ODD", attributes=[{"label": "Voltage", "value": "750 W"}])
    r = await client.post(f"{V3}/compare", json={"product_ids": [s["motor"]["id"], other["id"]]}, headers=k)
    rows = {row["field"]: row for row in r.json()["comparison"]["rows"]}
    assert rows["attribute:voltage"]["comparable"] is False and "Incompatible units" in rows["attribute:voltage"]["reason"]
    # validation and privacy: unapproved ids look exactly like missing ones
    r = await client.post(f"{V3}/compare", json={"product_ids": [s["laptop"]["id"], s["draft"]["id"]]}, headers=k)
    assert r.status_code == 404 and r.json()["error"]["details"]["missing"] == [s["draft"]["id"]]
    assert (await client.post(f"{V3}/compare", json={"product_ids": [s["laptop"]["id"]]}, headers=k)).status_code == 422


async def test_merchant_isolation_and_public_network(client):
    ha = await register(client, "a@example.com", "Alpha Store")
    hb = await register(client, "b@example.com", "Beta Store")
    pa = await make(client, ha, name="Alpha Drill", sku="AD", regular_price="100", currency="USD")
    pb = await make(client, hb, name="Beta Drill", sku="BD", regular_price="90", currency="USD")
    ka = await agent_key(client, ha)
    await enable(client, ha, public_name="Alpha")
    await enable(client, hb, public_name="Beta")
    # a merchant key only sees that merchant
    assert (await client.get(f"{V3}/products/{pb['id']}", headers=ka)).status_code == 404
    assert {i["product"]["name"] for i in (await client.get(f"{V3}/search?q=drill", headers=ka)).json()["items"]} == {"Alpha Drill"}
    # anonymous agents see only merchants that joined the public network
    assert (await client.get(f"{V3}/search?q=drill")).json()["total"] == 0
    await client.put(f"{API}/distribution/settings", json={"public_network": True}, headers=ha)
    await client.put(f"{API}/distribution/settings", json={"public_network": True}, headers=hb)
    r = await client.get(f"{V3}/search?q=drill")
    assert {i["product"]["merchant"]["name"] for i in r.json()["items"]} == {"Alpha", "Beta"}
    ms = (await client.get(f"{V3}/merchants")).json()["items"]
    assert {m["name"] for m in ms} == {"Alpha", "Beta"} and all(m["product_count"] == 1 for m in ms)
    # cross-merchant comparison (same currency -> comparable)
    r = await client.get(f"{V3}/compare?ids={pa['id']},{pb['id']}")
    rows = {row["field"]: row for row in r.json()["comparison"]["rows"]}
    assert rows["price"]["lowest"] == [pb["id"]] and rows["merchant"]["values"] == {pa["id"]: "Alpha", pb["id"]: "Beta"}
    # merchant filter and analytics attribution
    r = await client.get(f"{V3}/search", params={"merchant": "beta-store", "q": "drill"})
    assert [i["product"]["name"] for i in r.json()["items"]] == ["Beta Drill"]
    # B's management API never shows A's data
    lst = (await client.get(f"{API}/distribution/products", headers=hb)).json()
    assert [p["name"] for p in lst["items"]] == ["Beta Drill"]
    assert (await client.get(f"{API}/distribution/products/{pa['id']}/preview", headers=hb)).status_code == 404
    feed_a = (await client.post(f"{API}/distribution/feeds", json={"name": "A"}, headers=ha)).json()
    assert (await client.post(f"{API}/distribution/feeds/{feed_a['id']}/generate", headers=hb)).status_code == 404


# ---------------------------------------------------------------- freshness & caching
async def test_price_and_inventory_freshness_and_cache_invalidation(client, db):
    h = await register(client)
    s = await seed(client, h)
    k = await agent_key(client, h)
    await enable(client, h)
    pid = s["laptop"]["id"]
    url = f"{V3}/products/{pid}/pricing"
    assert (await client.get(url, headers=k)).json()["price"]["current"] == "65000.00"
    assert (await client.get(f"{V3}/search?q=ultrabook", headers=k)).json()["cached"] is False
    assert (await client.get(f"{V3}/search?q=ultrabook", headers=k)).json()["cached"] is True
    # a price change invalidates cached responses immediately
    await client.put(f"{API}/products/{pid}/pricing", json={"regular_price": "64000"}, headers=h)
    assert (await client.get(url, headers=k)).json()["price"]["current"] == "64000.00"
    r = (await client.get(f"{V3}/search?q=ultrabook", headers=k)).json()
    assert r["cached"] is False and r["items"][0]["product"]["price"]["current"] == "64000.00"
    # a scheduled change that is due is served even before the scheduler tick applied it
    r = await client.post(f"{API}/pricing/schedules", json={"product_id": pid, "new_amount": "60000",
                                                            "effective_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}, headers=h)
    sid = r.json()["id"]
    await db.execute(update(ScheduledPriceChange).where(ScheduledPriceChange.id == __import__("uuid").UUID(sid))
                     .values(effective_at=datetime.now(timezone.utc) - timedelta(seconds=5)))
    await db.commit()
    assert (await client.get(url, headers=k)).json()["price"]["current"] == "60000.00"
    # inventory changes (core UPDATE path) invalidate too
    adj = await client.post(f"{API}/inventory/adjust", json={"product_id": pid, "operation": "mark_out_of_stock"}, headers=h)
    assert adj.status_code == 200, adj.text
    av = (await client.get(f"{V3}/products/{pid}/availability", headers=k)).json()
    assert av["availability"]["status"] == "out_of_stock"
    # unpublishing removes it at once
    await client.patch(f"{API}/products/{pid}", json={"publication_status": "unpublished"}, headers=h)
    assert (await client.get(f"{V3}/products/{pid}", headers=k)).status_code == 404
    # sale windows: the response says until when the price is valid, and caching never crosses that moment
    start = datetime.now(timezone.utc) + timedelta(days=2)
    await client.put(f"{API}/products/{s['motor']['id']}/pricing", json={"sale_price": "9999", "sale_starts_at": start.isoformat()}, headers=h)
    pr = (await client.get(f"{V3}/products/{s['motor']['id']}/pricing", headers=k)).json()["price"]
    assert pr["current"] == "12000.00" and pr["upcoming_sale"]["price"] == "9999.00" and pr["valid_until"] is not None


# ---------------------------------------------------------------- feeds
async def _generate(client, h, feed_id):
    r = await client.post(f"{API}/distribution/feeds/{feed_id}/generate", headers=h)
    assert r.status_code == 202, r.text
    await feeds.wait_for_inline()
    snap = (await client.get(f"{API}/distribution/feed-snapshots/{r.json()['id']}", headers=h)).json()
    assert snap["status"] == "completed", snap
    body = (await client.get(f"{API}/distribution/feed-snapshots/{snap['id']}/download", headers=h)).text
    return snap, body


async def test_feed_formats_contain_only_distributed_public_data(client):
    h = await register(client)
    s = await seed(client, h)
    await enable(client, h)
    mk = lambda **b: client.post(f"{API}/distribution/feeds", json={"name": "Feed", **b}, headers=h)  # noqa: E731
    out = {}
    for fmt in ("json", "jsonl", "csv", "jsonld", "xml"):
        f = (await mk(format=fmt)).json()
        snap, body = await _generate(client, h, f["id"])
        assert snap["product_count"] == 5 and snap["total"] == 5 and snap["processed"] == 5
        assert "Secret prototype" not in body and "51000" not in body and "cost" not in body.lower()
        out[fmt] = body
    doc = json.loads(out["json"])
    assert doc["schema"] == "agentready-feed/1.0" and doc["item_count"] == 5
    assert len([json.loads(x) for x in out["jsonl"].splitlines()]) == 5
    rows = list(csv.DictReader(io.StringIO(out["csv"])))
    assert {r["sku"] for r in rows} == {"LAP-14", "LAP-16", "MOT-230", "MOT-12", "TEE"}
    ld = json.loads(out["jsonld"])
    assert ld["@type"] == "ItemList" and ld["numberOfItems"] == 5
    lap = next(i["item"] for i in ld["itemListElement"] if i["item"]["sku"] == "LAP-14")
    assert lap["offers"]["price"] == "65000.00" and lap["offers"]["availability"] == "https://schema.org/InStock"
    assert lap["gtin13"] == "4006381333931"
    root = ET.fromstring(out["xml"])
    assert root.tag == "feed" and len(root.findall("record")) == 5
    # variant, availability and pricing feeds
    snap, body = await _generate(client, h, (await mk(format="json", feed_type="variant")).json()["id"])
    assert {i["sku"] for i in json.loads(body)["items"]} == {"TEE-M-B", "TEE-M-W", "TEE-L-B", "TEE-L-W"}
    snap, body = await _generate(client, h, (await mk(format="csv", feed_type="availability")).json()["id"])
    rows = {r["sku"]: r["availability"] for r in csv.DictReader(io.StringIO(body))}
    assert rows["MOT-12"] == "out_of_stock" and rows["LAP-14"] == "available"
    snap, body = await _generate(client, h, (await mk(format="jsonl", feed_type="pricing")).json()["id"])
    assert {json.loads(x)["price"]["currency"] for x in body.splitlines()} == {"INR"}
    # configuration validation
    assert (await mk(format="csv", feed_type="incremental")).status_code == 422
    assert (await mk(profile="openai", format="xml")).status_code == 422
    assert (await mk(feed_type="category")).status_code == 422
    # feeds channel off -> generation fails with the reason (no data leaves)
    await client.put(f"{API}/distribution/settings", json={"feeds_enabled": False}, headers=h)
    f = (await mk(format="json")).json()
    r = await client.post(f"{API}/distribution/feeds/{f['id']}/generate", headers=h)
    await feeds.wait_for_inline()
    snap = (await client.get(f"{API}/distribution/feed-snapshots/{r.json()['id']}", headers=h)).json()
    assert snap["status"] == "failed" and "disabled" in snap["error"]


async def test_incremental_feed_and_tombstones(client):
    h = await register(client)
    s = await seed(client, h)
    await enable(client, h)
    f = (await client.post(f"{API}/distribution/feeds", json={"name": "Delta", "feed_type": "incremental", "format": "json"},
                           headers=h)).json()
    snap1, body1 = await _generate(client, h, f["id"])
    assert snap1["product_count"] == 5 and json.loads(body1)["removed"] == []
    snap2, body2 = await _generate(client, h, f["id"])  # nothing changed -> empty delta, no duplicates
    assert snap2["product_count"] == 0 and snap2["removed_count"] == 0 and snap2["version"] == 2
    await client.put(f"{API}/products/{s['motor']['id']}/pricing", json={"regular_price": "11500"}, headers=h)
    await client.post(f"{API}/products/{s['laptop2']['id']}/archive", headers=h)
    await client.patch(f"{API}/products/{s['tee']['id']}", json={"publication_status": "unpublished"}, headers=h)
    snap3, body3 = await _generate(client, h, f["id"])
    doc = json.loads(body3)
    assert [i["identifiers"]["sku"] for i in doc["items"]] == ["MOT-230"]
    assert doc["items"][0]["price"]["current"] == "11500.00"
    assert {(t["id"], t["reason"]) for t in doc["removed"]} == {(s["laptop2"]["id"], "archived"), (s["tee"]["id"], "unpublished")}
    # live incremental endpoint for agents
    k = await agent_key(client, h)
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    ch = (await client.get(f"{V3}/changes", params={"since": since}, headers=k)).json()
    assert {t["id"] for t in ch["removed"]} >= {s["laptop2"]["id"], s["tee"]["id"]} and ch["next_since"]


async def test_feed_tokens_rotation_revocation_expiry_rate_limit(client, caplog):
    h = await register(client)
    await seed(client, h)
    await enable(client, h)
    f = (await client.post(f"{API}/distribution/feeds", json={"name": "Public", "format": "jsonld"}, headers=h)).json()
    await _generate(client, h, f["id"])
    tok = (await client.post(f"{API}/distribution/feeds/{f['id']}/tokens", json={"name": "Partner", "rate_limit_per_minute": 4},
                             headers=h)).json()
    path = tok["url"].split("http://localhost:8000")[-1]
    assert tok["url"].startswith("http") and "arf_" in tok["url"]
    listed = (await client.get(f"{API}/distribution/feeds", headers=h)).json()["items"][0]["tokens"][0]
    assert "url" not in listed and listed["prefix"] == tok["prefix"]  # the secret is never shown again
    r = await client.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/ld+json") and r.headers["x-feed-version"] == "1"
    assert (await client.get(path, headers={"If-None-Match": r.headers["etag"]})).status_code == 304
    for _ in range(2):
        await client.get(path)
    assert (await client.get(path)).status_code == 429  # per-token rate limit
    # rotation: the old URL stops working, the new one works
    rot = (await client.post(f"{API}/distribution/feed-tokens/{tok['id']}/rotate", headers=h)).json()
    assert (await client.get(path)).status_code == 410
    new_path = rot["url"].split("http://localhost:8000")[-1]
    assert (await client.get(new_path)).status_code == 200
    # revocation
    await client.post(f"{API}/distribution/feed-tokens/{rot['id']}/revoke", headers=h)
    assert (await client.get(new_path)).status_code == 410
    # expiry
    exp = (await client.post(f"{API}/distribution/feeds/{f['id']}/tokens",
                             json={"name": "Short", "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()},
                             headers=h)).json()
    assert (await client.post(f"{API}/distribution/feeds/{f['id']}/tokens", json={"name": "Past", "expires_at": "2020-01-01T00:00:00Z"},
                              headers=h)).status_code == 422
    await _expire_token(client, exp["id"])
    assert (await client.get(exp["url"].split("http://localhost:8000")[-1])).status_code == 410
    assert (await client.get("/api/v3/feeds/arf_doesnotexist")).status_code == 404
    usage = (await client.get(f"{API}/distribution/feeds", headers=h)).json()["items"][0]["tokens"]
    assert sum(t["usage_count"] for t in usage) >= 4
    # the secret never appears in our logs
    assert not any(tok["url"].split("/")[-1] in rec.getMessage() for rec in caplog.records)


async def _expire_token(client, token_id):
    import uuid

    from app.db.session import session_factory
    from app.models import FeedToken

    async with session_factory()() as s:
        await s.execute(update(FeedToken).where(FeedToken.id == uuid.UUID(token_id))
                        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await s.commit()


async def test_expired_snapshot_is_regenerated_on_request(client, db):
    h = await register(client)
    await seed(client, h)
    await enable(client, h)
    f = (await client.post(f"{API}/distribution/feeds", json={"name": "Fresh", "max_age_hours": 1}, headers=h)).json()
    snap, _ = await _generate(client, h, f["id"])
    tok = (await client.post(f"{API}/distribution/feeds/{f['id']}/tokens", json={"name": "T"}, headers=h)).json()
    await db.execute(update(FeedSnapshot).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    await db.commit()
    r = await client.get(tok["url"].split("http://localhost:8000")[-1])
    assert r.status_code == 200 and r.headers["x-feed-version"] == "2"  # never serves past the refresh policy


async def test_scheduled_feed_generation_runs_once(client, db):
    h = await register(client)
    await seed(client, h)
    await enable(client, h)
    f = (await client.post(f"{API}/distribution/feeds", json={"name": "Hourly", "schedule": "hourly"}, headers=h)).json()
    assert f["next_run_at"] is not None
    await db.execute(update(FeedConfiguration).values(next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await db.commit()
    r1 = await feeds.generate_due_feeds(db)
    r2 = await feeds.generate_due_feeds(db)
    assert r1["generated"] == 1 and r2["generated"] == 0
    snaps = (await db.execute(select(FeedSnapshot))).scalars().all()
    assert len(snaps) == 1 and snaps[0].trigger == "scheduled" and snaps[0].status == "completed"


async def test_platform_profiles_validate_against_official_field_rules(client):
    h = await register(client)
    s = await seed(client, h)
    await enable(client, h)
    r = await client.post(f"{API}/distribution/platforms/openai_chatgpt/validate", headers=h)
    assert r.status_code == 200, r.text
    v = r.json()["last_validation"]
    # only the laptop has brand + description + image: others are excluded with the missing fields named
    assert v["records"] == 1 and v["excluded"] >= 4 and any("missing required" in e for e in v["errors"])
    snap_id = v["snapshot_id"]
    body = (await client.get(f"{API}/distribution/feed-snapshots/{snap_id}/download", headers=h)).text
    row = json.loads(body.splitlines()[0])
    assert row["item_id"] == s["laptop"]["id"] and row["price"] == "65000.00 INR" and row["availability"] == "in_stock"
    assert row["seller_name"] == "Sharma Electricals" and row["gtin"] == "4006381333931" and "cost" not in body
    st = r.json()
    assert st["status"] == "setup_required" or st["status"] == "not_configured"
    assert st["capabilities"]["live_status"] == "blocked"
    # submission is honestly blocked (needs OpenAI approval + SFTP credentials)
    sub = await client.post(f"{API}/distribution/platforms/openai_chatgpt/submit", headers=h)
    assert sub.status_code == 409 and "approved partners" in sub.json()["error"]["message"]
    # merchant-reported application -> pending_verification, never "active"
    await client.put(f"{API}/distribution/platforms/openai_chatgpt", json={"enabled": True}, headers=h)
    st = (await client.post(f"{API}/distribution/platforms/openai_chatgpt/application", json={"submitted": True}, headers=h)).json()
    assert st["status"] == "pending_verification"
    g = (await client.post(f"{API}/distribution/platforms/google_ucp/validate", headers=h)).json()
    gbody = (await client.get(f"{API}/distribution/feed-snapshots/{g['last_validation']['snapshot_id']}/download", headers=h)).text
    root = ET.fromstring(gbody)
    ns = {"g": "http://base.google.com/ns/1.0"}
    prices = [i.find("g:price", ns).text for i in root.iter("item")]
    assert "65000.00 INR" in prices and all(p.endswith(" INR") for p in prices)
    perp = await client.post(f"{API}/distribution/platforms/perplexity/validate", headers=h)
    assert perp.status_code == 409
    allp = (await client.get(f"{API}/distribution/platforms", headers=h)).json()
    mcp = next(p for p in allp["items"] if p["provider"] == "mcp_clients")
    assert mcp["status"] == "connected"  # enabled but no real MCP traffic yet -> not "active"
    assert "does not by itself make products appear" in allp["disclaimer"]


# ---------------------------------------------------------------- webhooks (SIMULATED destination)
class Receiver:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.fail_next = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            self.fail_next -= 1
            return httpx.Response(500)
        return httpx.Response(204)


@pytest.fixture
def receiver(monkeypatch):
    from app.services.crawler import fetcher as fetcher_mod
    from app.services.integrations import http as http_mod
    from app.tests.sitekit import public_resolver

    rec = Receiver()
    monkeypatch.setattr(http_mod, "TRANSPORT_OVERRIDE", httpx.MockTransport(rec.handler))
    monkeypatch.setattr(fetcher_mod, "RESOLVER_OVERRIDE", public_resolver)
    return rec


async def test_webhooks_signing_retry_idempotency_replay_rotation(client, db, receiver):
    from app.services.distribution import webhooks

    h = await register(client)
    s = await seed(client, h)
    await enable(client, h)
    bad = await client.post(f"{API}/distribution/webhooks", json={"url": "http://127.0.0.1/hook"}, headers=h)
    assert bad.status_code == 422  # SSRF / https rule
    assert (await client.post(f"{API}/distribution/webhooks", json={"url": "https://hooks.example.com/x", "events": ["nope"]},
                              headers=h)).status_code == 422
    sub = (await client.post(f"{API}/distribution/webhooks", json={"url": "https://hooks.example.com/agentready",
                                                                   "events": ["product.price_changed", "product.unpublished"]},
                             headers=h)).json()
    secret = sub["secret"]
    assert secret.startswith("whsec_")
    await webhooks.tick(db)  # earlier events (product creation) predate the subscription's interests
    receiver.requests.clear()
    receiver.fail_next = 1
    await client.put(f"{API}/products/{s['motor']['id']}/pricing", json={"regular_price": "11000", "cost_price": "7000"}, headers=h)
    await client.put(f"{API}/products/{s['draft']['id']}/pricing", json={"regular_price": "1"}, headers=h)  # not public
    out = await webhooks.tick(db)
    assert out["deliveries"] == 1 and len(receiver.requests) == 1
    first = receiver.requests[0]
    d = (await db.execute(select(WebhookDelivery))).scalars().all()
    d = [x for x in d if x.event_type == "product.price_changed"][0]
    assert d.status == "pending" and d.attempts == 1 and d.last_status_code == 500
    assert d.next_attempt_at > datetime.now(timezone.utc) + timedelta(seconds=20)  # exponential backoff (30s +/- 10%)
    await db.execute(update(WebhookDelivery).where(WebhookDelivery.id == d.id).values(next_attempt_at=datetime.now(timezone.utc)))
    await db.commit()
    await webhooks.tick(db)
    second = receiver.requests[1]
    # same event id on the retry (idempotency key), valid signature, sane timestamp, public data only
    assert first.headers["webhook-id"] == second.headers["webhook-id"]
    assert webhooks.verify(secret, dict(second.headers), second.content)
    assert not webhooks.verify(secret, {**dict(second.headers), "webhook-timestamp": "1000"}, second.content)  # replayed old
    assert not webhooks.verify("whsec_" + "A" * 44, dict(second.headers), second.content)
    payload = json.loads(second.content)
    assert payload["type"] == "product.price_changed" and payload["data"]["product"]["price"]["current"] == "11000.00"
    assert "7000" not in second.content.decode() and "cost" not in second.content.decode()
    await db.refresh(d)
    assert d.status == "succeeded" and d.attempts == 2
    # replay keeps the webhook-id
    rp = await client.post(f"{API}/distribution/webhook-deliveries/{d.id}/replay", headers=h)
    assert rp.status_code == 201 and rp.json()["event_id"] == d.event_id
    await webhooks.tick(db)
    assert receiver.requests[-1].headers["webhook-id"] == first.headers["webhook-id"]
    # secret rotation: both secrets sign during the grace period
    rot = (await client.post(f"{API}/distribution/webhooks/{sub['id']}/rotate-secret", headers=h)).json()
    await client.patch(f"{API}/products/{s['motor']['id']}", json={"publication_status": "unpublished"}, headers=h)
    await webhooks.tick(db)
    last = receiver.requests[-1]
    assert len(last.headers["webhook-signature"].split()) == 2
    assert webhooks.verify(rot["secret"], dict(last.headers), last.content) and webhooks.verify(secret, dict(last.headers), last.content)
    tomb = json.loads(last.content)
    assert tomb["type"] == "product.unpublished" and tomb["data"]["product"] == {"id": s["motor"]["id"], "removed": True,
                                                                                 "reason": "unpublished"}
    # permanent failure after max attempts, recorded in analytics
    receiver.fail_next = 100
    await client.patch(f"{API}/products/{s['motor']['id']}", json={"publication_status": "published"}, headers=h)
    await client.put(f"{API}/products/{s['motor']['id']}/pricing", json={"regular_price": "10500"}, headers=h)
    await webhooks.tick(db)
    dd = [x for x in (await db.execute(select(WebhookDelivery).where(WebhookDelivery.status == "pending"))).scalars()]
    assert dd
    for _ in range(webhooks.MAX_ATTEMPTS):
        await db.execute(update(WebhookDelivery).where(WebhookDelivery.status == "pending").values(next_attempt_at=datetime.now(timezone.utc)))
        await db.commit()
        await webhooks.tick(db)
    failed = (await db.execute(select(WebhookDelivery).where(WebhookDelivery.status == "failed"))).scalars().all()
    assert failed and failed[0].attempts == webhooks.MAX_ATTEMPTS
    lst = (await client.get(f"{API}/distribution/webhooks/{sub['id']}/deliveries?status=failed", headers=h)).json()
    assert lst["total"] >= 1
    stats = (await client.get(f"{API}/distribution/analytics", headers=h)).json()["observed"]["webhooks"]
    assert stats["succeeded"] >= 2 and stats["failed"] >= 1
    assert (await db.execute(select(OutboxEvent).where(OutboxEvent.dispatched_at.is_(None)))).first() is None


# ---------------------------------------------------------------- checkout foundation
async def test_checkout_validation_and_handoff(client):
    h = await register(client)
    s = await seed(client, h)
    await enable(client, h)
    reader = await agent_key(client, h)
    k = await agent_key(client, h, scopes=("agent:read", "checkout:write"))
    item = {"product_id": s["laptop"]["id"], "quantity": 1}
    assert (await client.post(f"{V3}/carts", json={"items": [item]}, headers=reader)).status_code == 403  # scope
    assert (await client.post(f"{V3}/carts", json={"items": [item]})).status_code == 403  # anonymous
    r = await client.post(f"{V3}/carts", json={"items": [item]}, headers=k)
    assert r.status_code == 403 and r.json()["error"]["code"] == "checkout_disabled"
    await client.put(f"{API}/distribution/settings", json={"checkout_enabled": True}, headers=h)
    r = await client.post(f"{V3}/carts", json={"items": [
        item, {"product_id": s["tee"]["id"]}, {"product_id": s["motor12"]["id"]},
        {"product_id": s["laptop"]["id"], "quantity": 50}, {"product_id": s["draft"]["id"]},
        {"product_id": s["laptop2"]["id"], "expected_unit_price": "1.00"}]}, headers=k)
    assert r.status_code == 201, r.text
    cart = r.json()
    codes = {i["code"] for i in cart["issues"]}
    assert codes == {"variant_required", "unavailable", "insufficient_stock", "not_found", "price_changed"}
    stock_issue = next(i for i in cart["issues"] if i["code"] == "insufficient_stock")
    assert "7" not in stock_issue["message"]  # the private quantity is not revealed
    # clean cart -> checkout hand-off to the merchant's verified product page; payment is never handled
    tee_variant = next(v for v in (await client.get(f"{V3}/products/{s['tee']['id']}/variants", headers=k)).json()["variants"]
                       if v["options"] == {"Size": "M", "Color": "Black"})
    cart = (await client.post(f"{V3}/carts", json={"items": [item, {"product_id": s["tee"]["id"], "variant_id": tee_variant["id"],
                                                                    "quantity": 2}]}, headers=k)).json()
    assert cart["issues"] == [] and cart["subtotal"] == "65900.00" and cart["currency"] == "INR"
    r = await client.post(f"{V3}/carts/{cart['id']}/checkout", json={"expected_subtotal": "65000.00"}, headers=k)
    assert r.status_code == 409 and r.json()["error"]["code"] == "price_changed"
    r = await client.post(f"{V3}/carts/{cart['id']}/checkout", json={"expected_subtotal": "65900.00", "expected_currency": "INR"},
                          headers=k)
    assert r.status_code == 201, r.text
    sess = r.json()
    assert sess["method"] == "merchant_product_urls" and sess["payment_status"] == "not_handled" and sess["order_status"] == "unknown"
    assert sess["checkout_url"] is None
    # another consumer cannot read it
    other = await agent_key(client, h, scopes=("agent:read", "checkout:write"))
    assert (await client.get(f"{V3}/checkout-sessions/{sess['id']}", headers=other)).status_code == 404
    assert (await client.get(f"{V3}/checkout-sessions/{sess['id']}", headers=k)).json()["id"] == sess["id"]
    # price changed after the cart -> re-verified at checkout
    cart2 = (await client.post(f"{V3}/carts", json={"items": [item]}, headers=k)).json()
    await client.put(f"{API}/products/{s['laptop']['id']}/pricing", json={"regular_price": "66000"}, headers=h)
    r = await client.post(f"{V3}/carts/{cart2['id']}/checkout", json={"expected_subtotal": cart2["subtotal"]}, headers=k)
    assert r.status_code == 409
    audit = (await client.get(f"{API}/audit-logs?action=checkout.session_created", headers=h)).json()
    assert audit["total"] == 1


async def test_checkout_shopify_cart_permalink(client, db):
    import uuid

    from app.models import CommerceIntegration, IntegrationLink

    h = await register(client)
    s = await seed(client, h)
    await enable(client, h, checkout_enabled=True)
    k = await agent_key(client, h, scopes=("agent:read", "checkout:write"))
    variants = (await client.get(f"{V3}/products/{s['tee']['id']}/variants", headers=k)).json()["variants"]
    bid = (await client.get(f"{API}/distribution/settings", headers=h)).json()
    from app.models import Product

    biz = (await db.execute(select(Product.business_id).where(Product.id == uuid.UUID(s["tee"]["id"])))).scalar_one()
    integ = CommerceIntegration(business_id=biz, provider="shopify", store_url="https://acme.myshopify.com", status="connected")
    db.add(integ)
    await db.flush()
    db.add(IntegrationLink(business_id=biz, integration_id=integ.id, product_id=uuid.UUID(s["tee"]["id"]),
                           variant_id=uuid.UUID(variants[0]["id"]), external_id="gid://shopify/Product/11",
                           external_variant_id="gid://shopify/ProductVariant/4242"))
    await db.commit()
    cart = (await client.post(f"{V3}/carts", json={"items": [{"product_id": s["tee"]["id"], "variant_id": variants[0]["id"],
                                                              "quantity": 3}]}, headers=k)).json()
    sess = (await client.post(f"{V3}/carts/{cart['id']}/checkout", json={}, headers=k)).json()
    assert sess["method"] == "shopify_cart_permalink" and sess["checkout_url"] == "https://acme.myshopify.com/cart/4242:3"
    assert bid["checkout_enabled"] is True


# ---------------------------------------------------------------- MCP protocol
def _rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, **({"params": params} if params is not None else {})}


async def test_mcp_streamable_http_protocol(client):
    h = await register(client)
    s = await seed(client, h)
    k = await agent_key(client, h)
    await enable(client, h)
    hdr = {**k, "Accept": "application/json, text/event-stream"}
    # handshake era
    r = await client.post("/api/v3/mcp", json=_rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                                  "clientInfo": {"name": "t", "version": "1"}}), headers=hdr)
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-11-25" and res["capabilities"]["tools"] and res["serverInfo"]["name"] == "agentready"
    assert "mcp-session-id" not in r.headers  # stateless
    old = await client.post("/api/v3/mcp", json=_rpc("initialize", {"protocolVersion": "2024-01-01"}), headers=hdr)
    assert old.json()["result"]["protocolVersion"] == "2025-11-25"  # counter-offer
    assert (await client.post("/api/v3/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                              headers=hdr)).status_code == 202
    hv = {**hdr, "MCP-Protocol-Version": "2025-11-25"}
    tl = (await client.post("/api/v3/mcp", json=_rpc("tools/list"), headers=hv)).json()["result"]["tools"]
    names = {t["name"] for t in tl}
    assert {"search_products", "get_product_details", "compare_products", "get_product_variants",
            "check_product_availability", "get_product_pricing", "search_merchants", "get_merchant_catalog"} <= names
    assert all(t["inputSchema"]["type"] == "object" and t["outputSchema"]["type"] == "object" and t["annotations"]["readOnlyHint"]
               for t in tl)
    assert not any(n for n in names if "inventory" in n or "set_" in n or "update" in n)  # no privileged tools
    call = await client.post("/api/v3/mcp", json=_rpc("tools/call", {"name": "search_products", "arguments": {
        "query": "motor", "attributes": ["voltage:230 V"]}}), headers=hv)
    out = call.json()["result"]
    assert out["isError"] is False and out["structuredContent"]["items"][0]["product"]["name"] == "Industrial Motor 2HP"
    assert json.loads(out["content"][0]["text"])["total"] == 1
    bad = (await client.post("/api/v3/mcp", json=_rpc("tools/call", {"name": "search_products", "arguments": {"page": 0}}),
                             headers=hv)).json()["result"]
    assert bad["isError"] is True and "page" in bad["content"][0]["text"]
    nf = (await client.post("/api/v3/mcp", json=_rpc("tools/call", {"name": "get_product_details",
                                                                    "arguments": {"product_id": s["draft"]["id"]}}), headers=hv)).json()
    assert nf["result"]["isError"] is True and "not_found" in nf["result"]["content"][0]["text"]
    unk = (await client.post("/api/v3/mcp", json=_rpc("tools/call", {"name": "set_price", "arguments": {}}), headers=hv)).json()
    assert unk["error"]["code"] == -32602
    assert (await client.post("/api/v3/mcp", json=_rpc("resources/list"), headers=hv)).json()["error"]["code"] == -32601
    # modern (2026-07-28) era
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
    hm = {**hdr, "MCP-Protocol-Version": "2026-07-28"}
    d = (await client.post("/api/v3/mcp", json=_rpc("server/discover", {"_meta": meta}), headers=hm)).json()["result"]
    assert "2026-07-28" in d["supportedVersions"] and d["resultType"] == "complete" and d["cacheScope"] == "private"
    assert d["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "agentready"
    tl2 = (await client.post("/api/v3/mcp", json=_rpc("tools/list", {"_meta": meta}), headers=hm)).json()["result"]
    assert tl2["resultType"] == "complete" and tl2["ttlMs"] > 0 and len(tl2["tools"]) == len(tl)
    mismatch = await client.post("/api/v3/mcp", json=_rpc("tools/list", {"_meta": meta}),
                                 headers={**hdr, "MCP-Protocol-Version": "2025-11-25"})
    assert mismatch.status_code == 400 and mismatch.json()["error"]["code"] == -32020
    unsupported = await client.post("/api/v3/mcp", json=_rpc("server/discover", {"_meta": {**meta, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}}),
                                    headers=hdr)
    assert unsupported.json()["error"]["code"] == -32022 and "2026-07-28" in unsupported.json()["error"]["data"]["supported"]
    # transport rules
    assert (await client.get("/api/v3/mcp")).status_code == 405
    assert (await client.post("/api/v3/mcp", content=b"{not json", headers=hdr)).status_code == 400
    assert (await client.post("/api/v3/mcp", json=[_rpc("ping")], headers=hdr)).status_code == 400
    assert (await client.post("/api/v3/mcp", json=_rpc("ping"), headers={**hdr, "Origin": "https://evil.example"})).status_code == 403
    # authentication failures are protocol errors; analytics count the MCP calls
    badkey = await client.post("/api/v3/mcp", json=_rpc("tools/call", {"name": "search_merchants", "arguments": {}}),
                               headers={"X-Catalog-Key": "ar_cat_invalid"})
    assert badkey.status_code == 401
    an = (await client.get(f"{API}/distribution/analytics?channel=mcp", headers=h)).json()["observed"]
    assert an["mcp_tool_calls"] >= 1
    plat = (await client.get(f"{API}/distribution/platforms", headers=h)).json()["items"]
    assert next(p for p in plat if p["provider"] == "mcp_clients")["status"] == "active"  # derived from real MCP traffic


async def test_rest_tools_and_capabilities(client):
    h = await register(client)
    s = await seed(client, h)
    k = await agent_key(client, h)
    await enable(client, h)
    cap = (await client.get(f"{V3}/capabilities")).json()
    assert cap["service"]["api_version"] == "3.0" and "2026-07-28" in cap["mcp"]["protocol_versions"]
    assert (await client.get("/.well-known/agentready.json")).json()["service"]["name"] == "AgentReady"
    tools = (await client.get(f"{V3}/tools")).json()["tools"]
    assert len(tools) >= 8
    r = await client.post(f"{V3}/tools/get_product_pricing", json={"product_id": s["motor"]["id"]}, headers=k)
    assert r.status_code == 200 and r.json()["result"]["price"]["current"] == "12000.00"
    assert (await client.post(f"{V3}/tools/get_product_pricing", json={"product_id": s["motor"]["id"], "x": 1},
                              headers=k)).status_code == 422
    assert (await client.post(f"{V3}/tools/delete_everything", json={}, headers=k)).status_code == 404
    rec = await client.post(f"{V3}/recommend", json={"request": "black T-shirts in medium size under ₹600"}, headers=k)
    body = rec.json()
    assert rec.status_code == 200, rec.text
    assert [i["product"]["name"] for i in body["items"]] == ["Basic T-shirt"]
    assert body["interpretation"]["source"] == "deterministic" and body["interpretation"]["filters"]["currency"] == "INR"
    ai = await client.post(f"{V3}/recommend", json={"request": "cheap motors", "interpret": "ai"}, headers=k)
    assert ai.status_code == 422 and "not available" in ai.json()["error"]["message"]  # Groq not configured in tests


async def test_agent_rate_limits_and_analytics(client):
    h = await register(client)
    await seed(client, h)
    k = await agent_key(client, h, rate=3)
    await enable(client, h, public_network=True)
    for _ in range(3):
        assert (await client.get(f"{V3}/search?q=motor", headers=k)).status_code == 200
    assert (await client.get(f"{V3}/search?q=motor", headers=k)).status_code == 429
    for _ in range(5):
        await client.get(f"{V3}/search?q=laptop")  # anonymous public-network agent
    an = (await client.get(f"{API}/distribution/analytics", headers=h)).json()
    obs = an["observed"]
    assert obs["search_requests"] == 8
    kinds = {c["type"]: c["requests"] for c in obs["consumers"]}
    assert kinds["api_key"] == 3 and kinds["anonymous"] == 5
    assert set(an["not_measured"]) >= {"purchases", "conversions"} and an["derived"]["freshness"]["distributed_products"] == 5
    ov = (await client.get(f"{API}/distribution/overview", headers=h)).json()
    assert ov["products"]["distributed"] == 5 and ov["last_7_days"]["agent_requests"] == 8
    assert (await client.get(f"{API}/distribution/analytics?channel=feeds", headers=h)).json()["observed"]["agent_requests"] == 0
