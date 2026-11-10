"""Pricing and inventory management endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, current_auth
from app.api.v1.products import get_owned_product
from app.core.errors import AppError, not_found
from app.db.session import get_db
from app.models import (
    InventoryTransaction,
    PriceHistory,
    Product,
    ProductVariant,
    ScheduledPriceChange,
    live_products,
)
from app.schemas.commerce import (
    InventoryAdjustIn,
    InventoryBulkIn,
    InventorySettingsIn,
    PricingBulkIn,
    PricingIn,
    ScheduleIn,
)
from app.services.commerce import bulk
from app.services.commerce.audit import actor_from_ctx, record
from app.services.commerce.inventory import change_stock, inventory_dict, update_settings
from app.services.commerce.money import aware, fmt, now_utc
from app.services.commerce.pricing import product_pricing, schedule_change, scheduled_dict, set_pricing
from app.services.validation.validator import revalidate_business

router = APIRouter(tags=["pricing & inventory"])


def _uuid(v: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(v)
    except (ValueError, TypeError):
        raise not_found(what)


async def owned_variant(db: AsyncSession, product: Product, variant_id: str | None) -> ProductVariant | None:
    if not variant_id:
        return None
    v = await db.get(ProductVariant, _uuid(variant_id, "Variant"))
    if v is None or v.product_id != product.id:
        raise not_found("Variant")
    return v


def history_dict(h: PriceHistory, name: str | None = None) -> dict:
    return {"id": str(h.id), "product_id": str(h.product_id), "product_name": name,
            "variant_id": str(h.variant_id) if h.variant_id else None, "price_type": h.price_type,
            "old_amount": fmt(h.old_amount), "new_amount": fmt(h.new_amount), "currency": h.currency,
            "change_type": h.change_type, "reason": h.reason, "actor": h.actor_label, "actor_type": h.actor_type,
            "bulk_operation_id": str(h.bulk_operation_id) if h.bulk_operation_id else None,
            "created_at": aware(h.created_at).isoformat()}


def tx_dict(t: InventoryTransaction, name: str | None = None) -> dict:
    return {"id": str(t.id), "product_id": str(t.product_id), "product_name": name,
            "variant_id": str(t.variant_id) if t.variant_id else None, "transaction_type": t.transaction_type,
            "quantity_field": t.quantity_field, "previous_quantity": t.previous_quantity, "adjustment": t.adjustment,
            "new_quantity": t.new_quantity, "reason": t.reason, "actor": t.actor_label, "actor_type": t.actor_type,
            "related_operation": t.related_operation,
            "bulk_operation_id": str(t.bulk_operation_id) if t.bulk_operation_id else None,
            "created_at": aware(t.created_at).isoformat()}


# ---------------------------------------------------------------- pricing
@router.get("/products/{product_id}/pricing")
async def get_pricing(product_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, product_id)
    scheduled = (await db.execute(select(ScheduledPriceChange).where(
        ScheduledPriceChange.product_id == p.id, ScheduledPriceChange.business_id == ctx.business_id,
        ScheduledPriceChange.status == "scheduled").order_by(ScheduledPriceChange.effective_at))).scalars().all()
    return {**product_pricing(p), "scheduled_changes": [scheduled_dict(c, p.name) for c in scheduled]}


@router.put("/products/{product_id}/pricing")
async def put_pricing(product_id: str, body: PricingIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, product_id)
    variant = await owned_variant(db, p, body.variant_id)
    fields = body.model_fields_set - {"reason", "variant_id"}
    mapping = {"regular_price": "regular", "sale_price": "sale", "cost_price": "cost", "currency": "currency",
               "sale_starts_at": "sale_starts_at", "sale_ends_at": "sale_ends_at", "tax_class": "tax_class",
               "tax_included": "tax_included"}
    kwargs = {mapping[f]: getattr(body, f) for f in fields}
    if "regular" in kwargs and kwargs["regular"] == 0:
        kwargs["regular"] = None
    if "sale" in kwargs and kwargs["sale"] == 0:
        kwargs["sale"] = None
    if variant is not None and {"cost", "sale_starts_at", "sale_ends_at", "tax_class", "tax_included"} & set(kwargs):
        raise AppError(422, "product_level_field", "Cost, tax and the sale window are set on the product, not per variant")
    changes = await set_pricing(db, p, actor=actor_from_ctx(ctx), reason=body.reason, variant=variant,
                                default_currency=ctx.business.default_currency, **kwargs)
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    await db.commit()
    await db.refresh(p)
    return {**product_pricing(p), "changed": sorted(changes)}


@router.get("/products/{product_id}/price-history")
async def price_history(product_id: str, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                        ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, product_id, include_deleted=True)
    q = select(PriceHistory).where(PriceHistory.business_id == ctx.business_id, PriceHistory.product_id == p.id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(PriceHistory.created_at.desc(), PriceHistory.id).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    return {"items": [history_dict(h, p.name) for h in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/pricing/history")
async def all_price_history(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                            change_type: str | None = None, ctx: AuthContext = Depends(current_auth),
                            db: AsyncSession = Depends(get_db)):
    q = select(PriceHistory, Product.name).join(Product, Product.id == PriceHistory.product_id).where(
        PriceHistory.business_id == ctx.business_id)
    if change_type:
        q = q.where(PriceHistory.change_type == change_type)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(PriceHistory.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    return {"items": [history_dict(h, n) for h, n in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/pricing/overview")
async def pricing_overview(ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    products = (await db.execute(select(Product).where(*live_products(ctx.business_id)))).scalars().all()
    now = now_utc()
    statuses: dict[str, int] = {}
    currencies: dict[str, int] = {}
    for p in products:
        st = product_pricing(p, at=now)["price_status"]
        statuses[st] = statuses.get(st, 0) + 1
        if p.currency:
            currencies[p.currency] = currencies.get(p.currency, 0) + 1
    scheduled = (await db.execute(select(func.count()).select_from(ScheduledPriceChange).where(
        ScheduledPriceChange.business_id == ctx.business_id, ScheduledPriceChange.status == "scheduled"))).scalar_one()
    return {"products": len(products), "price_status": statuses, "currencies": currencies, "scheduled_changes": scheduled,
            "missing_cost": sum(1 for p in products if p.price_amount is not None and p.cost_price_amount is None)}


@router.post("/pricing/bulk")
async def pricing_bulk(body: PricingBulkIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    """Bulk price changes. dry_run=true (default) returns the preview; false executes."""
    from app.api.v1.commerce import run_bulk_request

    return await run_bulk_request(db, ctx, body.operation, body.product_ids, body.params, body.dry_run)


@router.get("/pricing/schedules")
async def list_schedules(status: str | None = "scheduled", page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                         ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    q = select(ScheduledPriceChange, Product.name).join(Product, Product.id == ScheduledPriceChange.product_id).where(
        ScheduledPriceChange.business_id == ctx.business_id)
    if status:
        q = q.where(ScheduledPriceChange.status.in_(status.split(",")))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(ScheduledPriceChange.effective_at).offset((page - 1) * page_size).limit(page_size))).all()
    return {"items": [scheduled_dict(c, n) for c, n in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/pricing/schedules", status_code=201)
async def create_schedule(body: ScheduleIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, body.product_id)
    variant = await owned_variant(db, p, body.variant_id)
    change = await schedule_change(db, p, actor=actor_from_ctx(ctx), effective_at=body.effective_at, price_type=body.price_type,
                                   new_amount=body.new_amount, variant=variant, reason=body.reason)
    await db.commit()
    return scheduled_dict(change, p.name)


@router.post("/pricing/schedules/{change_id}/cancel")
async def cancel_schedule(change_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(ScheduledPriceChange).where(
        ScheduledPriceChange.id == _uuid(change_id, "Scheduled change"),
        ScheduledPriceChange.business_id == ctx.business_id))).scalar_one_or_none()
    if c is None:
        raise not_found("Scheduled change")
    if c.status != "scheduled":
        raise AppError(409, "not_cancellable", f"This change is already {c.status}")
    c.status, c.cancelled_at = "cancelled", now_utc()
    record(db, ctx.business_id, actor_from_ctx(ctx), "price.schedule_cancelled", "product", c.product_id,
           f"Scheduled {c.price_type} price change ({fmt(c.new_amount)}) cancelled", {"scheduled_change_id": str(c.id)})
    await db.commit()
    return scheduled_dict(c)


# ---------------------------------------------------------------- inventory
@router.get("/products/{product_id}/inventory")
async def get_inventory(product_id: str, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    from app.services.commerce.catalog_rules import variant_dict

    p = await get_owned_product(db, ctx, product_id)
    return {**inventory_dict(p, ctx.business), "availability": p.availability,
            "variants": [variant_dict(v, p, ctx.business) for v in p.variants if v.archived_at is None]}


@router.put("/products/{product_id}/inventory")
async def put_inventory(product_id: str, body: InventorySettingsIn, ctx: AuthContext = Depends(current_auth),
                        db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, product_id)
    variant = await owned_variant(db, p, body.variant_id)
    actor = actor_from_ctx(ctx)
    settings = {k: getattr(body, k) for k in ("track_inventory", "low_stock_threshold", "backorder_policy", "inventory_location")
                if k in body.model_fields_set}
    if "track_inventory" in settings and settings["track_inventory"] is None:
        raise AppError(422, "invalid", "track_inventory must be true or false")
    await update_settings(db, p, actor=actor, variant=variant, **settings)
    if body.stock_quantity is not None:
        await change_stock(db, p, actor=actor, variant=variant, operation="set", quantity=body.stock_quantity, reason=body.reason)
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    await db.commit()
    await db.refresh(p)
    row = variant or p
    if variant is not None:
        await db.refresh(variant)
    return {**inventory_dict(row, ctx.business), "availability": row.availability, "product_stock_status": p.stock_status}


@router.post("/inventory/adjust")
async def adjust_inventory(body: InventoryAdjustIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    p = await get_owned_product(db, ctx, body.product_id)
    variant = await owned_variant(db, p, body.variant_id)
    change = await change_stock(db, p, actor=actor_from_ctx(ctx), operation=body.operation, quantity=body.quantity,
                                variant=variant, reason=body.reason)
    await db.flush()
    await revalidate_business(db, ctx.business_id)
    await db.commit()
    row = variant or p
    await db.refresh(row)
    return {"previous": change.previous, "adjustment": change.adjustment, "new": change.new, "field": change.field,
            "stock_status": change.status, "transaction_id": str(change.transaction_id),
            "inventory": inventory_dict(row, ctx.business), "product_stock_status": p.stock_status}


@router.post("/inventory/bulk")
async def inventory_bulk(body: InventoryBulkIn, ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    from app.api.v1.commerce import run_bulk_request

    return await run_bulk_request(db, ctx, "inventory_adjust", body.product_ids,
                                  {"operation": body.operation, "quantity": body.quantity, "reason": body.reason}, body.dry_run)


@router.get("/inventory/transactions")
async def transactions(product_id: str | None = None, transaction_type: str | None = None,
                       page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                       ctx: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_db)):
    q = select(InventoryTransaction, Product.name).join(Product, Product.id == InventoryTransaction.product_id).where(
        InventoryTransaction.business_id == ctx.business_id)
    if product_id:
        q = q.where(InventoryTransaction.product_id == _uuid(product_id, "Product"))
    if transaction_type:
        q = q.where(InventoryTransaction.transaction_type == transaction_type)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(InventoryTransaction.created_at.desc(), InventoryTransaction.id)
                             .offset((page - 1) * page_size).limit(page_size))).all()
    return {"items": [tx_dict(t, n) for t, n in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/inventory/low-stock")
async def low_stock(limit: int = Query(100, ge=1, le=500), ctx: AuthContext = Depends(current_auth),
                    db: AsyncSession = Depends(get_db)):
    """Products and variants at or below their low-stock threshold, out of stock or on backorder."""
    from app.services.commerce.inventory import effective_threshold

    statuses = ("low_stock", "out_of_stock", "backorder")
    prods = (await db.execute(select(Product).where(*live_products(ctx.business_id), Product.lifecycle_status != "archived",
                                                    Product.stock_status.in_(statuses))
                              .order_by((Product.stock_on_hand - Product.stock_reserved)).limit(limit))).scalars().all()
    items = []
    for p in prods:
        variants = [v for v in p.variants if v.archived_at is None and v.is_enabled and v.stock_status in statuses]
        if p.track_inventory and p.stock_status in statuses and not variants:
            items.append({"product_id": str(p.id), "variant_id": None, "name": p.name, "sku": p.sku,
                          "stock_status": p.stock_status, "available": p.stock_on_hand - p.stock_reserved,
                          "on_hand": p.stock_on_hand, "threshold": effective_threshold(p, ctx.business)})
        for v in variants:
            items.append({"product_id": str(p.id), "variant_id": str(v.id), "name": f"{p.name} — {v.name}", "sku": v.sku,
                          "stock_status": v.stock_status, "available": v.stock_on_hand - v.stock_reserved,
                          "on_hand": v.stock_on_hand, "threshold": effective_threshold(v, ctx.business)})
    counts = dict((await db.execute(select(Product.stock_status, func.count()).where(
        *live_products(ctx.business_id), Product.lifecycle_status != "archived").group_by(Product.stock_status))).all())
    recent = (await db.execute(select(InventoryTransaction, Product.name).join(Product, Product.id == InventoryTransaction.product_id)
                               .where(InventoryTransaction.business_id == ctx.business_id)
                               .order_by(InventoryTransaction.created_at.desc()).limit(10))).all()
    return {"items": items, "counts": {s: counts.get(s, 0) for s in ("in_stock", "low_stock", "out_of_stock", "backorder", "untracked")},
            "recent_transactions": [tx_dict(t, n) for t, n in recent]}
