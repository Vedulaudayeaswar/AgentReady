"""Catalog persistence: snapshots, change detection, applying scans."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    PriceHistory,
    Product,
    ProductAttribute,
    ProductEvidence,
    ProductVariant,
    ScanJob,
    ScanProductSnapshot,
)
from app.schemas.canonical import ExtractedProduct
from app.services.normalization.normalize import normalize_name_for_match
from app.services.normalization.product import match_key
from app.services.validation.validator import revalidate_business

# fields compared between scans and their change-report category
TRACKED_FIELDS = {
    "name": "details", "sku": "details", "brand": "details", "manufacturer": "details", "category": "details",
    "description": "description", "price_amount": "price", "currency": "price", "availability": "availability",
    "gtin": "details", "mpn": "details", "material": "specifications", "dimensions": "specifications",
    "weight": "specifications", "images": "images", "attributes": "specifications", "product_url": "details",
}


# finer change categories used by the Phase 2 change report / detection switches
FIELD_CATEGORY = {"name": "name", "sku": "sku", "images": "images"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ projections
def snapshot_fields(data: dict) -> dict[str, Any]:
    """Project a canonical snapshot (ExtractedProduct JSON) onto product columns."""
    price = data.get("price") or {}
    amount = price.get("amount")
    return {
        "name": data.get("name"),
        "sku": data.get("sku"),
        "brand": data.get("brand"),
        "manufacturer": data.get("manufacturer"),
        "category": data.get("category"),
        "description": data.get("description"),
        "price_amount": str(Decimal(str(amount)).quantize(Decimal("0.01"))) if amount is not None else None,
        "currency": price.get("currency"),
        "availability": data.get("availability"),
        "gtin": data.get("gtin"),
        "mpn": data.get("mpn"),
        "material": data.get("material"),
        "dimensions": data.get("dimensions"),
        "weight": data.get("weight"),
        "product_url": data.get("product_url"),
        "images": sorted(i["url"] for i in data.get("images") or []),
        "attributes": {a["key"]: [a.get("value"), a.get("unit")] for a in data.get("attributes") or []},
    }


def product_fields(p: Product) -> dict[str, Any]:
    return {
        "name": p.name, "sku": p.sku, "brand": p.brand, "manufacturer": p.manufacturer, "category": p.category,
        "description": p.description,
        "price_amount": str(Decimal(p.price_amount).quantize(Decimal("0.01"))) if p.price_amount is not None else None,
        "currency": p.currency, "availability": p.availability, "gtin": p.gtin, "mpn": p.mpn, "material": p.material,
        "dimensions": p.dimensions, "weight": p.weight, "product_url": p.product_url,
        "images": sorted(i.get("url") for i in p.images or []),
        "attributes": {a.key: [a.value, a.unit] for a in p.attributes if a.source != "merchant"},
    }


def compute_changes(product: Product, data: dict) -> list[dict]:
    """Field changes between the catalog and a new extraction.

    For merchant-managed fields (``manual_overrides``) the website is compared with the website's own
    previous value (``source_values``): a change is reported only when the WEBSITE changed, and it is
    flagged ``manual_override`` so it becomes a conflict instead of being applied.
    """
    old, new = product_fields(product), snapshot_fields(data)
    overrides = set(product.manual_overrides or [])
    previous_site = product.source_values or {}
    changes = []
    for f, category in TRACKED_FIELDS.items():
        managed = f in overrides or (f == "price_amount" and "price" in overrides)
        if managed and f in previous_site:
            if previous_site.get(f) == new.get(f):
                continue  # the website did not change; the merchant's value stands
            if old.get(f) == new.get(f):
                continue  # the website now agrees with the merchant
        elif old.get(f) == new.get(f):
            continue
        if f in ("images", "attributes") and not new.get(f):
            continue  # an empty extraction does not erase existing data
        if new.get(f) is None and old.get(f) is not None and f not in ("availability", "price_amount"):
            continue
        changes.append({
            "field": f, "category": FIELD_CATEGORY.get(f, category), "old": old.get(f), "new": new.get(f),
            "manual_override": managed,
        })
    return changes


# ------------------------------------------------------------------ matching
class Matcher:
    def __init__(self, products: list[Product]):
        self.by_key: dict[str, Product] = {}
        self.by_sku: dict[str, Product] = {}
        self.by_gtin: dict[str, Product] = {}
        self.by_url: dict[str, Product] = {}
        self.by_name: dict[str, Product] = {}
        for p in products:
            self.by_key.setdefault(p.match_key, p)
            if p.sku:
                self.by_sku.setdefault(p.sku.strip().lower(), p)
            if p.gtin:
                self.by_gtin.setdefault(p.gtin, p)
            if p.product_url and p.product_url != p.source_url:
                self.by_url.setdefault(p.product_url, p)
            n = normalize_name_for_match(p.name)
            if n:
                self.by_name.setdefault(f"{n}|{p.source_url}", p)

    def match(self, e: ExtractedProduct, key: str) -> Product | None:
        if key in self.by_key:
            return self.by_key[key]
        if e.sku and e.sku.strip().lower() in self.by_sku:
            return self.by_sku[e.sku.strip().lower()]
        if e.gtin and e.gtin in self.by_gtin:
            return self.by_gtin[e.gtin]
        if e.product_url and e.product_url in self.by_url:
            return self.by_url[e.product_url]
        return self.by_name.get(f"{normalize_name_for_match(e.name)}|{e.source_url}")


# ------------------------------------------------------------------ snapshots
async def build_snapshots(
    session: AsyncSession,
    scan: ScanJob,
    extracted: list[ExtractedProduct],
    page_ids: dict[str, uuid.UUID],
    crawled_ok: set[str],
    gone_urls: set[str],
) -> dict[str, int]:
    from app.services.commerce.sync import DEFAULT_DETECT, FIELD_KIND, record_conflicts

    detect = {**DEFAULT_DETECT, **((scan.settings or {}).get("detect") or {})}
    everything = (
        await session.execute(
            select(Product).where(
                Product.business_id == scan.business_id,
                Product.website_id == scan.website_id,
                Product.is_removed.is_(False),
            )
        )
    ).scalars().all()
    existing = [p for p in everything if p.deleted_at is None]
    deleted = Matcher([p for p in everything if p.deleted_at is not None])
    matcher = Matcher(list(existing))
    matched_ids: set[uuid.UUID] = set()
    summary = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0, "price_changes": 0,
               "description_changes": 0, "specification_changes": 0, "availability_changes": 0,
               "name_changes": 0, "sku_changes": 0, "image_changes": 0, "conflicts": 0,
               "ignored_changes": 0, "ignored_deleted": 0}
    for e in extracted:
        key = match_key(e)
        data = e.model_dump(mode="json")
        data["match_key"] = key
        prod = matcher.match(e, key)
        page_id = page_ids.get(e.source_url or "")
        if prod is None and deleted.match(e, key) is not None:
            summary["ignored_deleted"] += 1  # the merchant deleted this product; do not resurrect it
            continue
        if prod is None or prod.id in matched_ids:
            if not detect.get("new_products", True):
                summary["ignored_changes"] += 1
                continue
            session.add(ScanProductSnapshot(scan_id=scan.id, business_id=scan.business_id, page_id=page_id,
                                            match_key=key, change_type="new", data=data))
            summary["new"] += 1
            continue
        matched_ids.add(prod.id)
        all_changes = compute_changes(prod, data)
        changes = [c for c in all_changes if detect.get(FIELD_KIND.get(c["field"], "details"), True)]
        summary["ignored_changes"] += len(all_changes) - len(changes)
        ctype = "changed" if changes else "unchanged"
        summary[ctype] += 1
        cats = {c["category"] for c in changes}
        for cat, k in (("price", "price_changes"), ("description", "description_changes"),
                       ("specifications", "specification_changes"), ("availability", "availability_changes"),
                       ("name", "name_changes"), ("sku", "sku_changes"), ("images", "image_changes")):
            if cat in cats:
                summary[k] += 1
        summary["conflicts"] += await record_conflicts(session, scan, prod, changes, data)
        session.add(ScanProductSnapshot(scan_id=scan.id, business_id=scan.business_id, product_id=prod.id,
                                        page_id=page_id, match_key=key, change_type=ctype, data=data, changes=changes))
    # removed: only when we actually re-checked the source page (or it is gone)
    for prod in existing:
        if prod.id in matched_ids:
            continue
        if not detect.get("removed_products", True):
            continue
        src = prod.source_url or ""
        if src in gone_urls or src in crawled_ok:
            summary["removed"] += 1
            reason = "source page no longer exists" if src in gone_urls else "no longer found on its source page"
            session.add(ScanProductSnapshot(scan_id=scan.id, business_id=scan.business_id, product_id=prod.id,
                                            match_key=prod.match_key, change_type="removed", data=None,
                                            changes=[{"field": "_removed", "category": "removed", "old": prod.name,
                                                      "new": None, "reason": reason, "manual_override": False}]))
    await session.flush()
    return summary


# ------------------------------------------------------------------ apply
def _set_attributes(product: Product, data: dict) -> None:
    merchant_attrs = [a for a in product.attributes if a.source == "merchant"]
    merchant_keys = {a.key for a in merchant_attrs}
    product.attributes = merchant_attrs + [
        ProductAttribute(key=a["key"], label=a["label"], value=a.get("value"), unit=a.get("unit"),
                         raw_value=a.get("raw_value"), source="extracted", position=len(merchant_attrs) + i)
        for i, a in enumerate(data.get("attributes") or []) if a["key"] not in merchant_keys
    ]


def _set_variants(product: Product, data: dict) -> None:
    if any(v.origin != "extracted" for v in product.variants):
        return  # merchant-managed variants (with their own prices/stock history) are never replaced by a scan
    product.variants = [
        ProductVariant(
            name=v.get("name"), sku=v.get("sku"),
            price_amount=Decimal(str(v["price"]["amount"])) if v.get("price") and v["price"].get("amount") is not None else None,
            currency=(v.get("price") or {}).get("currency"), availability=v.get("availability"),
            attributes=v.get("attributes") or {},
        )
        for v in data.get("variants") or []
    ]


def write_product_fields(product: Product, data: dict, respect_overrides: bool = True) -> list[str]:
    """Write canonical snapshot data onto a product; returns fields skipped due to merchant edits."""
    overrides = set(product.manual_overrides or []) if respect_overrides else set()
    skipped = []
    price = data.get("price") or {}
    values = {
        "name": data.get("name"), "sku": data.get("sku"), "brand": data.get("brand"),
        "manufacturer": data.get("manufacturer"), "category": data.get("category"),
        "description": data.get("description"), "availability": data.get("availability"),
        "product_url": data.get("product_url"), "source_url": data.get("source_url"),
        "gtin": data.get("gtin"), "mpn": data.get("mpn"), "material": data.get("material"),
        "dimensions": data.get("dimensions"), "weight": data.get("weight"),
        "price_amount": Decimal(str(price["amount"])) if price.get("amount") is not None else None,
        "currency": price.get("currency"),
        "category_inferred": bool(data.get("category_inferred")),
    }
    for f, v in values.items():
        guard = "price" if f in ("price_amount",) else f
        if guard in overrides or f in overrides:
            skipped.append(f)
            continue
        setattr(product, f, v)
    if "images" in overrides:
        skipped.append("images")
    elif data.get("images"):
        product.images = data["images"]
    if "attributes" not in overrides:
        _set_attributes(product, data)
    if "variants" not in overrides:
        _set_variants(product, data)
    for v in product.variants:
        v.business_id = product.business_id
    # what the website says for each tracked field, regardless of merchant edits (conflict detection)
    product.source_values = snapshot_fields(data)
    product.match_key = data.get("match_key") or product.match_key
    product.extra = {**(product.extra or {}), "flags": data.get("flags") or [], "origin": data.get("origin"),
                     "currency_inferred": bool(price.get("currency_inferred"))}
    return skipped


async def _replace_evidence(session: AsyncSession, product: Product, data: dict, scan_id: uuid.UUID,
                            page_id: uuid.UUID | None, skipped: list[str]) -> None:
    await session.execute(
        delete(ProductEvidence).where(ProductEvidence.product_id == product.id, ProductEvidence.extraction_method != "merchant")
    )
    for ev in data.get("evidence") or []:
        root = ev["field"].split(".")[0]
        if root in skipped or (root == "price" and "price_amount" in skipped):
            continue
        session.add(ProductEvidence(
            product_id=product.id, business_id=product.business_id, scan_id=scan_id, page_id=page_id,
            field=ev["field"], value=ev.get("value"), source_value=ev.get("source_value"),
            source_url=ev.get("source_url"), source_fragment=ev.get("source_fragment"),
            source_type=ev.get("source_type") or "html", extraction_method=ev.get("extraction_method") or "deterministic",
            inferred=bool(ev.get("inferred")), model_id=ev.get("model_id"), prompt_version=ev.get("prompt_version"),
            confidence=ev.get("confidence"), verified_in_source=ev.get("verified_in_source"),
        ))


async def apply_scan(session: AsyncSession, scan: ScanJob, snapshot_ids: set[uuid.UUID] | None = None) -> dict[str, int]:
    """Apply a scan's snapshots to the catalog. ``snapshot_ids`` limits which changes are applied."""
    snaps = (await session.execute(
        select(ScanProductSnapshot).where(ScanProductSnapshot.scan_id == scan.id, ScanProductSnapshot.decision == "pending")
    )).scalars().all()
    counts = {"created": 0, "updated": 0, "removed": 0, "skipped": 0, "kept_merchant_edits": 0}
    for s in snaps:
        if snapshot_ids is not None and s.id not in snapshot_ids and s.change_type != "unchanged":
            s.decision = "skipped"
            counts["skipped"] += 1
            continue
        if s.change_type == "new":
            product = Product(business_id=scan.business_id, website_id=scan.website_id, last_scan_id=scan.id,
                              source_page_id=s.page_id, match_key=s.match_key, attributes=[], variants=[], images=[],
                              extra={}, manual_overrides=[], origin="website", lifecycle_status="active",
                              publication_status="unpublished", shipping={})
            write_product_fields(product, s.data or {})
            session.add(product)
            await session.flush()
            await _replace_evidence(session, product, s.data or {}, scan.id, s.page_id, [])
            s.product_id = product.id
            counts["created"] += 1
        elif s.change_type in ("changed", "unchanged") and s.product_id:
            product = await session.get(Product, s.product_id)
            if product is None:
                continue
            if s.change_type == "unchanged":
                product.source_values = snapshot_fields(s.data or {})
            if s.change_type == "changed":
                old_price, old_currency = product.price_amount, product.currency
                skipped = write_product_fields(product, s.data or {})
                if product.price_amount != old_price and "price_amount" not in skipped:
                    session.add(PriceHistory(
                        business_id=product.business_id, product_id=product.id, price_type="regular",
                        old_amount=old_price, new_amount=product.price_amount, currency=product.currency or old_currency,
                        change_type="website_sync", reason="Price changed on the website (applied from scan review)",
                        actor_type="system", actor_label="Website sync"))
                counts["kept_merchant_edits"] += len(skipped)
                product.version += 1
                if product.approval_status == "approved" and any(not c["manual_override"] for c in s.changes):
                    product.approval_status = "needs_review"  # changed facts need a fresh approval
                    product.publication_status = "unpublished"
                await _replace_evidence(session, product, s.data or {}, scan.id, s.page_id, skipped)
                counts["updated"] += 1
            product.last_scan_id = scan.id
            product.source_page_id = s.page_id or product.source_page_id
        elif s.change_type == "removed" and s.product_id:
            product = await session.get(Product, s.product_id)
            if product is not None:
                product.is_removed = True
                product.removed_at = _now()
                product.publication_status = "unpublished"
                counts["removed"] += 1
        s.decision = "applied"
    scan.applied_at = _now()
    scan.status = "completed"
    await session.flush()
    await revalidate_business(session, scan.business_id, scan.id)
    from app.services.commerce.sync import finalize_scan_sync

    scan.change_summary = {**(scan.change_summary or {}), "applied": counts}
    await finalize_scan_sync(session, scan)
    return counts
