"""Phase 2: commerce management (products, pricing, inventory, variants, taxonomy, bulk, sync, catalog API, audit)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import AuditLog, InventoryTransaction, PriceHistory, Product, SyncSchedule
from app.tests.conftest import register
from app.workers.dispatch import wait_for_inline_tasks

API = "/api/v1"


async def make(client, h, **kw):
    body = {"name": "Cotton T-shirt", "sku": "TS-1", "regular_price": "499.00", "currency": "INR",
            "lifecycle_status": "active", **kw}
    r = await client.post(f"{API}/products", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def approve(client, h, pid):
    r = await client.patch(f"{API}/products/{pid}", json={"approval_status": "approved"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- products
async def test_manual_product_creation_and_lifecycle(client):
    h = await register(client)
    p = await make(client, h, description="Soft cotton", brand="Acme", gtin="4006381333931",
                   attributes=[{"label": "Material", "value": "Cotton"}], images=[{"url": "https://cdn.example/t.jpg"}],
                   shipping={"requires_shipping": True, "lead_time_days": 2}, weight={"value": "0.2", "unit": "kg"})
    assert p["origin"] == "manual" and p["lifecycle_status"] == "active" and p["approval_status"] == "needs_review"
    assert p["pricing"]["regular_price"] == "499.00" and p["pricing"]["currency"] == "INR"
    assert p["publication_status"] == "unpublished" and p["is_public"] is False
    detail = (await client.get(f"{API}/products/{p['id']}", headers=h)).json()
    # never fabricated: no website -> no "missing source" warning, evidence says merchant entered it
    assert not any(i["code"] == "missing_source" for i in detail["issues"])
    assert {e["extraction_method"] for e in detail["evidence"]} == {"merchant"}
    hist = (await client.get(f"{API}/products/{p['id']}/price-history", headers=h)).json()["items"]
    assert hist[0]["change_type"] == "product_created" and hist[0]["new_amount"] == "499.00"

    # approve -> published; archive -> unpublished; restore -> active; soft delete -> hidden; restore
    p = await approve(client, h, p["id"])
    assert p["publication_status"] == "published" and p["is_public"]
    r = await client.post(f"{API}/products/{p['id']}/archive", headers=h)
    assert r.json()["lifecycle_status"] == "archived" and r.json()["publication_status"] == "unpublished"
    assert (await client.get(f"{API}/products", headers=h)).json()["total"] == 0  # archived hidden by default
    assert (await client.get(f"{API}/products?lifecycle_status=archived", headers=h)).json()["total"] == 1
    r = await client.post(f"{API}/products/{p['id']}/restore", headers=h)
    assert r.json()["lifecycle_status"] == "active"
    assert (await client.delete(f"{API}/products/{p['id']}", headers=h)).status_code == 204
    assert (await client.get(f"{API}/products", headers=h)).json()["total"] == 0
    assert (await client.get(f"{API}/products?lifecycle_status=deleted", headers=h)).json()["total"] == 1
    assert (await client.patch(f"{API}/products/{p['id']}", json={"name": "x"}, headers=h)).status_code == 404
    r = await client.post(f"{API}/products/{p['id']}/restore", headers=h)
    assert r.status_code == 200 and r.json()["deleted_at"] is None
    # publishing requires approval + active lifecycle
    draft = await make(client, h, name="Draft thing", sku="D-1", lifecycle_status="draft")
    r = await client.patch(f"{API}/products/{draft['id']}", json={"publication_status": "published"}, headers=h)
    assert r.status_code == 409
    history = (await client.get(f"{API}/products/{p['id']}/history", headers=h)).json()["items"]
    actions = {e["action"] for e in history}
    assert {"product.created", "product.approved", "product.archived", "product.restored", "product.deleted"} <= actions


async def test_mass_assignment_and_validation_rejected(client):
    h = await register(client)
    r = await client.post(f"{API}/products", json={"name": "X", "business_id": "00000000-0000-0000-0000-000000000000"}, headers=h)
    assert r.status_code == 422
    p = await make(client, h)
    for bad in ({"version": 99}, {"quality_status": "valid"}, {"manual_overrides": []}, {"cost_price_amount": 1}):
        assert (await client.patch(f"{API}/products/{p['id']}", json=bad, headers=h)).status_code == 422, bad
    assert (await client.post(f"{API}/products", json={"name": "Y", "sku": "TS-1"}, headers=h)).status_code == 409  # SKU taken
    r = await client.post(f"{API}/products", json={"name": "Z", "images": [{"url": "javascript:alert(1)"}]}, headers=h)
    assert r.status_code == 422


async def test_catalog_filters(client):
    h = await register(client)
    a = await make(client, h, name="Alpha", sku="A1", brand="Acme", regular_price="100", product_type="physical")
    await make(client, h, name="Beta", sku="B1", brand="Other", regular_price="900", product_type="digital")
    await client.put(f"{API}/products/{a['id']}/inventory", json={"track_inventory": True, "stock_quantity": 2}, headers=h)

    async def names(qs):
        return {p["name"] for p in (await client.get(f"{API}/products?{qs}", headers=h)).json()["items"]}

    assert await names("brand=Acme") == {"Alpha"}
    assert await names("price_min=500") == {"Beta"}
    assert await names("product_type=digital") == {"Beta"}
    assert await names("stock_status=low_stock") == {"Alpha"}  # default threshold 5
    assert await names("q=b1") == {"Beta"}
    facets = (await client.get(f"{API}/products/facets", headers=h)).json()
    assert {b["value"] for b in facets["brands"]} == {"Acme", "Other"}


# ---------------------------------------------------------------- pricing
async def test_pricing_rules_history_and_cost_privacy(client):
    h = await register(client)
    p = await make(client, h)
    put = lambda b: client.put(f"{API}/products/{p['id']}/pricing", json=b, headers=h)  # noqa: E731
    r = await put({"sale_price": "599"})
    assert r.status_code == 422 and "lower than the regular price" in r.json()["error"]["message"]
    r = await put({"sale_price": "399", "cost_price": "200", "reason": "Diwali"})
    assert r.status_code == 200 and r.json()["price_status"] == "on_sale" and r.json()["effective_price"] == "399.00"
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    r = await put({"sale_starts_at": future})
    assert r.json()["price_status"] == "sale_scheduled" and r.json()["effective_price"] == "499.00"
    assert (await put({"sale_starts_at": "2026-01-01T00:00:00"})).status_code == 422  # naive datetime refused
    r = await put({"regular_price": "549.995"})  # Decimal, half-up to cents
    assert r.json()["regular_price"] == "550.00"
    hist = (await client.get(f"{API}/products/{p['id']}/price-history", headers=h)).json()["items"]
    kinds = [(x["price_type"], x["old_amount"], x["new_amount"]) for x in hist]
    assert ("sale", None, "399.00") in kinds and ("cost", None, "200.00") in kinds and ("regular", "499.00", "550.00") in kinds
    assert all(x["actor"] == "owner@example.com" for x in hist)
    assert next(x for x in hist if x["price_type"] == "sale")["reason"] == "Diwali"

    # cost price never leaves the merchant UI: not in exports, v1 catalog or v2 catalog
    await approve(client, h, p["id"])
    key = (await client.post(f"{API}/business/catalog-key", headers=h)).json()["catalog_api_key"]
    for path in ("/api/v1/catalog", "/api/v2/catalog/products", f"/api/v2/catalog/products/{p['id']}"):
        text = (await client.get(path, headers={"X-Catalog-Key": key})).text
        assert "200.00" not in text and "cost" not in text, path
    exp = (await client.post(f"{API}/exports", json={"format": "json"}, headers=h)).json()
    body = (await client.get(f"{API}/exports/{exp['id']}/download", headers=h)).text
    assert "cost_price" not in body and '"reserved"' not in body


async def test_history_is_immutable(client, db):
    h = await register(client)
    p = await make(client, h)
    await client.put(f"{API}/products/{p['id']}/pricing", json={"regular_price": "600"}, headers=h)
    row = (await db.execute(select(PriceHistory))).scalars().first()
    row.new_amount = Decimal("1")
    with pytest.raises(RuntimeError, match="immutable"):
        await db.flush()
    await db.rollback()
    entry = (await db.execute(select(AuditLog))).scalars().first()
    await db.delete(entry)
    with pytest.raises(RuntimeError, match="immutable"):
        await db.flush()


async def test_scheduled_price_changes(client, db):
    from app.services.commerce.pricing import apply_due_price_changes

    h = await register(client)
    p = await make(client, h)
    at = datetime.now(timezone.utc) + timedelta(hours=2)
    r = await client.post(f"{API}/pricing/schedules", json={"product_id": p["id"], "new_amount": "449", "effective_at": at.isoformat(),
                                                            "reason": "New season"}, headers=h)
    assert r.status_code == 201, r.text
    keep = r.json()
    r2 = await client.post(f"{API}/pricing/schedules", json={"product_id": p["id"], "new_amount": "100",
                                                             "effective_at": at.isoformat()}, headers=h)
    assert (await client.post(f"{API}/pricing/schedules/{r2.json()['id']}/cancel", headers=h)).json()["status"] == "cancelled"
    past = await client.post(f"{API}/pricing/schedules", json={"product_id": p["id"], "new_amount": "1",
                                                               "effective_at": "2020-01-01T00:00:00+00:00"}, headers=h)
    assert past.status_code == 422
    # not due yet: current price unchanged (scheduled changes are separate from active prices)
    assert (await apply_due_price_changes(db))["applied"] == 0
    assert (await client.get(f"{API}/products/{p['id']}/pricing", headers=h)).json()["regular_price"] == "499.00"
    # due: applied exactly once even if the scheduler runs twice
    res1 = await apply_due_price_changes(db, at=at + timedelta(minutes=1))
    res2 = await apply_due_price_changes(db, at=at + timedelta(minutes=2))
    assert res1["applied"] == 1 and res2["applied"] == 0
    pricing = (await client.get(f"{API}/products/{p['id']}/pricing", headers=h)).json()
    assert pricing["regular_price"] == "449.00" and pricing["scheduled_changes"] == []
    hist = (await client.get(f"{API}/products/{p['id']}/price-history", headers=h)).json()["items"]
    assert hist[0]["change_type"] == "scheduled" and hist[0]["reason"] == "New season"
    sched = (await client.get(f"{API}/pricing/schedules?status=applied,cancelled", headers=h)).json()["items"]
    assert {s["id"]: s["status"] for s in sched} == {keep["id"]: "applied", r2.json()["id"]: "cancelled"}


# ---------------------------------------------------------------- inventory
async def test_inventory_operations_and_low_stock(client):
    h = await register(client)
    p = await make(client, h)
    adj = lambda **b: client.post(f"{API}/inventory/adjust", json={"product_id": p["id"], **b}, headers=h)  # noqa: E731
    assert (await adj(operation="increase", quantity=5)).status_code == 422  # tracking disabled
    r = await client.put(f"{API}/products/{p['id']}/inventory", json={"track_inventory": True, "low_stock_threshold": 3,
                                                                     "stock_quantity": 10, "reason": "Stock take"}, headers=h)
    assert r.json()["on_hand"] == 10 and r.json()["stock_status"] == "in_stock" and r.json()["availability"] == "InStock"
    assert (await adj(operation="decrease", quantity=11)).status_code == 422  # never negative without backorders
    r = (await adj(operation="reserve", quantity=4)).json()
    assert r["inventory"]["available"] == 6 and r["inventory"]["reserved"] == 4
    r = (await adj(operation="decrease", quantity=3, reason="Damaged")).json()
    assert (r["previous"], r["adjustment"], r["new"]) == (10, -3, 7) and r["stock_status"] == "low_stock"  # 7-4=3 <= 3
    low = (await client.get(f"{API}/inventory/low-stock", headers=h)).json()
    assert [i["name"] for i in low["items"]] == ["Cotton T-shirt"] and low["counts"]["low_stock"] == 1
    r = (await adj(operation="mark_out_of_stock")).json()
    assert r["inventory"]["available"] == 0 and r["stock_status"] == "out_of_stock"
    assert (await client.get(f"{API}/products/{p['id']}", headers=h)).json()["availability"] == "OutOfStock"
    await client.put(f"{API}/products/{p['id']}/inventory", json={"backorder_policy": "allow"}, headers=h)
    r = (await adj(operation="decrease", quantity=10)).json()
    assert r["new"] == -6 and r["stock_status"] == "backorder"
    txs = (await client.get(f"{API}/inventory/transactions?product_id={p['id']}", headers=h)).json()["items"]
    assert [t["transaction_type"] for t in txs][::-1] == ["set", "reserve", "decrease", "mark_out_of_stock", "decrease"]
    assert txs[-1]["reason"] == "Stock take" and txs[-1]["actor"] == "owner@example.com"
    assert all(t["new_quantity"] == t["previous_quantity"] + t["adjustment"] for t in txs)


async def test_inventory_concurrency_no_lost_updates(client, db_engine):
    from app.db.session import session_factory
    from app.services.commerce.audit import Actor
    from app.services.commerce.inventory import change_stock

    h = await register(client)
    p = await make(client, h)
    await client.put(f"{API}/products/{p['id']}/inventory", json={"track_inventory": True, "stock_quantity": 0}, headers=h)

    async def one():
        async with session_factory()() as s:
            prod = await s.get(Product, __import__("uuid").UUID(p["id"]))
            await change_stock(s, prod, actor=Actor.system("test"), operation="increase", quantity=1)
            await s.commit()

    await asyncio.gather(*(one() for _ in range(15)))
    inv = (await client.get(f"{API}/products/{p['id']}/inventory", headers=h)).json()
    assert inv["on_hand"] == 15
    async with session_factory()() as s:
        txs = (await s.execute(select(InventoryTransaction).where(InventoryTransaction.transaction_type == "increase"))).scalars().all()
    assert len(txs) == 15 and sorted(t.new_quantity for t in txs) == list(range(1, 16))


# ---------------------------------------------------------------- variants
async def test_variants(client):
    h = await register(client)
    p = await make(client, h, track_inventory=False)
    url = f"{API}/products/{p['id']}/variants"
    ids = {}
    for size in ("S", "M", "L"):
        for colour in ("Black", "White"):
            r = await client.post(url, json={"options": {"Size": size, "Colour": colour}, "sku": f"TS-{size}-{colour[0]}",
                                             "track_inventory": True, "stock_quantity": 4}, headers=h)
            assert r.status_code == 201, r.text
            ids[(size, colour)] = r.json()["id"]
    assert len((await client.get(url, headers=h)).json()["items"]) == 6
    dup = await client.post(url, json={"options": {"colour": "black", "size": "s"}}, headers=h)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "duplicate_variant"
    assert (await client.post(url, json={"options": {"Size": "XL"}, "sku": "TS-1"}, headers=h)).status_code == 409  # SKU used
    v = ids[("M", "Black")]
    r = await client.patch(f"{API}/variants/{v}", json={"regular_price": "549"}, headers=h)
    assert r.json()["effective_price"] == "549.00" and r.json()["inherits_price"] is False
    assert (await client.patch(f"{API}/variants/{v}", json={"sale_price": "600"}, headers=h)).status_code == 422
    r = await client.post(f"{API}/inventory/adjust", json={"product_id": p["id"], "variant_id": v, "operation": "decrease",
                                                          "quantity": 4}, headers=h)
    assert r.json()["stock_status"] == "out_of_stock" and r.json()["product_stock_status"] == "low_stock"
    assert (await client.delete(f"{API}/variants/{v}", headers=h)).status_code == 204
    assert len((await client.get(url, headers=h)).json()["items"]) == 5
    # archived combination can be re-created; history preserved
    assert (await client.post(url, json={"options": {"Size": "M", "Colour": "Black"}}, headers=h)).status_code == 201
    hist = (await client.get(f"{API}/products/{p['id']}/price-history", headers=h)).json()["items"]
    assert any(x["variant_id"] == v for x in hist)
    allv = (await client.get(f"{API}/variants?q=TS-L", headers=h)).json()
    assert allv["total"] == 2 and allv["items"][0]["product_name"] == "Cotton T-shirt"


async def test_rescan_keeps_merchant_variants(client, site):
    from app.tests.test_e2e import connect_and_scan

    h = await register(client)
    website, _ = await connect_and_scan(client, h)
    motor = next(p for p in (await client.get(f"{API}/products?page_size=200", headers=h)).json()["items"] if p["name"] == "12V DC Motor")
    r = await client.post(f"{API}/products/{motor['id']}/variants", json={"options": {"Voltage": "24V"}, "sku": "MTR-24"}, headers=h)
    assert r.status_code == 201
    site.overrides["/products/dc-motor-12v"] = site.body("/products/dc-motor-12v").replace('"price":"450"', '"price":"475"').replace("Rs. 450", "Rs. 475")
    r = await client.post(f"{API}/scans", json={"website_id": website["id"]}, headers=h)
    await wait_for_inline_tasks()
    await client.post(f"{API}/scans/{r.json()['id']}/apply", json={}, headers=h)
    after = (await client.get(f"{API}/products/{motor['id']}", headers=h)).json()
    assert [v["sku"] for v in after["variants"]] == ["MTR-24"]
    assert after["pricing"]["regular_price"] == "475.00"
    hist = (await client.get(f"{API}/products/{motor['id']}/price-history", headers=h)).json()["items"]
    assert hist[0]["change_type"] == "website_sync" and hist[0]["old_amount"] == "450.00"


# ---------------------------------------------------------------- categories & attributes
async def test_categories(client):
    h = await register(client)
    mk = lambda **b: client.post(f"{API}/categories", json=b, headers=h)  # noqa: E731
    elec = (await mk(name="Electrical")).json()
    motors = (await mk(name="Motors", parent_id=elec["id"])).json()
    assert motors["path"] == "Electrical / Motors"
    assert (await mk(name="motors", parent_id=elec["id"])).status_code == 409
    assert (await mk(name="A/B")).status_code == 422
    p = await make(client, h, category_id=motors["id"])
    assert p["category"] == "Electrical / Motors"
    # filter by parent includes subcategories
    assert (await client.get(f"{API}/products?category_id={elec['id']}", headers=h)).json()["total"] == 1
    # cycle prevention
    r = await client.patch(f"{API}/categories/{elec['id']}", json={"parent_id": motors["id"]}, headers=h)
    assert r.status_code == 422
    # rename relabels products
    await client.patch(f"{API}/categories/{elec['id']}", json={"name": "Electricals"}, headers=h)
    assert (await client.get(f"{API}/products/{p['id']}", headers=h)).json()["category"] == "Electricals / Motors"
    assert (await client.delete(f"{API}/categories/{motors['id']}", headers=h)).status_code == 409  # in use
    tools = (await mk(name="Tools")).json()
    r = await client.post(f"{API}/bulk-operations", json={"operation": "assign_category", "product_ids": [p["id"]],
                                                         "params": {"category_id": tools["id"]}}, headers=h)
    assert r.json()["operation"]["succeeded"] == 1
    assert (await client.delete(f"{API}/categories/{motors['id']}", headers=h)).status_code == 204
    cats = (await client.get(f"{API}/categories", headers=h)).json()["items"]
    assert {c["path"]: c["product_count"] for c in cats} == {"Electricals": 0, "Tools": 1}


async def test_attribute_definitions_validation_and_aliases(client):
    h = await register(client)
    cat = (await client.post(f"{API}/categories", json={"name": "Electronics"}, headers=h)).json()
    r = await client.post(f"{API}/attribute-definitions", json={"name": "RAM", "data_type": "number", "unit": "GB",
                                                                "is_required": True, "category_id": cat["id"],
                                                                "aliases": ["memory", "Memory Size"]}, headers=h)
    assert r.status_code == 201 and r.json()["aliases"] == ["memory", "memory_size"]
    assert (await client.post(f"{API}/attribute-definitions", json={"name": "Fit", "data_type": "enum"}, headers=h)).status_code == 422
    phone = await make(client, h, name="Phone X", sku="PX", category_id=cat["id"])
    issues = (await client.get(f"{API}/products/{phone['id']}", headers=h)).json()["issues"]
    assert any(i["code"] == "missing_required_attribute" for i in issues)
    r = await client.patch(f"{API}/products/{phone['id']}", json={"attributes": [{"label": "Memory", "value": "lots"}]}, headers=h)
    assert r.json()["attributes"][0]["key"] == "ram"  # merchant alias -> canonical key; label kept
    assert r.json()["attributes"][0]["label"] == "Memory"
    issues = (await client.get(f"{API}/products/{phone['id']}", headers=h)).json()["issues"]
    assert any(i["code"] == "attribute_type_mismatch" and i["status"] == "open" for i in issues)
    await client.patch(f"{API}/products/{phone['id']}", json={"attributes": [{"label": "Memory", "value": "8"}]}, headers=h)
    issues = (await client.get(f"{API}/products/{phone['id']}", headers=h)).json()["issues"]
    assert not any(i["code"] in ("attribute_type_mismatch", "missing_required_attribute") and i["status"] == "open" for i in issues)
    # products outside the category are not required to have RAM
    shirt = await make(client, h, name="Shirt", sku="SH")
    assert not any(i["code"] == "missing_required_attribute"
                   for i in (await client.get(f"{API}/products/{shirt['id']}", headers=h)).json()["issues"])


def test_builtin_attribute_aliases():
    from app.schemas.canonical import ExtractedProduct
    from app.services.normalization.product import finalize

    p = ExtractedProduct.model_validate({"name": "Drill", "attributes": [
        {"key": "product_weight", "label": "Product weight", "value": "2", "unit": "kg"},
        {"key": "brand_name", "label": "Brand name", "value": "Bosch"}]})
    out = finalize(p)
    assert [a.key for a in out.attributes] == ["weight", "brand"] and out.attributes[0].label == "Product weight"
    assert out.brand == "Bosch"


# ---------------------------------------------------------------- bulk
async def test_bulk_price_preview_partial_failure_and_history(client):
    h = await register(client)
    a = await make(client, h, name="A", sku="A", regular_price="100", currency="INR")
    b = await make(client, h, name="B", sku="B", regular_price="50", currency="USD")
    c = await make(client, h, name="C", sku="C", regular_price=None, currency=None)
    ids = [a["id"], b["id"], c["id"]]
    body = {"operation": "price_adjust", "product_ids": ids, "params": {"mode": "fixed_increase", "value": "10", "currency": "INR"}}
    prev = (await client.post(f"{API}/pricing/bulk", json={**body, "dry_run": True}, headers=h)).json()["preview"]
    assert prev["selected"] == 3 and prev["valid"] == 1 and prev["invalid"] == 2
    items = {i["name"]: i for i in prev["items"]}
    assert items["A"]["before"]["regular_price"] == "100.00" and items["A"]["after"]["regular_price"] == "110.00"
    assert "Currency mismatch" in items["B"]["error"] and "No regular price" in items["C"]["error"]
    # preview wrote nothing
    assert (await client.get(f"{API}/products/{a['id']}", headers=h)).json()["pricing"]["regular_price"] == "100.00"
    op = (await client.post(f"{API}/pricing/bulk", json={**body, "dry_run": False}, headers=h)).json()["operation"]
    assert op["status"] == "completed_with_errors" and (op["succeeded"], op["failed"]) == (1, 2)  # never "complete success"
    failed = {i["name"]: i["error"] for i in op["items"] if i["status"] == "failed"}
    assert set(failed) == {"B", "C"}
    hist = (await client.get(f"{API}/products/{a['id']}/price-history", headers=h)).json()["items"]
    assert hist[0]["change_type"] == "bulk" and hist[0]["bulk_operation_id"] == op["id"]
    # percent works across currencies (each in its own currency)
    op = (await client.post(f"{API}/bulk-operations", json={"operation": "price_adjust", "product_ids": [a["id"], b["id"]],
                                                            "params": {"mode": "percent_decrease", "value": "10"}}, headers=h)).json()["operation"]
    assert op["status"] == "completed"
    assert (await client.get(f"{API}/products/{b['id']}", headers=h)).json()["pricing"]["regular_price"] == "45.00"
    assert (await client.post(f"{API}/bulk-operations", json={"operation": "price_adjust", "product_ids": ids,
                                                              "params": {"mode": "percent_decrease", "value": "100"}},
                              headers=h)).status_code == 422


async def test_bulk_operations_misc_and_background(client, monkeypatch):
    from app.services.commerce import bulk

    h = await register(client)
    ps = [await make(client, h, name=f"Product {i}", sku=f"P{i}") for i in range(4)]
    ids = [p["id"] for p in ps]
    run = lambda op, params=None, sel=ids: client.post(f"{API}/bulk-operations", json={"operation": op, "product_ids": sel,  # noqa: E731
                                                                                       "params": params or {}}, headers=h)
    assert (await run("set_brand", {"brand": "Acme"})).json()["operation"]["succeeded"] == 4
    assert (await run("add_attribute", {"label": "Warranty", "value": "1 year"})).json()["operation"]["succeeded"] == 4
    assert (await run("remove_attribute", {"key": "warranty"}, ids[:2])).json()["operation"]["succeeded"] == 2
    r = (await run("inventory_adjust", {"operation": "set", "quantity": 3})).json()["operation"]
    assert r["failed"] == 4 and "tracking is disabled" in r["items"][0]["error"]
    assert (await run("inventory_settings", {"track_inventory": True})).json()["operation"]["succeeded"] == 4
    assert (await run("inventory_adjust", {"operation": "increase", "quantity": 7})).json()["operation"]["succeeded"] == 4
    assert (await run("approve")).json()["operation"]["succeeded"] == 4
    assert (await run("archive", sel=ids[:1])).json()["operation"]["succeeded"] == 1
    assert (await run("publish", sel=ids[:1])).json()["operation"]["failed"] == 1  # archived cannot be published
    # large operations run in the background with real progress
    monkeypatch.setattr(bulk, "BACKGROUND_THRESHOLD", 2)
    op = (await run("set_product_type", {"product_type": "physical"})).json()["operation"]
    assert op["run_mode"] == "background" and op["status"] == "queued"
    await wait_for_inline_tasks()
    done = (await client.get(f"{API}/bulk-operations/{op['id']}", headers=h)).json()
    assert done["status"] == "completed" and done["processed"] == done["total"] == 4
    lst = (await client.get(f"{API}/bulk-operations", headers=h)).json()
    assert lst["total"] >= 9


# ---------------------------------------------------------------- scheduling & sync
def test_next_run_is_timezone_and_dst_aware():
    from app.models import SyncSchedule
    from app.services.commerce.sync import next_run

    s = SyncSchedule(frequency="daily", enabled=True, timezone="America/New_York", hour=3, minute=0, weekday=0, day_of_month=1)
    # 2026-03-08 is the US DST change; 03:00 local is 07:00 UTC after it and 08:00 UTC before
    assert next_run(s, datetime(2026, 3, 7, 12, tzinfo=timezone.utc)) == datetime(2026, 3, 8, 7, tzinfo=timezone.utc)
    assert next_run(s, datetime(2026, 3, 1, 9, tzinfo=timezone.utc)) == datetime(2026, 3, 2, 8, tzinfo=timezone.utc)
    s.frequency, s.weekday, s.timezone = "weekly", 4, "Asia/Kolkata"  # Fridays 03:00 IST = Thursday 21:30 UTC
    assert next_run(s, datetime(2026, 11, 8, tzinfo=timezone.utc)) == datetime(2026, 11, 12, 21, 30, tzinfo=timezone.utc)
    s.frequency, s.day_of_month = "monthly", 28
    assert next_run(s, datetime(2026, 12, 28, 0, tzinfo=timezone.utc)).date().isoformat() == "2027-01-27"
    s.enabled = False
    assert next_run(s, datetime(2026, 1, 1, tzinfo=timezone.utc)) is None


async def test_scheduled_rescan_runs_once(client, site, db):
    from app.services.commerce.sync import dispatch_due_scans
    from app.tests.test_e2e import connect_and_scan

    h = await register(client)
    website, _ = await connect_and_scan(client, h)
    r = await client.post(f"{API}/sync/schedules", json={"website_id": website["id"], "frequency": "daily", "enabled": True,
                                                         "timezone": "Asia/Kolkata", "hour": 2, "conflict_policy": "review"}, headers=h)
    assert r.status_code == 201 and r.json()["next_run_at"]
    assert (await client.post(f"{API}/sync/schedules", json={"website_id": website["id"], "timezone": "Mars/Base"},
                              headers=h)).status_code == 422
    due_at = datetime.fromisoformat(r.json()["next_run_at"]) + timedelta(seconds=1)
    first = await dispatch_due_scans(db, at=due_at)
    second = await dispatch_due_scans(db, at=due_at)  # duplicate scheduler tick: nothing new
    assert first == {"started": 1, "skipped": 0} and second == {"started": 0, "skipped": 0}
    await wait_for_inline_tasks()
    syncs = (await client.get(f"{API}/sync", headers=h)).json()["items"]
    assert [s["trigger"] for s in syncs][:2] == ["scheduled", "manual"]
    # the fixture site has a deliberately broken page, so the sync honestly reports errors
    assert syncs[0]["status"] in ("completed", "completed_with_errors") and syncs[0]["products_discovered"] > 0
    assert (syncs[0]["status"] == "completed_with_errors") == (syncs[0]["failed_operations"] > 0)
    sched = (await db.execute(select(SyncSchedule))).scalars().one()
    await db.refresh(sched)
    assert sched.next_run_at > due_at.replace(tzinfo=None) if sched.next_run_at.tzinfo is None else sched.next_run_at > due_at


async def test_conflict_detection_and_resolution(client, site):
    from app.tests.test_e2e import connect_and_scan

    h = await register(client)
    website, _ = await connect_and_scan(client, h)
    items = (await client.get(f"{API}/products?page_size=200", headers=h)).json()["items"]
    motor = next(p for p in items if p["name"] == "12V DC Motor")
    # merchant manages price + description in AgentReady
    await client.put(f"{API}/products/{motor['id']}/pricing", json={"regular_price": "460"}, headers=h)
    await client.patch(f"{API}/products/{motor['id']}", json={"description": "Merchant copy"}, headers=h)
    # website changes both
    page = site.body("/products/dc-motor-12v").replace('"price":"450"', '"price":"475"').replace("Rs. 450", "Rs. 475")
    site.overrides["/products/dc-motor-12v"] = page.replace("Compact 12V DC motor for robotics", "New website copy for robotics")
    r = await client.post(f"{API}/sync", json={"website_id": website["id"]}, headers=h)
    assert r.status_code == 202
    await wait_for_inline_tasks()
    sync = (await client.get(f"{API}/sync/{r.json()['id']}", headers=h)).json()
    assert sync["status"] == "awaiting_review" and sync["conflicts_detected"] == 2
    conflicts = {c["field"]: c for c in sync["conflicts"]}
    price = conflicts["price_amount"]
    assert (price["website_value"], price["agentready_value"], price["original_value"]) == ("475.00", "460.00", "450.00")
    assert price["suggested_action"] == "keep_agentready" and price["evidence"]
    assert conflicts["description"]["suggested_action"] == "accept_website"
    # applying the scan never overwrites merchant-managed values
    scan_id = sync["scan_id"]
    await client.post(f"{API}/scans/{scan_id}/apply", json={}, headers=h)
    p = (await client.get(f"{API}/products/{motor['id']}", headers=h)).json()
    assert p["pricing"]["regular_price"] == "460.00" and p["description"] == "Merchant copy" and p["open_conflicts"] == 2
    # resolve: keep our price, accept the website description
    r = await client.post(f"{API}/sync/{sync['id']}/resolve", json={"conflict_ids": [price["id"]], "action": "keep_agentready"}, headers=h)
    assert r.json()["resolved"] == 1
    r = await client.post(f"{API}/sync/conflicts/{conflicts['description']['id']}/resolve", json={"action": "accept_website"}, headers=h)
    assert r.json()["resolution"] == "accept_website"
    p = (await client.get(f"{API}/products/{motor['id']}", headers=h)).json()
    assert p["pricing"]["regular_price"] == "460.00" and p["description"].startswith("New website copy")
    assert "description" not in p["manual_overrides"] and "price" in p["manual_overrides"]
    assert (await client.post(f"{API}/sync/conflicts/{price['id']}/resolve", json={"action": "ignore"}, headers=h)).status_code == 409
    # a further rescan with the same website values raises no new conflict
    r = await client.post(f"{API}/sync", json={"website_id": website["id"]}, headers=h)
    await wait_for_inline_tasks()
    assert (await client.get(f"{API}/sync/{r.json()['id']}", headers=h)).json()["conflicts_detected"] == 0
    # edit resolution with a custom value
    site.overrides["/products/dc-motor-12v"] = site.overrides["/products/dc-motor-12v"].replace('"price":"475"', '"price":"480"').replace("Rs. 475", "Rs. 480")
    r = await client.post(f"{API}/sync", json={"website_id": website["id"]}, headers=h)
    await wait_for_inline_tasks()
    c = (await client.get(f"{API}/sync/conflicts", headers=h)).json()["items"][0]
    r = await client.post(f"{API}/sync/conflicts/{c['id']}/resolve", json={"action": "edit", "value": "470"}, headers=h)
    assert r.status_code == 200 and r.json()["resolution"] == "edited"
    assert (await client.get(f"{API}/products/{motor['id']}", headers=h)).json()["pricing"]["regular_price"] == "470.00"
    hist = (await client.get(f"{API}/products/{motor['id']}/price-history", headers=h)).json()["items"]
    assert hist[0]["change_type"] == "conflict_resolution"
    audit = (await client.get(f"{API}/audit-logs?action=sync.conflict", headers=h)).json()["items"]
    assert {a["action"] for a in audit} == {"sync.conflict_detected", "sync.conflict_resolved"}


async def test_conflict_policy_keep_agentready_and_detection_switches(client, site):
    from app.tests.test_e2e import connect_and_scan

    h = await register(client)
    website, _ = await connect_and_scan(client, h)
    motor = next(p for p in (await client.get(f"{API}/products?page_size=200", headers=h)).json()["items"] if p["name"] == "12V DC Motor")
    await client.put(f"{API}/products/{motor['id']}/pricing", json={"regular_price": "460"}, headers=h)
    await client.post(f"{API}/sync/schedules", json={"website_id": website["id"], "conflict_policy": "keep_agentready",
                                                     "detect": {"new_products": False}}, headers=h)
    site.overrides["/products/dc-motor-12v"] = site.body("/products/dc-motor-12v").replace('"price":"450"', '"price":"475"').replace("Rs. 450", "Rs. 475")
    site.overrides["/price-list"] = site.body("/price-list").replace(
        "<tr><td>Motor B</td>", "<tr><td>Motor C</td><td>36V</td><td>6000</td><td>990</td><td>DC Motors</td></tr><tr><td>Motor B</td>")
    r = await client.post(f"{API}/sync", json={"website_id": website["id"]}, headers=h)
    await wait_for_inline_tasks()
    sync = (await client.get(f"{API}/sync/{r.json()['id']}", headers=h)).json()
    assert sync["summary"]["new"] == 0 and sync["summary"]["ignored_changes"] >= 1  # new-product detection off
    assert [c["status"] for c in sync["conflicts"]] == ["resolved"] and sync["conflicts"][0]["resolution"] == "keep_agentready"


# ---------------------------------------------------------------- catalog API v2 + keys
async def test_catalog_v2_scopes_feeds_and_keys(client):
    h = await register(client)
    pub = await make(client, h, name="Published", sku="PUB", cost_price="10", track_inventory=True, stock_quantity=9)
    draft = await make(client, h, name="Draft", sku="DRF", lifecycle_status="draft")
    arch = await make(client, h, name="Archived", sku="ARC")
    await approve(client, h, pub["id"])
    await approve(client, h, draft["id"])  # approved but draft -> not public
    await approve(client, h, arch["id"])
    t0 = datetime.now(timezone.utc).isoformat()
    await client.post(f"{API}/products/{arch['id']}/archive", headers=h)
    full = (await client.post(f"{API}/api-keys", json={"name": "Agent", "scopes": ["catalog:read", "pricing:read", "inventory:read"]},
                              headers=h)).json()
    basic = (await client.post(f"{API}/api-keys", json={"name": "Basic", "scopes": ["catalog:read"], "rate_limit_per_minute": 3},
                               headers=h)).json()
    assert full["key"].startswith("ar_cat_") and "key" not in (await client.get(f"{API}/api-keys", headers=h)).json()["items"][0]
    K = lambda k: {"X-Catalog-Key": k}  # noqa: E731
    r = (await client.get("/api/v2/catalog/products", headers=K(full["key"]))).json()
    assert r["schema"] == "agentready-catalog/2.0" and [i["title"] for i in r["items"]] == ["Published"]
    item = r["items"][0]
    assert item["price"]["effective"] == "499.00" and item["available_quantity"] == 9
    assert "cost" not in json.dumps(item) and "reserved" not in json.dumps(item) and "location" not in json.dumps(item)
    b = (await client.get("/api/v2/catalog/products", headers=K(basic["key"]))).json()["items"][0]
    assert "price" not in b and "available_quantity" not in b
    assert (await client.get("/api/v2/catalog/pricing", headers=K(basic["key"]))).status_code == 403
    assert (await client.get(f"/api/v2/catalog/products/{draft['id']}", headers=K(full["key"]))).status_code == 404
    # rate limit per key
    codes = [(await client.get("/api/v2/catalog/categories", headers=K(basic["key"]))).status_code for _ in range(3)]
    assert codes[-1] == 429
    # incremental feed with tombstones
    inc = (await client.get(f"/api/v2/catalog/feed?type=incremental&updated_since={t0.replace('+', '%2B')}", headers=K(full["key"]))).json()
    assert [x["id"] for x in inc["removed"]] == [arch["id"]] and inc["removed"][0]["reason"] == "archived"
    assert inc["next_updated_since"]
    avail = (await client.get("/api/v2/catalog/feed?type=availability", headers=K(full["key"]))).json()
    assert avail["items"][0]["stock_status"] == "in_stock"
    # usage tracking, rotation, revocation
    keys = {k["name"]: k for k in (await client.get(f"{API}/api-keys", headers=h)).json()["items"]}
    assert keys["Agent"]["usage_count"] >= 3 and keys["Agent"]["last_used_at"] and keys["Agent"]["daily_usage"][0]["requests"] >= 3
    rot = (await client.post(f"{API}/api-keys/{full['id']}/rotate", headers=h)).json()
    assert (await client.get("/api/v2/catalog/products", headers=K(full["key"]))).status_code == 401
    assert (await client.get("/api/v2/catalog/products", headers=K(rot["key"]))).status_code == 200
    await client.post(f"{API}/api-keys/{rot['id']}/revoke", headers=h)
    assert (await client.get("/api/v1/catalog", headers=K(rot["key"]))).status_code == 401
    audit = {a["action"] for a in (await client.get(f"{API}/audit-logs?action=api_key", headers=h)).json()["items"]}
    assert audit == {"api_key.created", "api_key.rotated", "api_key.revoked"}
    assert not any(full["key"] in json.dumps(a) or rot["key"] in json.dumps(a)
                   for a in (await client.get(f"{API}/audit-logs?page_size=200", headers=h)).json()["items"])


async def test_phase2_tenant_isolation(client):
    a = await register(client, "a@example.com", "Shop A")
    b = await register(client, "b@example.com", "Shop B")
    p = await make(client, a, track_inventory=True, stock_quantity=3)
    cat = (await client.post(f"{API}/categories", json={"name": "Mine"}, headers=a)).json()
    v = (await client.post(f"{API}/products/{p['id']}/variants", json={"options": {"Size": "M"}}, headers=a)).json()
    key = (await client.post(f"{API}/api-keys", json={"name": "k"}, headers=a)).json()
    checks = [
        ("get", f"/products/{p['id']}/pricing", None), ("put", f"/products/{p['id']}/pricing", {"regular_price": "1"}),
        ("get", f"/products/{p['id']}/price-history", None), ("get", f"/products/{p['id']}/inventory", None),
        ("post", "/inventory/adjust", {"product_id": p["id"], "operation": "increase", "quantity": 1}),
        ("patch", f"/variants/{v['id']}", {"name": "x"}), ("delete", f"/variants/{v['id']}", None),
        ("patch", f"/categories/{cat['id']}", {"name": "x"}), ("delete", f"/categories/{cat['id']}", None),
        ("post", f"/api-keys/{key['id']}/revoke", None), ("get", f"/products/{p['id']}/history", None),
        ("post", "/pricing/schedules", {"product_id": p["id"], "new_amount": "1", "effective_at": "2030-01-01T00:00:00+00:00"}),
    ]
    for method, path, body in checks:
        r = await getattr(client, method)(API + path, headers=b, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (method, path, r.status_code)
    prev = (await client.post(f"{API}/bulk-operations", json={"operation": "set_brand", "product_ids": [p["id"]], "params": {"brand": "x"},
                                                              "dry_run": True}, headers=b)).json()["preview"]
    assert prev["valid"] == 0 and prev["items"][0]["error"] == "Product not found"
    op = (await client.post(f"{API}/bulk-operations", json={"operation": "set_brand", "product_ids": [p["id"]], "params": {"brand": "x"}},
                            headers=b)).json()["operation"]
    assert op["failed"] == 1
    for path in ("/variants", "/inventory/transactions", "/audit-logs", "/sync", "/sync/conflicts", "/pricing/history",
                 "/bulk-operations", "/api-keys", "/categories"):
        body = (await client.get(API + path, headers=b)).json()
        assert all(i.get("product_id") != p["id"] and i.get("entity_id") != p["id"] for i in body["items"]), path
        if path not in ("/bulk-operations", "/audit-logs"):  # B's own (failed) bulk operation and its audit entries
            assert body.get("total", len(body["items"])) == 0, path
    assert (await client.get(f"{API}/products/{p['id']}", headers=a)).json()["brand"] is None
    assert (await client.get(f"/api/v2/catalog/products/{p['id']}", headers={"X-Catalog-Key": key["key"]})).status_code == 404


async def test_dashboard_commerce_metrics_are_real(client):
    h = await register(client)
    a = await make(client, h, name="Alpha drill", sku="A", track_inventory=True, stock_quantity=1)
    await make(client, h, name="Beta drill", sku="B", lifecycle_status="draft")
    await approve(client, h, a["id"])
    dash = (await client.get(f"{API}/dashboard/overview", headers=h)).json()["commerce"]
    assert (dash["active"], dash["draft"], dash["published"], dash["low_stock"]) == (1, 1, 1, 1)
    charts = (await client.get(f"{API}/dashboard/charts?days=7", headers=h)).json()
    assert charts["catalog_growth"][-1]["products"] == 2 and len(charts["catalog_growth"]) == 7
    assert sum(d["manual"] for d in charts["price_changes"]) == 2  # two product_created price records
    act = (await client.get(f"{API}/dashboard/activity", headers=h)).json()["items"]
    assert act[0]["action"] == "product.approved"
    assert not any(k in json.dumps(charts) for k in ("revenue", "orders", "conversion"))
