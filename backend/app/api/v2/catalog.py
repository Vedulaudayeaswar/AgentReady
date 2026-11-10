"""Public catalog API v2 (stable schema ``agentready-catalog/2.0``).

Exposes only products that are approved AND published AND active/discontinued (never drafts, archived,
deleted or website-removed products). Never exposes cost prices, reserved stock, stock locations,
thresholds, merchant notes, credentials or other merchants' data. Fields are gated by API-key scope:

* catalog:read   — products, categories, variants, feeds (required for everything)
* pricing:read   — selling prices (regular / sale / effective)
* inventory:read — available quantity (otherwise only a stock status)
* evidence:read  — source evidence for published values
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, catalog_auth, has_scope
from app.api.v1.products import contains_pattern
from app.core.errors import not_found
from app.db.session import get_db
from app.models import Category, Product, ProductEvidence, ProductVariant
from app.services.catalog.serialize import PUBLIC_LIFECYCLE
from app.services.commerce.money import aware, fmt, now_utc
from app.services.commerce.pricing import product_pricing, variant_effective_price

router = APIRouter(prefix="/catalog", tags=["catalog v2"])
SCHEMA = "agentready-catalog/2.0"
SORTS = {"name": asc(Product.name), "-name": desc(Product.name), "updated": asc(Product.updated_at),
         "-updated": desc(Product.updated_at), "price": asc(Product.price_amount), "-price": desc(Product.price_amount)}


def require(ctx: AuthContext, scope: str) -> None:
    if not has_scope(ctx, scope):
        raise HTTPException(403, f"This API key lacks the '{scope}' scope")


def public_filter(business_id: uuid.UUID):
    return and_(Product.business_id == business_id, Product.approval_status == "approved",
                Product.publication_status == "published", Product.lifecycle_status.in_(PUBLIC_LIFECYCLE),
                Product.deleted_at.is_(None), Product.is_removed.is_(False))


def _variant(v: ProductVariant, p: Product, ctx: AuthContext) -> dict:
    out = {"id": str(v.id), "name": v.name, "sku": v.sku, "options": v.attributes or {}, "image": v.image_url,
           "availability": v.availability or p.availability, "stock_status": v.stock_status}
    if has_scope(ctx, "pricing:read"):
        eff, status = variant_effective_price(v, p)
        regular = v.price_amount if v.price_amount is not None else p.price_amount
        out["price"] = {"currency": v.currency or p.currency, "regular": fmt(regular), "effective": fmt(eff), "status": status}
    if has_scope(ctx, "inventory:read") and v.track_inventory:
        out["available_quantity"] = max(0, v.stock_on_hand - v.stock_reserved)
    return out


from app.services.distribution.public import is_private_attribute_key  # noqa: E402


def item(p: Product, ctx: AuthContext, evidence: list | None = None) -> dict:
    out = {
        "id": str(p.id), "version": p.version, "title": p.name, "description": p.description,
        "identifiers": {"sku": p.sku, "gtin": p.gtin, "mpn": p.mpn}, "brand": p.brand, "manufacturer": p.manufacturer,
        "product_type": p.product_type, "category": {"id": str(p.category_id) if p.category_id else None, "path": p.category},
        "url": p.product_url or p.source_url, "images": [i.get("url") for i in p.images or [] if i.get("url")],
        "attributes": [{"key": a.key, "name": a.label, "value": a.value, "unit": a.unit} for a in p.attributes
                       if not is_private_attribute_key(a.key)],
        "availability": p.availability, "stock_status": p.stock_status, "lifecycle": p.lifecycle_status,
        "weight": p.weight, "dimensions": p.dimensions,
        "variants": [_variant(v, p, ctx) for v in p.variants if v.archived_at is None and v.is_enabled],
        "seo": {"title": p.seo_title, "description": p.seo_description},
        "updated_at": aware(p.updated_at).isoformat() if p.updated_at else None,
    }
    if has_scope(ctx, "pricing:read"):
        pr = product_pricing(p, include_cost=False)
        out["price"] = {"currency": pr["currency"], "regular": pr["regular_price"], "sale": pr["sale_price"],
                        "effective": pr["effective_price"], "status": pr["price_status"],
                        "sale_starts_at": pr["sale_starts_at"], "sale_ends_at": pr["sale_ends_at"],
                        "tax_included": pr["tax_included"]}
    if has_scope(ctx, "inventory:read") and p.track_inventory:
        out["available_quantity"] = max(0, p.stock_on_hand - p.stock_reserved)
    if evidence is not None:
        out["evidence"] = evidence
    return out


async def _category_ids(db, business_id, category_id: str | None) -> list[uuid.UUID] | None:
    if not category_id:
        return None
    try:
        root = uuid.UUID(category_id)
    except ValueError:
        raise not_found("Category")
    cats = (await db.execute(select(Category).where(Category.business_id == business_id, Category.archived_at.is_(None)))).scalars().all()
    if root not in {c.id for c in cats}:
        raise not_found("Category")
    out, frontier = {root}, {root}
    while frontier:
        frontier = {c.id for c in cats if c.parent_id in frontier} - out
        out |= frontier
    return list(out)


def _envelope(ctx: AuthContext, items: list, total: int, page: int, page_size: int, **extra) -> dict:
    return {"schema": SCHEMA, "merchant": {"id": str(ctx.business.id), "name": ctx.business.name},
            "generated_at": now_utc().isoformat(), "items": items, "total": total, "page": page, "page_size": page_size,
            "has_more": page * page_size < total, **extra}


@router.get("/products")
async def products(
    q: str | None = None, category_id: str | None = None, brand: str | None = None, product_type: str | None = None,
    availability: str | None = None, updated_since: datetime | None = None,
    min_price: Decimal | None = Query(None, ge=0), max_price: Decimal | None = Query(None, ge=0),
    sort: str = Query("name", pattern="^-?(name|updated|price)$"),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db),
):
    require(ctx, "catalog:read")
    stmt = select(Product).where(public_filter(ctx.business_id))
    if q:
        like = contains_pattern(q)
        stmt = stmt.where(or_(func.lower(Product.name).like(like, escape="\\"), func.lower(Product.sku).like(like, escape="\\"),
                              func.lower(Product.brand).like(like, escape="\\")))
    ids = await _category_ids(db, ctx.business_id, category_id)
    if ids is not None:
        stmt = stmt.where(Product.category_id.in_(ids))
    if brand:
        stmt = stmt.where(Product.brand == brand)
    if product_type:
        stmt = stmt.where(Product.product_type == product_type)
    if availability:
        stmt = stmt.where(Product.availability.in_(availability.split(",")))
    if updated_since:
        stmt = stmt.where(Product.updated_at > updated_since)
    if (min_price is not None or max_price is not None) and not has_scope(ctx, "pricing:read"):
        require(ctx, "pricing:read")
    if min_price is not None:
        stmt = stmt.where(Product.price_amount >= min_price)
    if max_price is not None:
        stmt = stmt.where(Product.price_amount <= max_price)
    if sort.lstrip("-") == "price":
        require(ctx, "pricing:read")
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(SORTS[sort].nulls_last(), Product.id).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return _envelope(ctx, [item(p, ctx) for p in rows], total, page, page_size)


@router.get("/search")
async def search(q: str = Query(min_length=1, max_length=200), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                 ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db)):
    return await products(q=q, category_id=None, brand=None, product_type=None, availability=None, updated_since=None,
                          min_price=None, max_price=None, sort="name", page=page, page_size=page_size, ctx=ctx, db=db)


@router.get("/products/{product_id}")
async def product(product_id: str, ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db)):
    require(ctx, "catalog:read")
    try:
        pid = uuid.UUID(product_id)
    except ValueError:
        raise not_found("Product")
    p = (await db.execute(select(Product).where(public_filter(ctx.business_id), Product.id == pid))).scalar_one_or_none()
    if p is None:
        raise not_found("Product")
    evidence = None
    if has_scope(ctx, "evidence:read"):
        rows = (await db.execute(select(ProductEvidence).where(ProductEvidence.product_id == p.id,
                                                               ProductEvidence.business_id == ctx.business_id))).scalars().all()
        evidence = [{"field": e.field, "source_url": e.source_url, "source_text": e.source_value, "method": e.extraction_method,
                     "model": e.model_id, "verified_in_source": e.verified_in_source} for e in rows]
    return {"schema": SCHEMA, **item(p, ctx, evidence)}


@router.get("/products/{product_id}/variants")
async def variants(product_id: str, ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db)):
    data = await product(product_id, ctx, db)
    return {"schema": SCHEMA, "product_id": data["id"], "items": data["variants"]}


@router.get("/categories")
async def categories(ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db)):
    require(ctx, "catalog:read")
    cats = (await db.execute(select(Category).where(Category.business_id == ctx.business_id, Category.archived_at.is_(None))
                             .order_by(Category.path_key))).scalars().all()
    counts = dict((await db.execute(select(Product.category_id, func.count()).where(public_filter(ctx.business_id))
                                    .group_by(Product.category_id))).all())
    return {"schema": SCHEMA, "items": [{"id": str(c.id), "name": c.name, "path": c.path,
                                         "parent_id": str(c.parent_id) if c.parent_id else None,
                                         "product_count": counts.get(c.id, 0)} for c in cats]}


@router.get("/availability")
async def availability(updated_since: datetime | None = None, page: int = Query(1, ge=1), page_size: int = Query(200, ge=1, le=1000),
                       ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db)):
    """Compact availability feed: one row per product/variant."""
    require(ctx, "catalog:read")
    stmt = select(Product).where(public_filter(ctx.business_id))
    if updated_since:
        stmt = stmt.where(Product.updated_at > updated_since)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Product.id).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    qty = has_scope(ctx, "inventory:read")
    items = []
    for p in rows:
        row = {"id": str(p.id), "sku": p.sku, "availability": p.availability, "stock_status": p.stock_status,
               "updated_at": aware(p.updated_at).isoformat()}
        if qty and p.track_inventory:
            row["available_quantity"] = max(0, p.stock_on_hand - p.stock_reserved)
        row["variants"] = [{k: v for k, v in _variant(v, p, ctx).items() if k in ("id", "sku", "availability", "stock_status",
                                                                                   "available_quantity")}
                           for v in p.variants if v.archived_at is None and v.is_enabled]
        items.append(row)
    return _envelope(ctx, items, total, page, page_size)


@router.get("/pricing")
async def pricing(updated_since: datetime | None = None, page: int = Query(1, ge=1), page_size: int = Query(200, ge=1, le=1000),
                  ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db)):
    require(ctx, "catalog:read")
    require(ctx, "pricing:read")
    stmt = select(Product).where(public_filter(ctx.business_id))
    if updated_since:
        stmt = stmt.where(Product.updated_at > updated_since)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Product.id).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    items = [{"id": str(p.id), "sku": p.sku, "price": item(p, ctx)["price"],
              "variants": [{"id": str(v.id), "sku": v.sku, "price": _variant(v, p, ctx)["price"]}
                           for v in p.variants if v.archived_at is None and v.is_enabled]} for p in rows]
    return _envelope(ctx, items, total, page, page_size)


@router.get("/inventory")
async def inventory(page: int = Query(1, ge=1), page_size: int = Query(200, ge=1, le=1000),
                    ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db)):
    require(ctx, "catalog:read")
    require(ctx, "inventory:read")
    return await availability(updated_since=None, page=page, page_size=page_size, ctx=ctx, db=db)


@router.get("/feed")
async def feed(type: str = Query("full", pattern="^(full|incremental|category|availability)$"),
               updated_since: datetime | None = None, category_id: str | None = None,
               page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
               ctx: AuthContext = Depends(catalog_auth), db: AsyncSession = Depends(get_db)):
    """Catalog feeds.

    * full         — every published product
    * incremental  — products changed since ``updated_since`` plus ``removed`` tombstones for products that were
                     published before and are no longer (unpublished, archived, deleted). Use ``next_updated_since``
                     from the response as the next cursor.
    * category     — published products in ``category_id`` (and its subcategories)
    * availability — compact stock/availability rows
    """
    require(ctx, "catalog:read")
    server_time = now_utc()
    if type == "availability":
        return {**await availability(updated_since=updated_since, page=page, page_size=page_size, ctx=ctx, db=db), "feed_type": type}
    if type == "incremental" and updated_since is None:
        raise HTTPException(422, "updated_since is required for an incremental feed")
    if type == "category" and not category_id:
        raise HTTPException(422, "category_id is required for a category feed")
    stmt = select(Product).where(public_filter(ctx.business_id))
    if type == "incremental":
        stmt = stmt.where(Product.updated_at > updated_since)
    ids = await _category_ids(db, ctx.business_id, category_id) if type == "category" else None
    if ids is not None:
        stmt = stmt.where(Product.category_id.in_(ids))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Product.updated_at, Product.id).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    extra = {"feed_type": type, "server_time": server_time.isoformat()}
    if type == "incremental":
        removed = (await db.execute(select(Product.id, Product.updated_at, Product.lifecycle_status, Product.deleted_at).where(
            Product.business_id == ctx.business_id, Product.published_at.is_not(None), Product.updated_at > updated_since,
            ~public_filter(ctx.business_id)).limit(5000))).all()
        extra["removed"] = [{"id": str(pid), "reason": "deleted" if deleted else ("archived" if lc == "archived" else "unpublished"),
                             "at": aware(at).isoformat()} for pid, at, lc, deleted in removed]
        extra["next_updated_since"] = server_time.isoformat()
    return _envelope(ctx, [item(p, ctx) for p in rows], total, page, page_size, **extra)
