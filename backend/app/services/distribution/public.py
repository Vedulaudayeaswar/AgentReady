"""Canonical public product representation (schema ``agentready-product/1.0``).

This is the ONLY serializer used for anything an external consumer can see: the agent API, MCP tools, feeds,
webhook payloads and the merchant's distribution preview. It is separate from the internal merchant serializer
(``app.services.catalog.serialize.product_dict``) and never includes cost prices, stock quantities, reserved
stock, locations, notes, evidence, approval/quality internals or credentials.

Unknown is not the same as unavailable: a value the merchant never provided is listed in ``unknown_fields``;
a value the merchant chose not to publish is listed in ``withheld_fields``; ``availability.status`` is
``unknown`` when there is no stock signal at all, which is different from ``out_of_stock``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import String, and_, cast, or_

from app.models import Product, ProductVariant
from app.services.catalog.serialize import PUBLIC_LIFECYCLE, is_public
from app.services.commerce.money import aware, fmt, now_utc, price_state
from app.services.distribution.profile import MerchantScope, channel_open
from app.services.distribution.units import normalized

SCHEMA = "agentready-product/1.0"
AVAILABILITY_VALUES = ("available", "out_of_stock", "preorder", "backorder", "discontinued", "unknown")
# keys that could leak private inventory data or internal notes even if they were extracted as attributes
PRIVATE_ATTRIBUTE_KEYS = {"stock_quantity", "cost", "cost_price", "internal_notes", "notes", "supplier", "margin"}


def is_private_attribute_key(key: str | None) -> bool:
    """Attribute rows that must never leave AgentReady through any external API or feed."""
    return (key or "").strip().lower() in PRIVATE_ATTRIBUTE_KEYS

_FROM_SCHEMA_ORG = {"instock": "available", "limitedavailability": "available", "onlineonly": "available",
                    "instoreonly": "available", "outofstock": "out_of_stock", "soldout": "out_of_stock",
                    "preorder": "preorder", "presale": "preorder", "backorder": "backorder",
                    "discontinued": "discontinued"}
_FROM_STOCK = {"in_stock": "available", "low_stock": "available", "out_of_stock": "out_of_stock", "backorder": "backorder"}


# ------------------------------------------------------------------ eligibility
def eligibility_clause(scope: MerchantScope, channel: str):
    """SQL WHERE clause: products of this merchant distributable through ``channel`` (profile checked separately)."""
    p = scope.profile
    conds = [Product.business_id == scope.business_id, Product.approval_status == "approved",
             Product.publication_status == "published", Product.lifecycle_status.in_(PUBLIC_LIFECYCLE),
             Product.deleted_at.is_(None), Product.is_removed.is_(False)]
    if p.product_scope == "selected":
        conds.append(Product.distribution_enabled.is_(True))
    else:
        conds.append(or_(Product.distribution_enabled.is_(None), Product.distribution_enabled.is_(True)))
    if scope.excluded_categories:
        conds.append(or_(Product.category_id.is_(None), Product.category_id.not_in(list(scope.excluded_categories))))
    conds.append(or_(Product.distribution_channels.is_(None),
                     cast(Product.distribution_channels, String).like(f'%"{channel}"%')))
    return and_(*conds)


def distribution_status(p: Product, scope: MerchantScope, channel: str | None = None, at: datetime | None = None) -> dict:
    """Why a product is (not) visible to external consumers. Mirrors ``eligibility_clause`` exactly."""
    reasons: list[str] = []
    prof = scope.profile
    if p.deleted_at is not None:
        reasons.append("Product is deleted")
    if p.is_removed:
        reasons.append("Product was removed from the website")
    if p.approval_status != "approved":
        reasons.append("Product is not approved")
    if p.publication_status != "published":
        reasons.append("Product is not published")
    if p.lifecycle_status not in PUBLIC_LIFECYCLE:
        reasons.append(f"Product is {p.lifecycle_status}")
    if p.distribution_enabled is False:
        reasons.append("Distribution is turned off for this product")
    elif prof.product_scope == "selected" and p.distribution_enabled is not True:
        reasons.append("Only selected products are distributed and this product is not selected")
    if p.category_id is not None and p.category_id in scope.excluded_categories:
        reasons.append("Its category is excluded from distribution")
    channels = {c: True for c in ("agent_api", "mcp", "feeds", "webhooks")}
    for c in channels:
        ok, _ = channel_open(prof, c, at)
        channels[c] = ok and (p.distribution_channels is None or c in (p.distribution_channels or []))
    merchant_ok, merchant_reason = channel_open(prof, channel or "agent_api", at)
    if not prof.enabled or merchant_reason and "expired" in merchant_reason:
        reasons.append(merchant_reason or "Public distribution is disabled")
    product_ok = not reasons
    return {"distributed": product_ok and any(channels.values()), "eligible": product_ok,
            "reasons": reasons, "channels": {c: product_ok and ok for c, ok in channels.items()},
            "merchant_enabled": merchant_ok}


def is_distributable(p: Product, scope: MerchantScope, channel: str, at: datetime | None = None) -> bool:
    st = distribution_status(p, scope, channel, at)
    return st["eligible"] and st["channels"].get(channel, False)


# ------------------------------------------------------------------ availability & price
def availability_of(p: Product) -> dict:
    if p.lifecycle_status == "discontinued":
        return {"status": "discontinued", "source": "merchant"}
    if p.track_inventory or any(v.track_inventory for v in live_variants(p)):
        st = p.stock_status
        if st in _FROM_STOCK:
            return {"status": _FROM_STOCK[st], "source": "inventory"}
    raw = (p.availability or "").replace(" ", "").lower()
    if raw in _FROM_SCHEMA_ORG:
        return {"status": _FROM_SCHEMA_ORG[raw], "source": "website" if p.origin == "website" else "merchant"}
    return {"status": "unknown", "source": None}


def variant_availability(v: ProductVariant, p: Product) -> dict:
    if p.lifecycle_status == "discontinued":
        return {"status": "discontinued", "source": "merchant"}
    if v.track_inventory and v.stock_status in _FROM_STOCK:
        return {"status": _FROM_STOCK[v.stock_status], "source": "inventory"}
    raw = (v.availability or "").replace(" ", "").lower()
    if raw in _FROM_SCHEMA_ORG:
        return {"status": _FROM_SCHEMA_ORG[raw], "source": "website" if v.origin == "extracted" else "merchant"}
    if not p.track_inventory:
        return availability_of(p)
    return {"status": "unknown", "source": None}


def _boundaries(starts, ends, at) -> datetime | None:
    nxt = [d for d in (aware(starts), aware(ends)) if d is not None and d > at]
    return min(nxt) if nxt else None


def price_block(regular: Decimal | None, sale: Decimal | None, currency: str | None, p: Product, at: datetime) -> dict | None:
    current, status = price_state(regular, sale, p.sale_starts_at, p.sale_ends_at, at)
    if current is None:
        return None
    vu = _boundaries(p.sale_starts_at, p.sale_ends_at, at) if sale is not None else None
    return {
        "currency": currency, "regular": fmt(regular), "current": fmt(current),
        "sale": fmt(sale) if status == "on_sale" else None, "on_sale": status == "on_sale",
        "sale_starts_at": aware(p.sale_starts_at).isoformat() if sale is not None and p.sale_starts_at else None,
        "sale_ends_at": aware(p.sale_ends_at).isoformat() if sale is not None and p.sale_ends_at else None,
        "upcoming_sale": {"price": fmt(sale), "starts_at": aware(p.sale_starts_at).isoformat()} if status == "sale_scheduled" else None,
        "tax_included": p.tax_included,
        "valid_until": vu.isoformat() if vu else None,
    }


def product_price(p: Product, at: datetime) -> dict | None:
    return price_block(p.price_amount, p.sale_price_amount, p.currency, p, at)


def variant_price(v: ProductVariant, p: Product, at: datetime) -> dict | None:
    regular = v.price_amount if v.price_amount is not None else p.price_amount
    sale = v.sale_price_amount if v.price_amount is not None else (v.sale_price_amount or p.sale_price_amount)
    return price_block(regular, sale, v.currency or p.currency, p, at)


def live_variants(p: Product) -> list[ProductVariant]:
    return sorted([v for v in p.variants if v.archived_at is None and v.is_enabled], key=lambda v: (v.position, v.name or ""))


# ------------------------------------------------------------------ attributes
def visible_attribute_keys(p: Product, scope: MerchantScope) -> set[str] | None:
    """None = all (minus hidden/private)."""
    if p.public_attribute_keys is not None:
        return {str(k).lower() for k in p.public_attribute_keys}
    if scope.profile.public_attribute_keys is not None:
        return {str(k).lower() for k in scope.profile.public_attribute_keys}
    return None


def public_attributes(p: Product, scope: MerchantScope) -> list[dict]:
    allowed = visible_attribute_keys(p, scope)
    hidden = {str(k).lower() for k in scope.profile.hidden_attribute_keys or []} | PRIVATE_ATTRIBUTE_KEYS
    out = []
    for a in p.attributes:
        k = (a.key or "").lower()
        if k in hidden or (allowed is not None and k not in allowed) or a.value in (None, ""):
            continue
        out.append({"key": a.key, "name": a.label, "value": a.value, "unit": a.unit,
                    "normalized": normalized(a.value, a.unit)})
    return out


# ------------------------------------------------------------------ the representation
def public_product(p: Product, scope: MerchantScope, at: datetime | None = None, *, compact: bool = False) -> dict:
    at = at or now_utc()
    prof = scope.profile
    images = p.public_images if p.public_images is not None else [i.get("url") for i in p.images or [] if i.get("url")]
    description = p.public_description if p.public_description else p.description
    category = None
    if p.category_id and p.category_id in scope.category_paths:
        category = {"id": str(p.category_id), "path": scope.category_paths[p.category_id]}
    elif p.category:
        category = {"id": None, "path": p.category}
    unknown: list[str] = []
    withheld: list[str] = []
    out: dict = {
        "id": str(p.id),
        "merchant": scope.merchant_ref(),
        "name": p.name,
        "description": description,
        "brand": p.brand,
        "manufacturer": p.manufacturer,
        "identifiers": {"sku": p.sku, "gtin": p.gtin, "mpn": p.mpn},
        "category": category,
        "product_type": p.product_type,
        "url": p.product_url or p.source_url,
        "images": images,
    }
    if prof.show_prices:
        out["price"] = product_price(p, at)
        if out["price"] is None:
            unknown.append("price")
    else:
        out["price"] = None
        withheld.append("price")
    if prof.show_availability:
        out["availability"] = {**availability_of(p), "as_of": at.isoformat()}
        if out["availability"]["status"] == "unknown":
            unknown.append("availability")
    else:
        out["availability"] = None
        withheld.append("availability")
    out["attributes"] = public_attributes(p, scope)
    variants = []
    for v in live_variants(p):
        vd = {"id": str(v.id), "name": v.name, "sku": v.sku, "options": v.attributes or {}, "image": v.image_url}
        vd["price"] = variant_price(v, p, at) if prof.show_prices else None
        vd["availability"] = variant_availability(v, p) if prof.show_availability else None
        variants.append(vd)
    out["variants"] = variants
    if not compact:
        out["specifications"] = {"weight": p.weight or None, "dimensions": p.dimensions or None, "material": p.material}
        ship = p.shipping or {}
        out["shipping"] = {k: ship.get(k) for k in ("requires_shipping", "shipping_class", "lead_time_days", "notes")
                           if ship.get(k) is not None} or None
        if out["shipping"] is None:
            unknown.append("shipping")
    for f, v in (("description", description), ("brand", p.brand), ("gtin", p.gtin), ("mpn", p.mpn),
                 ("category", category), ("images", images), ("url", out["url"])):
        if not v:
            unknown.append(f)
    out["unknown_fields"] = unknown
    out["withheld_fields"] = withheld
    out["version"] = p.version
    out["updated_at"] = aware(p.updated_at).isoformat() if p.updated_at else None
    return out


def tombstone(p: Product, reason: str) -> dict:
    return {"id": str(p.id), "removed": True, "reason": reason,
            "at": aware(p.updated_at).isoformat() if p.updated_at else now_utc().isoformat()}


def removal_reason(p: Product) -> str:
    if p.deleted_at is not None:
        return "deleted"
    if p.lifecycle_status == "archived":
        return "archived"
    if not is_public(p):
        return "unpublished"
    return "not_distributed"
