"""Commerce integrations against MOCKED provider APIs (httpx.MockTransport).

These tests verify AgentReady's side of the documented WooCommerce REST v3 / Shopify Admin GraphQL
contracts (auth headers, pagination, payload mapping, webhooks, error handling). They are NOT live
provider verification — no real store is contacted.
"""

import base64
import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import select

from app.models import AuditLog, CommerceIntegration
from app.services.crawler import fetcher as fetcher_mod
from app.services.integrations import http as http_mod
from app.tests.conftest import register
from app.tests.sitekit import public_resolver
from app.workers.dispatch import wait_for_inline_tasks

API = "/api/v1"
CK, CS = "ck_" + "a" * 40, "cs_" + "b" * 40


class FakeWoo:
    def __init__(self):
        self.products = [
            {"id": 11, "name": "Brass Valve", "type": "simple", "status": "publish", "sku": "BV-1", "regular_price": "250.00",
             "sale_price": "", "manage_stock": True, "stock_quantity": 12, "stock_status": "instock",
             "description": "<p>Solid brass</p>", "permalink": "https://store.example/p/valve",
             "categories": [{"id": 1, "name": "Plumbing"}], "images": [{"src": "https://store.example/v.jpg", "alt": ""}],
             "attributes": [{"name": "Material", "options": ["Brass"], "variation": False}], "variations": []},
            {"id": 12, "name": "Work Shirt", "type": "variable", "status": "publish", "sku": "WS", "regular_price": "",
             "sale_price": "", "manage_stock": False, "stock_quantity": None, "stock_status": "instock",
             "description": "", "permalink": "https://store.example/p/shirt", "categories": [], "images": [],
             "attributes": [{"name": "Size", "options": ["M", "L"], "variation": True}], "variations": [21, 22]},
        ]
        self.variations = {12: [
            {"id": 21, "sku": "WS-M", "regular_price": "300", "sale_price": "", "manage_stock": True, "stock_quantity": 5,
             "attributes": [{"name": "Size", "option": "M"}], "image": None},
            {"id": 22, "sku": "WS-L", "regular_price": "320", "sale_price": "", "manage_stock": True, "stock_quantity": 0,
             "attributes": [{"name": "Size", "option": "L"}], "image": None}]}
        self.puts: list[tuple[str, dict]] = []
        self.calls = 0
        self.fail_next = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert req.headers["host"] == "store.example"
        auth = req.headers.get("authorization", "")
        if auth != "Basic " + base64.b64encode(f"{CK}:{CS}".encode()).decode():
            return httpx.Response(401, json={"code": "woocommerce_rest_cannot_view", "message": "Sorry"})
        if self.fail_next:
            self.fail_next -= 1
            return httpx.Response(503, json={})
        path = req.url.path.removeprefix("/wp-json/wc/v3")
        if path == "/data/currencies/current":
            return httpx.Response(200, json={"code": "INR", "name": "Indian rupee", "symbol": "₹"})
        if path == "/products" and req.method == "GET":
            page = int(req.url.params.get("page", 1))
            per = int(req.url.params.get("per_page", 10))
            chunk = self.products[(page - 1) * per: page * per] if per > 1 else self.products[:1]
            return httpx.Response(200, json=chunk, headers={"X-WP-TotalPages": "1", "X-WP-Total": str(len(self.products))})
        if path.startswith("/products/12/variations") and req.method == "GET":
            return httpx.Response(200, json=self.variations[12], headers={"X-WP-TotalPages": "1"})
        if req.method == "PUT":
            self.puts.append((path, json.loads(req.content)))
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(404, json={"message": "no route"})


@pytest.fixture
def woo(monkeypatch, settings):
    fake = FakeWoo()
    monkeypatch.setattr(http_mod, "TRANSPORT_OVERRIDE", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(fetcher_mod, "RESOLVER_OVERRIDE", public_resolver)
    settings.integration_retry_base_seconds = 0.0
    return fake


async def connect(client, h, **kw):
    r = await client.post(f"{API}/integrations/woocommerce/connect",
                          json={"store_url": "https://store.example", "consumer_key": CK, "consumer_secret": CS, **kw}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def test_woocommerce_connect_import_conflict_push_webhook(client, woo, db):
    h = await register(client)
    i = await connect(client, h)
    assert i["verification"]["ok"] and i["status"] == "connected" and i["currency"] == "INR"
    # credentials: encrypted at rest, never returned, never logged
    row = (await db.execute(select(CommerceIntegration))).scalars().one()
    assert CK not in (row.credentials_encrypted or "") and CS not in (row.credentials_encrypted or "")
    listing = (await client.get(f"{API}/integrations", headers=h)).text
    assert CK not in listing and CS not in listing and i["credentials_hint"] == "ck_…aaaa"
    assert not any(CK in json.dumps(a.changes) or CS in json.dumps(a.changes) or CK in a.summary
                   for a in (await db.execute(select(AuditLog))).scalars())

    # import (background job)
    r = await client.post(f"{API}/integrations/{i['id']}/sync", json={"direction": "import"}, headers=h)
    assert r.status_code == 202
    await wait_for_inline_tasks()
    job = (await client.get(f"{API}/sync/{r.json()['id']}", headers=h)).json()
    assert job["status"] == "completed", job["errors"]
    assert job["products_created"] == 2 and job["direction"] == "import"
    prods = {p["name"]: p for p in (await client.get(f"{API}/products?page_size=50", headers=h)).json()["items"]}
    valve, shirt = prods["Brass Valve"], prods["Work Shirt"]
    assert valve["origin"] == "woocommerce" and valve["pricing"]["regular_price"] == "250.00" and valve["pricing"]["currency"] == "INR"
    assert valve["inventory"]["on_hand"] == 12 and valve["inventory"]["track_inventory"]
    assert valve["approval_status"] == "needs_review" and valve["publication_status"] == "unpublished"
    assert {v["sku"]: v["inventory"]["on_hand"] for v in shirt["variants"]} == {"WS-M": 5, "WS-L": 0}
    txs = (await client.get(f"{API}/inventory/transactions?product_id={valve['id']}", headers=h)).json()["items"]
    assert txs[0]["transaction_type"] == "integration_import"

    # re-import with no store changes: nothing changes
    r = await client.post(f"{API}/integrations/{i['id']}/sync", json={}, headers=h)
    await wait_for_inline_tasks()
    job = (await client.get(f"{API}/sync/{r.json()['id']}", headers=h)).json()
    assert job["products_created"] == 0 and job["products_changed"] == 0 and job["products_ignored"] == 2

    # merchant manages the valve price in AgentReady; the store changes it -> conflict, not an overwrite
    await client.put(f"{API}/products/{valve['id']}/pricing", json={"regular_price": "240"}, headers=h)
    woo.products[0]["regular_price"] = "275.00"
    woo.products[0]["stock_quantity"] = 9
    r = await client.post(f"{API}/integrations/{i['id']}/sync", json={}, headers=h)
    await wait_for_inline_tasks()
    job = (await client.get(f"{API}/sync/{r.json()['id']}", headers=h)).json()
    assert job["conflicts_detected"] == 1 and job["conflicts"][0]["field"] == "price_amount"
    assert job["conflicts"][0]["website_value"] == "275.00" and job["conflicts"][0]["source_type"] == "woocommerce"
    v = (await client.get(f"{API}/products/{valve['id']}", headers=h)).json()
    assert v["pricing"]["regular_price"] == "240.00" and v["inventory"]["on_hand"] == 9  # stock: store is master

    # push AgentReady price/stock to the store (records direction + results)
    r = await client.post(f"{API}/integrations/{i['id']}/sync", json={"direction": "export"}, headers=h)
    assert r.json()["direction"] == "export" and r.json()["status"] == "completed"
    assert ("/products/11", {"regular_price": "240.00", "sale_price": ""}) in woo.puts  # AgentReady-managed price
    assert ("/products/11", {"manage_stock": True, "stock_quantity": 9}) in woo.puts
    assert ("/products/12/variations/21", {"regular_price": "300.00", "sale_price": ""}) in woo.puts

    # retries on 503, then succeeds
    woo.fail_next = 1
    v2 = await client.post(f"{API}/integrations/{i['id']}/verify", headers=h)
    assert v2.json()["verification"]["ok"]

    # webhooks: valid signature applies; bad signature rejected; duplicate delivery ignored; echo produces no change
    secret = i["webhook_secret"]
    body = json.dumps({**woo.products[1], "id": 12, "name": "Work Shirt (2026)"}).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    hdr = {"X-WC-Webhook-Signature": sig, "X-WC-Webhook-Topic": "product.updated", "X-WC-Webhook-Delivery-ID": "d-1",
           "Content-Type": "application/json"}
    url = f"{API}/integrations/{i['id']}/webhooks"
    assert (await client.post(url, content=body, headers={**hdr, "X-WC-Webhook-Signature": "bad"})).status_code == 401
    r = await client.post(url, content=body, headers=hdr)
    assert r.status_code == 200 and r.json()["detail"] == "updated"
    assert (await client.get(f"{API}/products/{shirt['id']}", headers=h)).json()["name"] == "Work Shirt (2026)"
    r = await client.post(url, content=body, headers=hdr)
    assert r.json()["detail"] == "duplicate delivery ignored"
    r = await client.post(url, content=body, headers={**hdr, "X-WC-Webhook-Delivery-ID": "d-2"})
    assert r.json()["detail"] == "unchanged"  # same data again (e.g. an echo of our own push): no loop

    # disconnect erases credentials
    await client.delete(f"{API}/integrations/{i['id']}", headers=h)
    await db.refresh(row)
    assert row.credentials_encrypted is None and row.status == "disconnected"
    assert (await client.post(url, content=body, headers=hdr)).status_code == 404


async def test_woocommerce_errors_and_validation(client, woo):
    h = await register(client)
    r = await client.post(f"{API}/integrations/woocommerce/connect",
                          json={"store_url": "http://store.example", "consumer_key": CK, "consumer_secret": CS}, headers=h)
    assert r.status_code == 422  # https only
    r = await client.post(f"{API}/integrations/woocommerce/connect",
                          json={"store_url": "https://store.example", "consumer_key": "ck_" + "z" * 40, "consumer_secret": CS}, headers=h)
    assert r.status_code == 201 and r.json()["verification"]["ok"] is False and "rejected the API keys" in r.json()["verification"]["message"]
    r = await client.post(f"{API}/integrations/woocommerce/connect",
                          json={"store_url": "https://10.0.0.5", "consumer_key": CK, "consumer_secret": CS}, headers=h)
    assert r.status_code == 201 and "rejected" in r.json()["verification"]["message"].lower()  # SSRF guard on store URLs
    other = await register(client, "b@example.com", "B")
    ints = (await client.get(f"{API}/integrations", headers=h)).json()["items"]
    assert (await client.post(f"{API}/integrations/{ints[0]['id']}/sync", json={}, headers=other)).status_code == 404


# ---------------------------------------------------------------- Shopify
def test_shopify_oauth_hmac_and_shop_domain():
    from app.services.integrations.providers import valid_shop_domain, verify_oauth_hmac

    secret = "shpss_test"
    params = {"code": "abc", "shop": "demo.myshopify.com", "state": "xyz", "timestamp": "1700000000"}
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    params["hmac"] = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    assert verify_oauth_hmac(params, secret)
    assert not verify_oauth_hmac({**params, "shop": "evil.myshopify.com"}, secret)
    assert valid_shop_domain("demo-store.myshopify.com")
    assert not any(valid_shop_domain(s) for s in ("evil.com", "demo.myshopify.com.evil.com", "-x.myshopify.com", ""))


async def test_shopify_requires_app_credentials_for_oauth(client):
    h = await register(client)
    r = await client.post(f"{API}/integrations/shopify/connect", json={"shop": "demo.myshopify.com"}, headers=h)
    assert r.status_code == 503 and r.json()["error"]["code"] == "shopify_not_configured"
    assert (await client.post(f"{API}/integrations/shopify/connect", json={"shop": "demo.com"}, headers=h)).status_code == 422
    r = await client.get(f"{API}/integrations/shopify/callback?shop=demo.myshopify.com&code=x&state=y&hmac=bad")
    assert r.status_code == 400


async def test_shopify_token_import_mocked(client, monkeypatch, settings):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert req.headers["x-shopify-access-token"] == "shpat_" + "t" * 32
        assert req.url.path == f"/admin/api/{settings.shopify_api_version}/graphql.json"
        q = json.loads(req.content)["query"]
        if "products(" in q:
            return httpx.Response(200, json={"data": {"shop": {"name": "Demo", "currencyCode": "USD"}, "products": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "gid://shopify/Product/1", "title": "Mug", "descriptionHtml": "<b>Ceramic</b>", "vendor": "Acme",
                           "productType": "Kitchen", "status": "ACTIVE", "onlineStoreUrl": None, "media": {"nodes": []},
                           "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/9", "title": "Default Title", "sku": "MUG",
                                                   "price": "8.00", "compareAtPrice": "10.00", "inventoryQuantity": 4,
                                                   "selectedOptions": [{"name": "Title", "value": "Default Title"}],
                                                   "inventoryItem": {"id": "gid://shopify/InventoryItem/5", "tracked": True}}]}}]}}})
        return httpx.Response(200, json={"data": {"shop": {"name": "Demo", "currencyCode": "USD"}}})

    monkeypatch.setattr(http_mod, "TRANSPORT_OVERRIDE", httpx.MockTransport(handler))
    monkeypatch.setattr(fetcher_mod, "RESOLVER_OVERRIDE", public_resolver)
    h = await register(client)
    r = await client.post(f"{API}/integrations/shopify/connect",
                          json={"shop": "demo.myshopify.com", "access_token": "shpat_" + "t" * 32}, headers=h)
    assert r.status_code == 201 and r.json()["verification"]["ok"] and r.json()["currency"] == "USD"
    r = await client.post(f"{API}/integrations/{r.json()['id']}/sync", json={}, headers=h)
    await wait_for_inline_tasks()
    mug = (await client.get(f"{API}/products?q=mug", headers=h)).json()["items"][0]
    # compareAtPrice > price => regular 10.00, sale 8.00
    assert mug["pricing"]["regular_price"] == "10.00" and mug["pricing"]["sale_price"] == "8.00"
    assert mug["brand"] == "Acme" and mug["inventory"]["on_hand"] == 4 and mug["variants"] == []
