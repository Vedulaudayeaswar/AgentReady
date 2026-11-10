"""Commerce provider abstraction and the WooCommerce / Shopify implementations.

Only official, documented APIs are used:
* WooCommerce REST API v3 (``/wp-json/wc/v3``), HTTP Basic auth with a consumer key/secret over HTTPS,
  webhooks signed with ``X-WC-Webhook-Signature`` = base64(HMAC-SHA256(secret, raw body)).
* Shopify Admin GraphQL API (``/admin/api/<version>/graphql.json``) with an access token obtained through
  Shopify OAuth (or a custom-app Admin API token), webhooks signed with ``X-Shopify-Hmac-Sha256``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.config import get_settings
from app.services.commerce.money import PricingError, to_money
from app.services.integrations.http import IntegrationHTTPError, SafeAPIClient

SHOP_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")


class ProviderError(RuntimeError):
    pass


@dataclass
class ExternalVariant:
    external_id: str
    sku: str | None = None
    name: str | None = None
    options: dict[str, str] = field(default_factory=dict)
    regular_price: Decimal | None = None
    sale_price: Decimal | None = None
    manage_stock: bool = False
    stock_quantity: int | None = None
    image_url: str | None = None
    inventory_item_id: str | None = None


@dataclass
class ExternalProduct:
    external_id: str
    name: str | None
    sku: str | None = None
    description: str | None = None
    brand: str | None = None
    product_type: str | None = None
    status: str = "active"  # active|draft|archived
    regular_price: Decimal | None = None
    sale_price: Decimal | None = None
    manage_stock: bool = False
    stock_quantity: int | None = None
    availability: str | None = None
    categories: list[str] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    url: str | None = None
    attributes: list[tuple[str, str]] = field(default_factory=list)
    variants: list[ExternalVariant] = field(default_factory=list)
    gtin: str | None = None
    default_variant_id: str | None = None  # Shopify: products without options still have one variant
    inventory_item_id: str | None = None

    def fingerprint(self) -> str:
        payload = json.dumps({
            "n": self.name, "s": self.sku, "r": str(self.regular_price), "sp": str(self.sale_price), "q": self.stock_quantity,
            "v": [[v.external_id, str(v.regular_price), str(v.sale_price), v.stock_quantity] for v in self.variants],
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


def _money(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        d = to_money(v)
    except PricingError:
        return None
    return d if d is not None and d > 0 else None


class CommerceProvider(ABC):
    name: str

    def __init__(self, store_url: str, credentials: dict, client: SafeAPIClient | None = None):
        self.store_url = store_url.rstrip("/")
        self.credentials = credentials
        self.client = client or SafeAPIClient()

    async def aclose(self) -> None:
        await self.client.aclose()

    @abstractmethod
    async def verify(self) -> dict: ...

    @abstractmethod
    def iter_products(self) -> AsyncIterator[ExternalProduct]: ...

    @abstractmethod
    async def update_price(self, external_id: str, external_variant_id: str | None, regular: Decimal | None,
                           sale: Decimal | None) -> None: ...

    @abstractmethod
    async def update_inventory(self, external_id: str, external_variant_id: str | None, quantity: int,
                               inventory_item_id: str | None = None) -> None: ...

    @staticmethod
    @abstractmethod
    def verify_webhook(secret: str, headers: dict[str, str], body: bytes) -> bool: ...

    @staticmethod
    @abstractmethod
    def webhook_meta(headers: dict[str, str]) -> tuple[str | None, str | None]:
        """(delivery id, topic)"""


# ---------------------------------------------------------------- WooCommerce
class WooCommerceProvider(CommerceProvider):
    name = "woocommerce"

    @property
    def api(self) -> str:
        return f"{self.store_url}/wp-json/wc/v3"

    async def _call(self, method: str, path: str, params: dict | None = None, body=None):
        resp = await self.client.request(method, f"{self.api}{path}", params=params, json_body=body,
                                         auth=(self.credentials["consumer_key"], self.credentials["consumer_secret"]))
        if resp.status in (401, 403):
            raise ProviderError("WooCommerce rejected the API keys (check the consumer key/secret and their Read/Write permission)")
        if resp.status == 404:
            raise ProviderError("WooCommerce REST API not found at this URL (is WooCommerce installed and permalinks enabled?)")
        if resp.status >= 400:
            msg = (resp.json() or {}).get("message") if resp.body.startswith(b"{") else None
            raise ProviderError(f"WooCommerce API error (HTTP {resp.status}){': ' + str(msg)[:200] if msg else ''}")
        return resp

    async def verify(self) -> dict:
        await self._call("GET", "/products", {"per_page": 1})
        currency = None
        try:
            cur = (await self._call("GET", "/data/currencies/current")).json()
            currency = (cur or {}).get("code")
        except ProviderError:
            pass  # key may lack settings access; products work, currency falls back to the business default
        return {"ok": True, "currency": currency}

    @staticmethod
    def _variant(v: dict) -> ExternalVariant:
        opts = {a.get("name") or "Option": a.get("option") for a in v.get("attributes") or [] if a.get("option")}
        return ExternalVariant(
            external_id=str(v["id"]), sku=v.get("sku") or None, name=" / ".join(opts.values()) or None, options=opts,
            regular_price=_money(v.get("regular_price")), sale_price=_money(v.get("sale_price")),
            manage_stock=v.get("manage_stock") is True, stock_quantity=v.get("stock_quantity"),
            image_url=(v.get("image") or {}).get("src"))

    async def iter_products(self) -> AsyncIterator[ExternalProduct]:
        page = 1
        while True:
            resp = await self._call("GET", "/products", {"per_page": 100, "page": page})
            items = resp.json() or []
            for raw in items:
                variants = []
                if raw.get("type") == "variable" and raw.get("variations"):
                    vpage = 1
                    while True:
                        vr = await self._call("GET", f"/products/{raw['id']}/variations", {"per_page": 100, "page": vpage})
                        variants.extend(self._variant(v) for v in vr.json() or [])
                        if vpage >= int(vr.headers.get("x-wp-totalpages") or 1):
                            break
                        vpage += 1
                stock_status = raw.get("stock_status")
                yield ExternalProduct(
                    external_id=str(raw["id"]), name=raw.get("name"), sku=raw.get("sku") or None,
                    description=re.sub(r"<[^>]+>", " ", raw.get("description") or raw.get("short_description") or "").strip() or None,
                    status={"publish": "active"}.get(raw.get("status"), "draft"),
                    regular_price=_money(raw.get("regular_price")), sale_price=_money(raw.get("sale_price")),
                    manage_stock=raw.get("manage_stock") is True, stock_quantity=raw.get("stock_quantity"),
                    availability={"instock": "InStock", "outofstock": "OutOfStock", "onbackorder": "BackOrder"}.get(stock_status),
                    categories=[c.get("name") for c in raw.get("categories") or [] if c.get("name")],
                    images=[{"url": i["src"], "alt": i.get("alt") or None} for i in raw.get("images") or [] if i.get("src")],
                    url=raw.get("permalink"),
                    attributes=[(a.get("name"), ", ".join(a.get("options") or [])) for a in raw.get("attributes") or []
                                if a.get("name") and not a.get("variation")],
                    variants=variants, gtin=raw.get("global_unique_id") or None,
                )
            if page >= int(resp.headers.get("x-wp-totalpages") or 1) or not items:
                return
            page += 1

    async def update_price(self, external_id, external_variant_id, regular, sale) -> None:
        body = {"regular_price": format(regular, "f") if regular is not None else "",
                "sale_price": format(sale, "f") if sale is not None else ""}
        path = f"/products/{external_id}/variations/{external_variant_id}" if external_variant_id else f"/products/{external_id}"
        await self._call("PUT", path, body=body)

    async def update_inventory(self, external_id, external_variant_id, quantity, inventory_item_id=None) -> None:
        path = f"/products/{external_id}/variations/{external_variant_id}" if external_variant_id else f"/products/{external_id}"
        await self._call("PUT", path, body={"manage_stock": True, "stock_quantity": quantity})

    @staticmethod
    def verify_webhook(secret, headers, body) -> bool:
        sig = headers.get("x-wc-webhook-signature")
        if not sig or not secret:
            return False
        expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(sig, expected)

    @staticmethod
    def webhook_meta(headers):
        return headers.get("x-wc-webhook-delivery-id"), headers.get("x-wc-webhook-topic")


# ---------------------------------------------------------------- Shopify
PRODUCTS_QUERY = """
query Products($cursor: String) {
  shop { name currencyCode }
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title descriptionHtml vendor productType status onlineStoreUrl
      media(first: 10) { nodes { ... on MediaImage { image { url altText } } } }
      variants(first: 100) {
        nodes { id title sku price compareAtPrice inventoryQuantity
                selectedOptions { name value } inventoryItem { id tracked } }
      }
    }
  }
}"""
PRICE_MUTATION = """
mutation UpdatePrice($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) { userErrors { field message } }
}"""
LOCATION_QUERY = "query { locations(first: 1) { nodes { id } } }"
INVENTORY_MUTATION = """
mutation SetQty($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) { userErrors { field message } }
}"""


def valid_shop_domain(shop: str) -> bool:
    return bool(SHOP_DOMAIN.fullmatch((shop or "").strip().lower()))


class ShopifyProvider(CommerceProvider):
    name = "shopify"

    @property
    def endpoint(self) -> str:
        return f"{self.store_url}/admin/api/{get_settings().shopify_api_version}/graphql.json"

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        resp = await self.client.request("POST", self.endpoint, json_body={"query": query, "variables": variables or {}},
                                         headers={"X-Shopify-Access-Token": self.credentials["access_token"],
                                                  "Content-Type": "application/json"})
        if resp.status in (401, 403):
            raise ProviderError("Shopify rejected the access token (reconnect the store or check the app's scopes)")
        if resp.status >= 400:
            raise ProviderError(f"Shopify API error (HTTP {resp.status})")
        data = resp.json() or {}
        if data.get("errors"):
            first = data["errors"][0] if isinstance(data["errors"], list) else data["errors"]
            msg = first.get("message") if isinstance(first, dict) else str(first)
            if "THROTTLED" in json.dumps(data["errors"]):
                raise IntegrationHTTPError("Shopify API rate limit reached; the sync will retry", 429, True)
            raise ProviderError(f"Shopify API error: {str(msg)[:200]}")
        return data.get("data") or {}

    async def verify(self) -> dict:
        data = await self._gql("query { shop { name currencyCode } }")
        shop = data.get("shop") or {}
        return {"ok": True, "currency": shop.get("currencyCode"), "shop_name": shop.get("name")}

    async def iter_products(self) -> AsyncIterator[ExternalProduct]:
        cursor = None
        while True:
            data = await self._gql(PRODUCTS_QUERY, {"cursor": cursor})
            conn = data.get("products") or {}
            for n in conn.get("nodes") or []:
                variants = []
                for v in (n.get("variants") or {}).get("nodes") or []:
                    price, compare = _money(v.get("price")), _money(v.get("compareAtPrice"))
                    regular, sale = (compare, price) if compare and price and compare > price else (price, None)
                    opts = {o["name"]: o["value"] for o in v.get("selectedOptions") or [] if o.get("value") != "Default Title"}
                    variants.append(ExternalVariant(
                        external_id=v["id"], sku=v.get("sku") or None, name=v.get("title"), options=opts,
                        regular_price=regular, sale_price=sale, manage_stock=bool((v.get("inventoryItem") or {}).get("tracked")),
                        stock_quantity=v.get("inventoryQuantity"), inventory_item_id=(v.get("inventoryItem") or {}).get("id")))
                single = len(variants) == 1 and not variants[0].options
                base = variants[0] if single else None
                yield ExternalProduct(
                    external_id=n["id"], name=n.get("title"), sku=base.sku if base else None,
                    description=re.sub(r"<[^>]+>", " ", n.get("descriptionHtml") or "").strip() or None,
                    brand=n.get("vendor") or None, product_type=n.get("productType") or None,
                    status={"ACTIVE": "active", "ARCHIVED": "archived"}.get(n.get("status"), "draft"),
                    regular_price=base.regular_price if base else None, sale_price=base.sale_price if base else None,
                    manage_stock=base.manage_stock if base else False, stock_quantity=base.stock_quantity if base else None,
                    images=[{"url": m["image"]["url"], "alt": m["image"].get("altText")}
                            for m in (n.get("media") or {}).get("nodes") or [] if (m or {}).get("image")],
                    url=n.get("onlineStoreUrl"), variants=[] if single else variants,
                    default_variant_id=base.external_id if base else None,
                    inventory_item_id=base.inventory_item_id if base else None,
                )
            info = conn.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return
            cursor = info.get("endCursor")

    async def update_price(self, external_id, external_variant_id, regular, sale) -> None:
        if not external_variant_id:
            raise ProviderError("Shopify prices are set per variant")
        variant = {"id": external_variant_id,
                   "price": format(sale if sale is not None else regular, "f"),
                   "compareAtPrice": format(regular, "f") if sale is not None else None}
        data = await self._gql(PRICE_MUTATION, {"productId": external_id, "variants": [variant]})
        errs = (data.get("productVariantsBulkUpdate") or {}).get("userErrors") or []
        if errs:
            raise ProviderError(f"Shopify refused the price update: {errs[0].get('message')}")

    async def update_inventory(self, external_id, external_variant_id, quantity, inventory_item_id=None) -> None:
        if not inventory_item_id:
            raise ProviderError("This Shopify variant has no inventory item")
        loc = await self._gql(LOCATION_QUERY)
        nodes = (loc.get("locations") or {}).get("nodes") or []
        if not nodes:
            raise ProviderError("The Shopify store has no location")
        data = await self._gql(INVENTORY_MUTATION, {"input": {
            "name": "available", "reason": "correction", "ignoreCompareQuantity": True,
            "quantities": [{"inventoryItemId": inventory_item_id, "locationId": nodes[0]["id"], "quantity": quantity}]}})
        errs = (data.get("inventorySetQuantities") or {}).get("userErrors") or []
        if errs:
            raise ProviderError(f"Shopify refused the inventory update: {errs[0].get('message')}")

    @staticmethod
    def verify_webhook(secret, headers, body) -> bool:
        sig = headers.get("x-shopify-hmac-sha256")
        if not sig or not secret:
            return False
        expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(sig, expected)

    @staticmethod
    def webhook_meta(headers):
        return headers.get("x-shopify-webhook-id"), headers.get("x-shopify-topic")


def verify_oauth_hmac(params: dict[str, str], secret: str) -> bool:
    """Shopify OAuth callback check: HMAC-SHA256 (hex) of the sorted query string without ``hmac``."""
    given = params.get("hmac", "")
    msg = "&".join(f"{k}={v}" for k, v in sorted((k, v) for k, v in params.items() if k not in ("hmac", "signature")))
    expected = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return bool(given) and hmac.compare_digest(given, expected)


PROVIDERS = {"woocommerce": WooCommerceProvider, "shopify": ShopifyProvider}


def build(provider: str, store_url: str, credentials: dict, client: SafeAPIClient | None = None) -> CommerceProvider:
    return PROVIDERS[provider](store_url, credentials, client)
