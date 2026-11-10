"""ORM -> API/export dictionaries."""

from __future__ import annotations

from decimal import Decimal

from app.models import Business, Product, ProductEvidence

PUBLIC_LIFECYCLE = ("active", "discontinued")


def money(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


def is_public(p: Product) -> bool:
    """Visible through the catalog API / feeds: approved AND published AND active, not deleted/removed."""
    return (p.approval_status == "approved" and p.publication_status == "published"
            and p.lifecycle_status in PUBLIC_LIFECYCLE and p.deleted_at is None and not p.is_removed)


def live_variants(p: Product):
    return [v for v in p.variants if v.archived_at is None]


def product_dict(p: Product, business: Business | None = None, include_private: bool = True) -> dict:
    from app.services.commerce.catalog_rules import variant_dict
    from app.services.commerce.inventory import inventory_dict
    from app.services.commerce.pricing import product_pricing

    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "id": str(p.id),
        "name": p.name,
        "sku": p.sku,
        "brand": p.brand,
        "manufacturer": p.manufacturer,
        "category": p.category,
        "category_inferred": p.category_inferred,
        "description": p.description,
        "price": {"amount": money(p.price_amount), "currency": p.currency} if p.price_amount is not None else None,
        "availability": p.availability,
        "product_url": p.product_url,
        "source_url": p.source_url,
        "gtin": p.gtin,
        "mpn": p.mpn,
        "material": p.material,
        "dimensions": p.dimensions,
        "weight": p.weight,
        "images": p.images or [],
        "attributes": [
            {"key": a.key, "label": a.label, "value": a.value, "unit": a.unit, "raw_value": a.raw_value, "source": a.source}
            for a in p.attributes
        ],
        "variants": [variant_dict(v, p, business, include_private) for v in sorted(p.variants, key=lambda v: (v.position, v.name or ""))
                     if include_private or (v.archived_at is None and v.is_enabled)],
        "quality_status": p.quality_status,
        "quality_score": p.quality_score,
        "approval_status": p.approval_status,
        "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        "manual_overrides": p.manual_overrides or [],
        "version": p.version,
        "is_removed": p.is_removed,
        "website_id": str(p.website_id) if p.website_id else None,
        "last_scan_id": str(p.last_scan_id) if p.last_scan_id else None,
        "flags": (p.extra or {}).get("flags", []),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        # ---- Phase 2
        "origin": p.origin,
        "product_type": p.product_type,
        "lifecycle_status": p.lifecycle_status,
        "publication_status": p.publication_status,
        "is_public": is_public(p),
        "published_at": iso(p.published_at),
        "archived_at": iso(p.archived_at),
        "deleted_at": iso(p.deleted_at),
        "category_id": str(p.category_id) if p.category_id else None,
        "pricing": product_pricing(p, include_cost=include_private),
        "stock_status": p.stock_status,
        "inventory": inventory_dict(p, business, include_private),
        "shipping": p.shipping or {},
        "seo_title": p.seo_title,
        "seo_description": p.seo_description,
    }


def evidence_dict(e: ProductEvidence) -> dict:
    return {
        "id": str(e.id),
        "field": e.field,
        "value": e.value,
        "source_value": e.source_value,
        "source_url": e.source_url,
        "source_fragment": e.source_fragment,
        "source_type": e.source_type,
        "extraction_method": e.extraction_method,
        "inferred": e.inferred,
        "model_id": e.model_id,
        "prompt_version": e.prompt_version,
        "confidence": e.confidence,
        "verified_in_source": e.verified_in_source,
        "page_id": str(e.page_id) if e.page_id else None,
        "scan_id": str(e.scan_id) if e.scan_id else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }
