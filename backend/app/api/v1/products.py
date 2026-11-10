from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import Select, and_, asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.core.errors import AppError, not_found
from app.db.session import get_db
from app.models import (
    AuditLog,
    Business,
    CrawledPage,
    InventoryTransaction,
    PriceHistory,
    Product,
    ProductAttribute,
    ProductEvidence,
    ProductVariant,
    ValidationIssue,
)
from app.schemas.api import BulkApproveIn
from app.schemas.commerce import ProductCreate, ProductUpdate
from app.services.catalog.serialize import evidence_dict, product_dict
from app.services.commerce import lifecycle
from app.services.commerce.audit import actor_from_ctx, jsonable, record
from app.services.commerce.catalog_rules import (
    assign_category,
    canonical_key,
    check_sku,
    get_category,
    merchant_alias_map,
)
from app.services.commerce.inventory import change_stock, refresh_product_status
from app.services.commerce.money import aware, fmt, now_utc
from app.services.commerce.pricing import set_pricing
from app.services.normalization.normalize import attribute_key, split_value_unit
from app.services.validation.validator import revalidate_business

router = APIRouter(prefix="/products", tags=["products"])

SORTS = {
    "updated_desc": desc(Product.updated_at), "updated_asc": asc(Product.updated_at),
    "created_desc": desc(Product.created_at), "created_asc": asc(Product.created_at),
    "price_asc": asc(Product.price_amount), "price_desc": desc(Product.price_amount),
    "name_asc": asc(Product.name), "name_desc": desc(Product.name),
    "sku_asc": asc(Product.sku), "brand_asc": asc(Product.brand),
    "stock_asc": asc(Product.stock_on_hand - Product.stock_reserved), "stock_desc": desc(Product.stock_on_hand - Product.stock_reserved),
    "quality_asc": asc(Product.quality_score), "quality_desc": desc(Product.quality_score),
}
STOCK_STATUSES = ("in_stock", "low_stock", "out_of_stock", "backorder", "untracked")


def contains_pattern(q: str) -> str:
    """Case-insensitive 'contains' LIKE pattern where % and _ in the query are literal characters."""
    escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _csv(v: str | None) -> list[str]:
    return [x for x in (v or "").split(",") if x]


def filtered_products(
    business_id: uuid.UUID, q: str | None = None, category: str | None = None, quality_status: str | None = None,
    approval_status: str | None = None, website_id: str | None = None, include_removed: bool = False, *,
    brand: str | None = None, publication_status: str | None = None, lifecycle_status: str | None = None,
    stock_status: str | None = None, product_type: str | None = None, origin: str | None = None,
    category_ids: list[uuid.UUID] | None = None, price_min: Decimal | None = None, price_max: Decimal | None = None,
    include_archived: bool = True,
) -> Select:
    stmt = select(Product).where(Product.business_id == business_id)
    lifecycles = _csv(lifecycle_status)
    if "deleted" in lifecycles:
        stmt = stmt.where(Product.deleted_at.is_not(None))
        lifecycles = [x for x in lifecycles if x != "deleted"]
    else:
        stmt = stmt.where(Product.deleted_at.is_(None))
    if lifecycles:
        stmt = stmt.where(Product.lifecycle_status.in_(lifecycles))
    elif not include_archived:
        stmt = stmt.where(Product.lifecycle_status != "archived")
    if not include_removed:
        stmt = stmt.where(Product.is_removed.is_(False))
    if q:
        like = contains_pattern(q)
        variant_sku = select(ProductVariant.product_id).where(func.lower(ProductVariant.sku).like(like, escape="\\"))
        stmt = stmt.where(or_(*(func.lower(col).like(like, escape="\\")
                                for col in (Product.name, Product.sku, Product.brand, Product.gtin)),
                              Product.id.in_(variant_sku)))
    if category:
        stmt = stmt.where(Product.category == category)
    if category_ids:
        stmt = stmt.where(Product.category_id.in_(category_ids))
    for col, val in ((Product.quality_status, quality_status), (Product.approval_status, approval_status),
                     (Product.publication_status, publication_status), (Product.stock_status, stock_status),
                     (Product.origin, origin)):
        if val:
            stmt = stmt.where(col.in_(_csv(val)))
    if brand:
        stmt = stmt.where(Product.brand.in_(_csv(brand))) if brand != "__none__" else stmt.where(Product.brand.is_(None))
    if product_type:
        stmt = stmt.where(Product.product_type.in_(_csv(product_type)))
    if price_min is not None:
        stmt = stmt.where(Product.price_amount >= price_min)
    if price_max is not None:
        stmt = stmt.where(Product.price_amount <= price_max)
    if website_id:
        try:
            stmt = stmt.where(Product.website_id == uuid.UUID(website_id))
        except ValueError:
            raise AppError(422, "invalid_id", "Invalid website id")
    return stmt


async def get_owned_product(db: AsyncSession, ctx: AuthContext, product_id: str, include_deleted: bool = False) -> Product:
    try:
        pid = uuid.UUID(product_id)
    except ValueError:
        raise not_found("Product")
    q = select(Product).where(Product.id == pid, Product.business_id == ctx.business_id)
    p = (await db.execute(q)).scalar_one_or_none()
    if p is None or (p.deleted_at is not None and not include_deleted):
        raise not_found("Product")
    return p


@router.get("")
async def list_products(
    q: str | None = None, category: str | None = None, category_id: str | None = None, quality_status: str | None = None,
    approval_status: str | None = None, publication_status: str | None = None, lifecycle_status: str | None = None,
    stock_status: str | None = None, brand: str | None = None, product_type: str | None = None, origin: str | None = None,
    price_min: Decimal | None = Query(None, ge=0), price_max: Decimal | None = Query(None, ge=0),
    website_id: str | None = None, include_removed: bool = False, include_archived: bool = False,
    sort: str = "updated_desc", page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
    ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db),
):
    from app.services.commerce.catalog_rules import descendants

    cat_ids = None
    if category_id:
        if category_id == "__none__":
            cat_ids = None
        else:
            cat = await get_category(db, ctx.business_id, category_id)
            cat_ids = [cat.id, *(c.id for c in await descendants(db, ctx.business_id, cat))]
    stmt = filtered_products(ctx.business_id, q, category, quality_status, approval_status, website_id, include_removed,
                             brand=brand, publication_status=publication_status, lifecycle_status=lifecycle_status,
                             stock_status=stock_status, product_type=product_type, origin=origin, category_ids=cat_ids,
                             price_min=price_min, price_max=price_max, include_archived=include_archived)
    if category_id == "__none__":
        stmt = stmt.where(Product.category_id.is_(None))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    order = SORTS.get(sort, SORTS["updated_desc"])
    rows = (await db.execute(stmt.order_by(order.nulls_last(), Product.id).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    ids = [p.id for p in rows]
    issue_counts = {}
    if ids:
        issue_counts = dict((await db.execute(
            select(ValidationIssue.product_id, func.count()).where(
                ValidationIssue.product_id.in_(ids), ValidationIssue.status == "open",
                ValidationIssue.severity.in_(("error", "warning"))).group_by(ValidationIssue.product_id)
        )).all())
    items = [{**product_dict(p, ctx.business), "open_issues": issue_counts.get(p.id, 0)} for p in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/categories")
async def categories(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Distinct category texts on products (Phase 1 shape; managed categories live at /categories)."""
    rows = (await db.execute(
        select(Product.category, func.count()).where(Product.business_id == ctx.business_id, Product.is_removed.is_(False),
                                                     Product.deleted_at.is_(None), Product.category.is_not(None))
        .group_by(Product.category).order_by(func.count().desc())
    )).all()
    return {"items": [{"category": c, "count": n} for c, n in rows]}


@router.get("/facets")
async def facets(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    base = (Product.business_id == ctx.business_id, Product.is_removed.is_(False), Product.deleted_at.is_(None))

    async def group(col):
        return [{"value": v, "count": n} for v, n in (await db.execute(
            select(col, func.count()).where(*base).group_by(col).order_by(func.count().desc()).limit(200))).all()]

    return {"brands": await group(Product.brand), "product_types": await group(Product.product_type),
            "lifecycle": await group(Product.lifecycle_status), "publication": await group(Product.publication_status),
            "stock": await group(Product.stock_status), "origins": await group(Product.origin)}


def _evidence(db, p: Product, field: str, value, source: str = "Entered by merchant") -> None:
    db.add(ProductEvidence(product_id=p.id, business_id=p.business_id, field=field,
                           value=jsonable(value) if not isinstance(value, list) else jsonable(value),
                           source_value=source, source_type="merchant_entry", extraction_method="merchant"))


async def _attributes(db: AsyncSession, business_id, items) -> list[ProductAttribute]:
    aliases = await merchant_alias_map(db, business_id)
    out, seen = [], set()
    for i, a in enumerate(items or []):
        key = canonical_key(a.key or attribute_key(a.label), aliases)
        if key in seen:
            raise AppError(422, "duplicate_attribute", f"Attribute '{a.label}' is listed twice")
        seen.add(key)
        value, unit = (a.value, a.unit) if a.unit else split_value_unit(a.value)
        out.append(ProductAttribute(key=key, label=a.label, value=value, unit=a.unit or unit, raw_value=a.value,
                                    source="merchant", position=i))
    return out


def _safe_url(u: str | None, what: str) -> str | None:
    from urllib.parse import urlsplit

    if not u:
        return None
    p = urlsplit(u.strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        raise AppError(422, "invalid_url", f"{what} must be an http(s) URL")
    return u.strip()


@router.post("", status_code=201)
async def create_product(body: ProductCreate, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Create a product manually (no website needed). Only the fields provided are stored — nothing is inferred."""
    actor = actor_from_ctx(ctx)
    business: Business = ctx.business
    await check_sku(db, business, body.sku)
    category = await get_category(db, ctx.business_id, body.category_id) if body.category_id else None
    images = [{"url": _safe_url(i.url, "Image URL"), "alt": i.alt} for i in body.images]
    p = Product(
        business_id=ctx.business_id, match_key=f"manual:{uuid.uuid4()}", origin="manual",
        name=body.name.strip(), description=body.description, brand=body.brand, manufacturer=body.manufacturer, sku=body.sku,
        gtin=body.gtin, mpn=body.mpn, material=body.material, product_type=body.product_type,
        category_id=category.id if category else None, category=category.path[:300] if category else None,
        availability=body.availability, product_url=_safe_url(body.product_url, "Product URL"), images=images,
        lifecycle_status=body.lifecycle_status, approval_status="needs_review", publication_status="unpublished",
        shipping=body.shipping.model_dump(exclude_none=True) if body.shipping else {},
        weight=body.weight.model_dump() if body.weight else None, dimensions=body.dimensions.model_dump() if body.dimensions else None,
        seo_title=body.seo_title, seo_description=body.seo_description,
        track_inventory=body.track_inventory, low_stock_threshold=body.low_stock_threshold,
        backorder_policy=body.backorder_policy, inventory_location=body.inventory_location,
        attributes=await _attributes(db, ctx.business_id, body.attributes), variants=[], extra={}, manual_overrides=[],
    )
    db.add(p)
    await db.flush()
    await set_pricing(db, p, actor=actor, change_type="product_created", regular=body.regular_price, sale=body.sale_price,
                      cost=body.cost_price, currency=body.currency, tax_class=body.tax_class, tax_included=body.tax_included,
                      default_currency=business.default_currency, mark_override=False, audit=False)
    p.version = 1
    for f in ("name", "description", "brand", "manufacturer", "sku", "gtin", "mpn", "material", "availability", "product_url"):
        if getattr(p, f):
            _evidence(db, p, f, getattr(p, f))
    if p.price_amount is not None:
        _evidence(db, p, "price", fmt(p.price_amount))
    if images:
        _evidence(db, p, "images", images)
    for a in p.attributes:
        _evidence(db, p, f"attributes.{a.key}", a.raw_value)
    if body.track_inventory:
        await db.flush()
        await refresh_product_status(db, p, business)
        if body.stock_quantity is not None:
            await change_stock(db, p, actor=actor, operation="set", quantity=body.stock_quantity,
                               reason="Initial stock", transaction_type="initial")
    record(db, ctx.business_id, actor, "product.created", "product", p.id, f"Product '{p.name}' created manually",
           {"sku": p.sku, "lifecycle_status": p.lifecycle_status, "regular_price": fmt(p.price_amount), "currency": p.currency})
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    await db.commit()
    await db.refresh(p)
    return product_dict(p, business)


@router.get("/{product_id}")
async def get_product(product_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, product_id, include_deleted=True)
    evidence = (await db.execute(select(ProductEvidence).where(
        ProductEvidence.product_id == p.id, ProductEvidence.business_id == ctx.business_id
    ).order_by(ProductEvidence.field, ProductEvidence.created_at))).scalars().all()
    issues = (await db.execute(select(ValidationIssue).where(
        ValidationIssue.product_id == p.id, ValidationIssue.business_id == ctx.business_id
    ).order_by(ValidationIssue.status, ValidationIssue.severity))).scalars().all()
    page = await db.get(CrawledPage, p.source_page_id) if p.source_page_id else None
    from app.api.v1.validation import issue_dict
    from app.models import SyncConflict

    open_conflicts = (await db.execute(select(func.count()).select_from(SyncConflict).where(
        SyncConflict.product_id == p.id, SyncConflict.status == "open"))).scalar_one()
    return {
        **product_dict(p, ctx.business),
        "evidence": [evidence_dict(e) for e in evidence],
        "issues": [issue_dict(i) for i in issues],
        "open_conflicts": open_conflicts,
        "source_values": p.source_values,
        "source_page": {"id": str(page.id), "url": page.url, "title": page.title, "page_type": page.page_type,
                        "fetched_at": page.fetched_at.isoformat(), "text_excerpt": (page.text_excerpt or "")[:8000],
                        "signals": page.signals} if page and page.business_id == ctx.business_id else None,
    }


SIMPLE_FIELDS = ("name", "sku", "brand", "manufacturer", "category", "description", "availability", "product_url", "gtin",
                 "mpn", "material", "product_type", "seo_title", "seo_description")


@router.patch("/{product_id}")
async def update_product(product_id: str, body: ProductUpdate, ctx: AuthContext = Depends(current_auth),
                         db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, product_id)
    actor = actor_from_ctx(ctx)
    fields = body.model_fields_set - {"approval_status", "lifecycle_status", "publication_status"}
    overrides = set(p.manual_overrides or [])
    before = {f: getattr(p, f, None) for f in fields if f not in ("images", "attributes", "category_id", "price_amount")}
    if "sku" in fields and body.sku != p.sku:
        await check_sku(db, ctx.business, body.sku, exclude_product=p.id)
    if body.name is not None and not body.name.strip():
        raise AppError(422, "invalid_name", "Name cannot be empty")
    content_changed = False
    for f in fields:
        v = getattr(body, f)
        if f == "images":
            p.images = [{"url": _safe_url(i.url, "Image URL"), "alt": i.alt} for i in (v or [])]
        elif f == "attributes":
            p.attributes = await _attributes(db, ctx.business_id, v)
        elif f == "category_id":
            await assign_category(db, p, await get_category(db, ctx.business_id, v) if v else None, actor)
            continue
        elif f in ("price_amount", "currency"):
            continue  # handled together below (pricing rules + history)
        elif f in ("shipping",):
            p.shipping = v.model_dump(exclude_none=True) if v else {}
        elif f in ("weight", "dimensions"):
            setattr(p, f, v.model_dump() if v else None)
        elif f == "product_url":
            p.product_url = _safe_url(v, "Product URL")
        elif f in SIMPLE_FIELDS:
            setattr(p, f, v)
        if f not in ("seo_title", "seo_description", "product_type", "shipping"):
            override = f
            overrides.add(override)
            _evidence(db, p, override, v if not isinstance(v, list) else [x.model_dump() for x in v], "Edited by merchant")
        content_changed = True
    if {"price_amount", "currency"} & fields:
        kwargs = {}
        if "price_amount" in fields:
            kwargs["regular"] = body.price_amount
        if "currency" in fields:
            kwargs["currency"] = body.currency.upper() if body.currency else None
        await set_pricing(db, p, actor=actor, change_type="manual", default_currency=ctx.business.default_currency, **kwargs)
        # a value the merchant explicitly set is merchant-managed even if it equals the extracted one (Phase 1 contract)
        overrides = set(p.manual_overrides or []) | overrides | {"price" if f == "price_amount" else f
                                                                 for f in {"price_amount", "currency"} & fields}
        p.manual_overrides = sorted(overrides)
    if content_changed:
        p.manual_overrides = sorted(overrides | set(p.manual_overrides or []))
        p.version += 1
        p.updated_at = now_utc()
        after = {f: getattr(p, f, None) for f in before}
        record(db, ctx.business_id, actor, "product.updated", "product", p.id, f"'{p.name}' updated: {', '.join(sorted(fields))}",
               {k: {"from": jsonable(before[k]), "to": jsonable(after[k])} for k in before if before[k] != after[k]})
    if body.lifecycle_status is not None:
        lifecycle.set_lifecycle(db, p, body.lifecycle_status, actor)
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    if body.approval_status is not None:
        if body.approval_status == "approved":
            try:
                lifecycle.check_approve(p, await lifecycle.blocking_errors(db, p.id))
            except lifecycle.LifecycleError as e:
                await db.rollback()
                raise AppError(409, e.code, str(e))
        lifecycle.set_approval(db, p, body.approval_status, actor)
    if body.publication_status is not None:
        try:
            lifecycle.set_publication(db, p, body.publication_status, actor)
        except lifecycle.LifecycleError as e:
            await db.rollback()
            raise AppError(409, e.code, str(e))
    await db.commit()
    await db.refresh(p)
    return product_dict(p, ctx.business)


@router.post("/bulk-approval")
async def bulk_approval(body: BulkApproveIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Phase 1 endpoint (kept for compatibility). New clients use /bulk-operations."""
    try:
        ids = [uuid.UUID(i) for i in body.product_ids]
    except ValueError:
        raise AppError(422, "invalid_id", "Invalid product id")
    actor = actor_from_ctx(ctx)
    products = (await db.execute(select(Product).where(Product.business_id == ctx.business_id, Product.id.in_(ids),
                                                       Product.deleted_at.is_(None)))).scalars().all()
    blocked = set()
    if body.approval_status == "approved":
        blocked = {pid for (pid,) in (await db.execute(select(ValidationIssue.product_id).where(
            ValidationIssue.product_id.in_(ids), ValidationIssue.status == "open", ValidationIssue.severity == "error"
        ))).all()}
    updated = 0
    for p in products:
        if p.id in blocked:
            continue
        lifecycle.set_approval(db, p, body.approval_status, actor)
        updated += 1
    await db.commit()
    return {"updated": updated, "skipped_blocking_issues": [str(b) for b in blocked]}


@router.post("/{product_id}/archive")
async def archive_product(product_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, product_id)
    lifecycle.set_lifecycle(db, p, "archived", actor_from_ctx(ctx))
    await db.commit()
    return product_dict(p, ctx.business)


@router.post("/{product_id}/restore")
async def restore_product(product_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, product_id, include_deleted=True)
    lifecycle.restore(db, p, actor_from_ctx(ctx))
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    await db.commit()
    return product_dict(p, ctx.business)


@router.delete("/{product_id}", status_code=204)
async def delete_product(product_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Soft delete: the product leaves the catalog and feeds but its history is kept and it can be restored."""
    p = await get_owned_product(db, ctx, product_id)
    lifecycle.soft_delete(db, p, actor_from_ctx(ctx))
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    await db.commit()
    return Response(status_code=204)


@router.get("/{product_id}/history")
async def product_history(product_id: str, limit: int = Query(200, ge=1, le=500), ctx: AuthContext = Depends(current_auth),
                          db: AsyncSession = Depends(get_db)):
    """Merged timeline: audit events, price changes and inventory transactions for one product."""
    p = await get_owned_product(db, ctx, product_id, include_deleted=True)
    audit = (await db.execute(select(AuditLog).where(
        AuditLog.business_id == ctx.business_id, AuditLog.entity_type == "product", AuditLog.entity_id == str(p.id)
    ).order_by(AuditLog.created_at.desc()).limit(limit))).scalars().all()
    prices = (await db.execute(select(PriceHistory).where(
        PriceHistory.business_id == ctx.business_id, PriceHistory.product_id == p.id
    ).order_by(PriceHistory.created_at.desc()).limit(limit))).scalars().all()
    stock = (await db.execute(select(InventoryTransaction).where(
        InventoryTransaction.business_id == ctx.business_id, InventoryTransaction.product_id == p.id
    ).order_by(InventoryTransaction.created_at.desc()).limit(limit))).scalars().all()
    events = [{"kind": "audit", "at": aware(a.created_at).isoformat(), "action": a.action, "summary": a.summary,
               "actor": a.actor_label, "actor_type": a.actor_type, "changes": a.changes} for a in audit]
    events += [{"kind": "price", "at": aware(h.created_at).isoformat(), "action": f"price.{h.price_type}",
                "summary": f"{h.price_type.title()} price {fmt(h.old_amount) or '—'} → {fmt(h.new_amount) or '—'} {h.currency or ''}".strip(),
                "actor": h.actor_label, "actor_type": h.actor_type, "change_type": h.change_type} for h in prices]
    events += [{"kind": "inventory", "at": aware(t.created_at).isoformat(), "action": f"inventory.{t.transaction_type}",
                "summary": f"{t.quantity_field.replace('_', ' ').title()} {t.previous_quantity} → {t.new_quantity} ({t.adjustment:+d})"
                           + (f" — {t.reason}" if t.reason else ""),
                "actor": t.actor_label, "actor_type": t.actor_type} for t in stock]
    events.sort(key=lambda e: e["at"], reverse=True)
    return {"items": events[:limit]}
