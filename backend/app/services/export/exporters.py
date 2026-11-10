"""Export formats.

* ``json``   — AgentReady structured product records.
* ``csv``    — spreadsheet-friendly, one row per product, attributes flattened.
* ``jsonld`` — schema.org ``ItemList`` of ``Product`` + ``Offer``.
* ``feed``   — AgentReady Normalized Product Feed v1 (documented in
               docs/feed-format.md), designed for future commerce integrations.

Only data with provenance is exported: extracted values (which carry source
evidence) and merchant edits. By default only approved products are included.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

from app.models import Business, Product
from app.services.catalog.serialize import money, product_dict

FEED_VERSION = "agentready-feed/1.0"
FORMATS = {
    "json": ("application/json", "json"),
    "csv": ("text/csv; charset=utf-8", "csv"),
    "jsonld": ("application/ld+json", "jsonld"),
    "feed": ("application/json", "feed.json"),
}

SCHEMA_AVAIL = "https://schema.org/"


def _record(p: Product) -> dict:
    d = product_dict(p, include_private=False)
    for k in ("flags", "is_removed", "manual_overrides", "last_scan_id", "website_id", "quality_score", "deleted_at"):
        d.pop(k, None)
    return d


def to_json(products: list[Product], business: Business) -> str:
    return json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(), "business": business.name,
         "count": len(products), "products": [_record(p) for p in products]},
        ensure_ascii=False, indent=2,
    )


def to_csv(products: list[Product]) -> str:
    attr_keys: list[str] = []
    for p in products:
        for a in p.attributes:
            if a.key not in attr_keys:
                attr_keys.append(a.key)
    base_cols = ["id", "name", "sku", "brand", "manufacturer", "category", "description", "price", "currency",
                 "availability", "gtin", "mpn", "material", "product_url", "source_url", "image_url", "additional_images",
                 "approval_status", "quality_status", "updated_at"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(base_cols + [f"attr:{k}" for k in attr_keys])
    for p in products:
        imgs = [i.get("url") for i in p.images or [] if i.get("url")]
        attrs = {a.key: (f"{a.value} {a.unit}".strip() if a.unit else (a.value or "")) for a in p.attributes}
        row = [str(p.id), p.name, p.sku, p.brand, p.manufacturer, p.category, p.description,
               money(p.price_amount), p.currency, p.availability, p.gtin, p.mpn, p.material, p.product_url,
               p.source_url, imgs[0] if imgs else "", " ".join(imgs[1:]), p.approval_status, p.quality_status,
               p.updated_at.isoformat() if p.updated_at else ""]
        # neutralise spreadsheet formula injection
        safe = [("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else ("" if v is None else v) for v in row]
        w.writerow(safe + [attrs.get(k, "") for k in attr_keys])
    return buf.getvalue()


def jsonld_product(p: Product, business: Business, keep_attribute=None) -> dict:
    node: dict = {"@type": "Product", "name": p.name}
    if p.product_url or p.source_url:
        node["url"] = p.product_url or p.source_url
    for k, v in (("sku", p.sku), ("description", p.description), ("category", p.category), ("mpn", p.mpn),
                 ("material", p.material)):
        if v:
            node[k] = v
    if p.gtin:
        node[{8: "gtin8", 12: "gtin12", 13: "gtin13", 14: "gtin14"}.get(len(p.gtin), "gtin")] = p.gtin
    if p.brand:
        node["brand"] = {"@type": "Brand", "name": p.brand}
    if p.manufacturer:
        node["manufacturer"] = {"@type": "Organization", "name": p.manufacturer}
    imgs = [i.get("url") for i in p.images or [] if i.get("url")]
    if imgs:
        node["image"] = imgs
    if p.weight and p.weight.get("value"):
        node["weight"] = {"@type": "QuantitativeValue", "value": p.weight["value"], "unitText": p.weight.get("unit")}
    attrs = [a for a in p.attributes if keep_attribute is None or keep_attribute(a.key)]
    if attrs:
        node["additionalProperty"] = [
            {"@type": "PropertyValue", "name": a.label, "value": a.value, **({"unitText": a.unit} if a.unit else {})}
            for a in attrs
        ]
    if p.price_amount is not None and p.currency:
        offer = {"@type": "Offer", "price": f"{p.price_amount:.2f}", "priceCurrency": p.currency,
                 "seller": {"@type": "Organization", "name": business.name}}
        if p.availability:
            offer["availability"] = SCHEMA_AVAIL + p.availability
        if p.product_url:
            offer["url"] = p.product_url
        node["offers"] = offer
    if p.variants:
        node["hasVariant"] = [
            {"@type": "Product", "name": v.name or p.name, **({"sku": v.sku} if v.sku else {}),
             **({"offers": {"@type": "Offer", "price": f"{v.price_amount:.2f}", "priceCurrency": v.currency or p.currency}}
                if v.price_amount is not None and (v.currency or p.currency) else {})}
            for v in p.variants
        ]
    return node


def to_jsonld(products: list[Product], business: Business) -> str:
    doc = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": f"{business.name} product catalog",
        "numberOfItems": len(products),
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "item": jsonld_product(p, business)} for i, p in enumerate(products)
        ],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


def feed_item(p: Product, keep_attribute=None) -> dict:
    return {
        "id": str(p.id),
        "version": p.version,
        "title": p.name,
        "identifiers": {"sku": p.sku, "gtin": p.gtin, "mpn": p.mpn},
        "brand": p.brand,
        "manufacturer": p.manufacturer,
        "category": {"value": p.category, "inferred": p.category_inferred} if p.category else None,
        "description": p.description,
        "offer": {
            "price": {"amount": f"{p.price_amount:.2f}", "currency": p.currency} if p.price_amount is not None else None,
            "availability": p.availability,
            "url": p.product_url or p.source_url,
        },
        "images": [i.get("url") for i in p.images or [] if i.get("url")],
        "attributes": [{"key": a.key, "name": a.label, "value": a.value, "unit": a.unit} for a in p.attributes
                       if keep_attribute is None or keep_attribute(a.key)],
        "variants": [{"name": v.name, "sku": v.sku,
                      "price": {"amount": f"{v.price_amount:.2f}", "currency": v.currency or p.currency} if v.price_amount is not None else None,
                      "attributes": v.attributes} for v in p.variants],
        "provenance": {"source_url": p.source_url, "merchant_edited_fields": p.manual_overrides or []},
        "status": {"approval": p.approval_status, "quality": p.quality_status},
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def to_feed(products: list[Product], business: Business) -> str:
    return json.dumps(
        {
            "feed_version": FEED_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "merchant": {"id": str(business.id), "name": business.name, "default_currency": business.default_currency},
            "item_count": len(products),
            "items": [feed_item(p) for p in products],
        },
        ensure_ascii=False, indent=2,
    )


def render(fmt: str, products: list[Product], business: Business) -> str:
    if fmt == "json":
        return to_json(products, business)
    if fmt == "csv":
        return to_csv(products)
    if fmt == "jsonld":
        return to_jsonld(products, business)
    if fmt == "feed":
        return to_feed(products, business)
    raise ValueError(f"unknown export format {fmt}")
